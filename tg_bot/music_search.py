"""
Модуль швидкого пошуку музики, чартів, схожих треків, текстів пісень (Lyrics)
та розпізнавання Spotify-посилань через Deezer Public API та LRCLIB.
Використовує пул постійних з'єднань (Keep-Alive + DNS кеш) та in-memory TTL-кеш.
"""

import re
import time
import logging
from typing import Optional
from urllib.parse import quote
import aiohttp

logger = logging.getLogger(__name__)

DEEZER_SEARCH_URL = "https://api.deezer.com/search/track"
DEEZER_TRACK_URL = "https://api.deezer.com/track/{track_id}"
DEEZER_CHART_URL = "https://api.deezer.com/chart/0/tracks"
DEEZER_ARTIST_TOP_URL = "https://api.deezer.com/artist/{artist_id}/top"
LRCLIB_GET_URL = "https://lrclib.net/api/get"
LRCLIB_SEARCH_URL = "https://lrclib.net/api/search"
LYRICS_OVH_URL = "https://api.lyrics.ovh/v1/{artist}/{title}"
SPOTIFY_OEMBED_URL = "https://open.spotify.com/oembed"

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=6, connect=3)

# In-memory TTL-кеші
SEARCH_CACHE_TTL = 180  # 3 хвилини
CHART_CACHE_TTL = 600   # 10 хвилин

_search_cache: dict[str, tuple[float, list[dict]]] = {}
_chart_cache: Optional[tuple[float, list[dict]]] = None
_lyrics_cache: dict[str, str] = {}

_session: Optional[aiohttp.ClientSession] = None


async def _get_session() -> aiohttp.ClientSession:
    """Повертає спільну HTTP-сесію з Keep-Alive та DNS-кешуванням для мінімальної затримки."""
    global _session
    if _session is None or _session.closed:
        connector = aiohttp.TCPConnector(
            limit=64,
            ttl_dns_cache=300,
            keepalive_timeout=60,
            enable_cleanup_closed=True,
        )
        _session = aiohttp.ClientSession(
            connector=connector,
            timeout=REQUEST_TIMEOUT,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
                "User-Agent": "TelegramMusicExplorerBot/2.0",
            },
        )
    return _session


async def warmup_session():
    """Прогріває TCP/TLS-з'єднання з Deezer API та завантажує Топ-чарт у RAM при старті."""
    try:
        await get_chart_tracks(limit=10)
        logger.info("Deezer API connection pool and Top Chart warmed up")
    except Exception as e:
        logger.debug(f"Warmup skipped: {e}")


async def close_session():
    """Закриває HTTP-сесію при зупинці бота."""
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None


def _parse_deezer_item(item: dict) -> Optional[dict]:
    """Нормалізує об'єкт треку з відповіді Deezer API."""
    if not item or "id" not in item or "error" in item:
        return None
    artist_obj = item.get("artist") or {}
    album_obj = item.get("album") or {}
    return {
        "id": int(item["id"]),
        "title": item.get("title") or item.get("title_short") or "Unknown Title",
        "artist": artist_obj.get("name") or "Unknown Artist",
        "artist_id": int(artist_obj["id"]) if artist_obj.get("id") else 0,
        "album": album_obj.get("title") or "",
        "duration": int(item.get("duration") or 0),
        "cover_url": (
            album_obj.get("cover_medium")
            or album_obj.get("cover_big")
            or album_obj.get("cover")
            or ""
        ),
        "preview_url": item.get("preview") or "",
    }


async def search_tracks(query: str, limit: int = 10) -> list[dict]:
    """
    Шукає треки через Deezer API з миттєвим поверненням з TTL-кешу при повторних запитах.
    """
    if not query or not query.strip():
        return []

    norm_query = " ".join(query.strip().lower().split())
    cache_key = f"{norm_query}:{limit}"
    now = time.monotonic()

    cached = _search_cache.get(cache_key)
    if cached and (now - cached[0]) < SEARCH_CACHE_TTL:
        return cached[1]

    params = {
        "q": query.strip(),
        "limit": min(limit, 25),
    }

    try:
        session = await _get_session()
        async with session.get(DEEZER_SEARCH_URL, params=params) as resp:
            if resp.status != 200:
                logger.error(f"Deezer API search error: HTTP {resp.status}")
                return []

            data = await resp.json()
            tracks = []
            for item in data.get("data", []):
                parsed = _parse_deezer_item(item)
                if parsed:
                    tracks.append(parsed)

            if tracks:
                if len(_search_cache) > 500:
                    keys = list(_search_cache.keys())[:250]
                    for k in keys:
                        _search_cache.pop(k, None)
                _search_cache[cache_key] = (now, tracks)

            logger.info(f"Found {len(tracks)} tracks for query: '{query}'")
            return tracks

    except aiohttp.ClientError as e:
        logger.error(f"Network error during Deezer search: {e}")
        return []
    except Exception as e:
        logger.error(f"Unexpected error during search: {e}")
        return []


async def get_chart_tracks(limit: int = 10) -> list[dict]:
    """
    Отримує світовий Топ-чарт треків із Deezer (миттєво з RAM-кешу протягом 10 хвилин).
    Використовується при порожньому інлайн-запиті @bot та команді /top.
    """
    global _chart_cache
    now = time.monotonic()
    if _chart_cache and (now - _chart_cache[0]) < CHART_CACHE_TTL:
        return _chart_cache[1][:limit]

    try:
        session = await _get_session()
        async with session.get(DEEZER_CHART_URL, params={"limit": max(limit, 15)}) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            tracks = []
            for item in data.get("data", []):
                parsed = _parse_deezer_item(item)
                if parsed:
                    tracks.append(parsed)
            if tracks:
                _chart_cache = (now, tracks)
            return tracks[:limit]
    except Exception as e:
        logger.warning(f"Failed to fetch Deezer chart: {e}")
        return []


async def get_track_by_id(deezer_id: int) -> Optional[dict]:
    """
    Отримує повну інформацію про трек та свіжий preview_url за Deezer ID.
    """
    if deezer_id <= 0:
        return None
    url = DEEZER_TRACK_URL.format(track_id=deezer_id)
    try:
        session = await _get_session()
        async with session.get(url) as resp:
            if resp.status != 200:
                logger.warning(f"Deezer API track lookup HTTP {resp.status} for id={deezer_id}")
                return None
            data = await resp.json()
            return _parse_deezer_item(data)
    except Exception as e:
        logger.error(f"Failed to fetch track id={deezer_id} from Deezer: {e}")
        return None


async def get_similar_tracks(
    deezer_id: int, artist: str = "", title: str = "", limit: int = 6
) -> list[dict]:
    """
    Підбирає схожі треки та топ-хіти виконавця без дублікатів поточного треку.
    """
    cache_key = f"sim:{deezer_id}:{artist.lower()}:{limit}"
    now = time.monotonic()
    cached = _search_cache.get(cache_key)
    if cached and (now - cached[0]) < SEARCH_CACHE_TTL:
        return cached[1]

    candidates: list[dict] = []
    artist_id = 0

    if deezer_id > 0:
        track_info = await get_track_by_id(deezer_id)
        if track_info:
            artist_id = int(track_info.get("artist_id") or 0)
            if not artist:
                artist = track_info.get("artist", "")
            if not title:
                title = track_info.get("title", "")

    if artist_id > 0:
        try:
            session = await _get_session()
            url = DEEZER_ARTIST_TOP_URL.format(artist_id=artist_id)
            async with session.get(url, params={"limit": limit + 6}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for item in data.get("data", []):
                        parsed = _parse_deezer_item(item)
                        if parsed:
                            candidates.append(parsed)
        except Exception as e:
            logger.debug(f"Artist top lookup failed for {artist_id}: {e}")

    if len(candidates) < limit and artist:
        searched = await search_tracks(artist, limit=limit + 6)
        candidates.extend(searched)

    results: list[dict] = []
    seen_ids = {int(deezer_id)}
    norm_current_title = title.strip().lower()
    seen_titles = {norm_current_title} if norm_current_title else set()

    for item in candidates:
        tid = int(item["id"])
        t_norm = item["title"].strip().lower()
        if tid in seen_ids or t_norm in seen_titles:
            continue
        seen_ids.add(tid)
        seen_titles.add(t_norm)
        results.append(item)
        if len(results) >= limit:
            break

    if results:
        _search_cache[cache_key] = (now, results)
    return results


def _clean_song_term(term: str) -> str:
    """Очищує назву треку від службових дужок типу (Official Video), [prod. ...] для кращого пошуку тексту."""
    cleaned = re.sub(r"\s*[\(\[][^)\]]*(?:official|video|audio|lyric|remaster|feat\.|ft\.|prod\.)[^)\]]*[\)\]]", "", term, flags=re.IGNORECASE)
    return cleaned.strip() or term.strip()


async def get_track_lyrics(artist: str, title: str) -> Optional[str]:
    """
    Шукає повний текст пісні (Lyrics) через безкоштовний API LRCLIB (з фолбеком на lyrics.ovh).
    """
    if not title:
        return None

    clean_artist = _clean_song_term(artist) if artist and artist != "Unknown Artist" else ""
    clean_title = _clean_song_term(title)
    cache_key = f"{clean_artist.lower()}::{clean_title.lower()}"

    if cache_key in _lyrics_cache:
        return _lyrics_cache[cache_key]

    session = await _get_session()

    # 1. Прямий запит до LRCLIB /api/get
    if clean_artist:
        try:
            async with session.get(
                LRCLIB_GET_URL,
                params={"artist_name": clean_artist, "track_name": clean_title},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    lyrics = (data.get("plainLyrics") or "").strip()
                    if lyrics:
                        lyrics = re.sub(r"\n{3,}", "\n\n", lyrics)
                        _lyrics_cache[cache_key] = lyrics
                        return lyrics
        except Exception as e:
            logger.debug(f"LRCLIB direct get failed: {e}")

    # 2. Пошук через LRCLIB /api/search
    search_q = f"{clean_artist} {clean_title}".strip()
    try:
        async with session.get(LRCLIB_SEARCH_URL, params={"q": search_q}) as resp:
            if resp.status == 200:
                items = await resp.json()
                if isinstance(items, list):
                    for item in items:
                        lyrics = (item.get("plainLyrics") or "").strip()
                        if lyrics:
                            lyrics = re.sub(r"\n{3,}", "\n\n", lyrics)
                            _lyrics_cache[cache_key] = lyrics
                            return lyrics
    except Exception as e:
        logger.debug(f"LRCLIB search failed: {e}")

    # 3. Фолбек на lyrics.ovh
    if clean_artist:
        try:
            ovh_url = LYRICS_OVH_URL.format(
                artist=quote(clean_artist), title=quote(clean_title)
            )
            async with session.get(ovh_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    lyrics = (data.get("lyrics") or "").strip()
                    if lyrics:
                        lyrics = re.sub(r"\r\n", "\n", lyrics)
                        lyrics = re.sub(r"\n{3,}", "\n\n", lyrics)
                        _lyrics_cache[cache_key] = lyrics
                        return lyrics
        except Exception as e:
            logger.debug(f"lyrics.ovh fallback failed: {e}")

    return None


async def resolve_spotify_query(url: str) -> Optional[str]:
    """
    Витягує 'Виконавець - Назва' з посилання Spotify (open.spotify.com/track/...)
    без необхідності Spotify API ключів.
    """
    if not url:
        return None
    session = await _get_session()
    try:
        # Спочатку пробуємо швидкий публічний oEmbed
        async with session.get(SPOTIFY_OEMBED_URL, params={"url": url}) as resp:
            if resp.status == 200:
                data = await resp.json()
                title = (data.get("title") or "").strip()
                # Також перевіримо HTML сторінки для отримання імені виконавця
                async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as page_resp:
                    if page_resp.status == 200:
                        html_text = await page_resp.text()
                        # <title>Song - song and lyrics by Artist | Spotify</title>
                        m = re.search(r"<title>([^<]+)</title>", html_text, re.IGNORECASE)
                        if m:
                            raw_t = m.group(1).replace("| Spotify", "").strip()
                            by_match = re.split(r"\s+-\s+song(?:\s+and\s+lyrics)?\s+by\s+", raw_t, flags=re.IGNORECASE)
                            if len(by_match) == 2:
                                return f"{by_match[1].strip()} - {by_match[0].strip()}"
                if title:
                    return title
    except Exception as e:
        logger.debug(f"Spotify resolution failed for {url}: {e}")
    return None


async def download_url_bytes(url: str, max_bytes: int = 10 * 1024 * 1024) -> Optional[bytes]:
    """Завантажує байти за URL (для обкладинки альбому або 30-сек прев'ю)."""
    if not url:
        return None
    try:
        session = await _get_session()
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            content = await resp.read()
            if len(content) > max_bytes:
                return None
            return content
    except Exception as e:
        logger.debug(f"Could not download bytes from {url}: {e}")
        return None


def format_duration(seconds: int) -> str:
    """Форматує тривалість із секунд у M:SS."""
    seconds = max(0, int(seconds or 0))
    minutes = seconds // 60
    secs = seconds % 60
    return f"{minutes}:{secs:02d}"
