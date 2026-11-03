"""
Модуль швидкого пошуку музики, чартів, схожих треків, текстів пісень (Lyrics)
та розпізнавання Spotify-посилань через Deezer Public API та LRCLIB.
Використовує пул постійних з'єднань (Keep-Alive + DNS кеш) та in-memory TTL-кеш.
"""

import re
import time
import asyncio
import logging
from typing import Optional
from urllib.parse import quote
import aiohttp

logger = logging.getLogger(__name__)

DEEZER_SEARCH_URL = "https://api.deezer.com/search/track"
DEEZER_TRACK_URL = "https://api.deezer.com/track/{track_id}"
DEEZER_CHART_URL = "https://api.deezer.com/chart/0/tracks"
DEEZER_ARTIST_TOP_URL = "https://api.deezer.com/artist/{artist_id}/top"
GENIUS_LYRIC_SEARCH_URL = "https://genius.com/api/search/lyric"
YOUTUBE_INNERTUBE_SEARCH_URL = "https://www.youtube.com/youtubei/v1/search?prettyPrint=false"
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


def _tokenize_words(text: str) -> list[str]:
    """Розбиває рядок на значущі слова (від 2 символів) для оцінки збігу."""
    return [w for w in re.findall(r"[\w'’іїєґ]+", text.lower()) if len(w) >= 2]


def _direct_overlap_score(query_words: list[str], tracks: list[dict]) -> float:
    """
    Оцінює (від 0.0 до 1.0), наскільки прямі результати Deezer збігаються зі словами запиту
    в назві треку або імені виконавця (ігноруючи сумнівні караоке/fanchants кавери).
    """
    if not query_words or not tracks:
        return 0.0

    best = 0.0
    for tr in tracks[:3]:
        artist_l = tr.get("artist", "").lower()
        title_l = tr.get("title", "").lower()
        combined = f"{artist_l} {title_l}"
        if any(
            bad in combined
            for bad in ("karaoke", "fanchants", "tribute", "made famous", "8-bit")
        ):
            continue
        matched = sum(1 for w in query_words if w in combined)
        score = matched / len(query_words)
        if score > best:
            best = score
    return best


async def _deezer_search_raw(
    session: aiohttp.ClientSession, query: str, limit: int = 10
) -> list[dict]:
    """Виконує один прямий запит до Deezer Search API."""
    if not query or not query.strip():
        return []
    params = {"q": query.strip(), "limit": min(limit, 25)}
    try:
        async with session.get(DEEZER_SEARCH_URL, params=params) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            tracks = []
            for item in data.get("data", []):
                parsed = _parse_deezer_item(item)
                if parsed:
                    tracks.append(parsed)
            return tracks
    except Exception as e:
        logger.debug(f"Deezer raw search error for '{query}': {e}")
        return []


def _extract_genius_candidates(artist_raw: str, title_raw: str) -> list[tuple[str, str]]:
    """
    Перетворює пару (artist_names, title) з Genius на список пар (повний_запит, фолбек_назва)
    для пошуку у Deezer, враховуючи латинські/кириличні дублі в дужках типу 'Океан Ельзи (Okean Elzy)'.
    """
    if not artist_raw or not title_raw:
        return []
    art_low = artist_raw.lower()
    tit_low = title_raw.lower()
    if (
        "genius" in art_low
        or "переклад" in tit_low
        or "translation" in tit_low
        or "romanization" in art_low
    ):
        return []

    m_art = re.match(r"^([^\(\[]+)\s*\(([^\(\)]+)\)", artist_raw.strip())
    art_main = (
        m_art.group(1).strip()
        if m_art
        else re.sub(r"\s*[\(\[].*?[\)\]]", "", artist_raw).strip()
    )
    art_alt = m_art.group(2).strip() if m_art else ""

    art_parts = [
        p.strip()
        for p in re.split(r"\s+(?:ft\.?|feat\.?|&|,)\s+", art_main, flags=re.IGNORECASE)
        if p.strip()
    ]
    art_primary = art_parts[0] if art_parts else art_main
    art_secondary = art_parts[1] if len(art_parts) > 1 else ""
    art_alt = re.split(r"\s+(?:ft\.?|feat\.?|&|,)\s+", art_alt, flags=re.IGNORECASE)[0].strip()

    t_main = re.sub(r"\s*[\(\[].*?[\)\]]", "", title_raw).strip() or title_raw.strip()
    t_main = re.sub(r"\*+$", "", t_main).strip()
    if not t_main:
        return []

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a in (art_primary, art_alt, art_secondary):
        if a:
            full_q = f"{a} {t_main}"
            if full_q.lower() not in seen:
                seen.add(full_q.lower())
                out.append((full_q, t_main))
    return out


def _clean_yt_video_candidate(raw_title: str, channel: str = "") -> list[tuple[str, str]]:
    """Витягує чисту пару (Виконавець Назва, Назва) із заголовка музичного відео YouTube."""
    if not raw_title:
        return []
    t = re.split(r"\s*(?:\||//|#)\s*", raw_title)[0].strip()
    t = re.sub(
        r"\s*[\(\[][^)\]]*(?:official|video|audio|lyric|текст|кліп|live|premiere|прем'єра|eurovision|пісня|року|remix|ремікс|cover|кавер|hd|4k|1080p|prod\.?|feat\.?|ft\.?)[^)\]]*[\)\]]",
        "",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", t)
    t = re.sub(r"[^\w\s'’іїєґ\-—–&.,!]", " ", t)
    t = " ".join(t.split()).strip(" -—–.")
    if not t:
        return []

    dash_parts = re.split(r"\s+(?:—|–|-)\s+", t, maxsplit=1)
    if len(dash_parts) == 2:
        art, song = dash_parts[0].strip(), dash_parts[1].strip()
        song = re.sub(r"\b(?:19|20)\d{2}\b$", "", song).strip()
        if art and song:
            return [(f"{art} {song}", song)]

    clean_ch = re.sub(r"\s*-\s*Topic$", "", channel or "", flags=re.IGNORECASE).strip()
    if clean_ch and t:
        return [(f"{clean_ch} {t}", t)]
    return [(t, t)]


async def _fetch_genius_lyric_candidates(
    session: aiohttp.ClientSession, query: str
) -> list[tuple[str, str]]:
    """Шукає пісню за словами з тексту через Genius Lyric Search API."""
    q_words = _tokenize_words(query)
    if not q_words:
        return []
    try:
        async with session.get(
            GENIUS_LYRIC_SEARCH_URL,
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=aiohttp.ClientTimeout(total=2.8),
        ) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            sections = data.get("response", {}).get("sections", [])
            hits = sections[0].get("hits", []) if sections else []
            out: list[tuple[str, str]] = []
            seen: set[str] = set()
            for h in hits[:5]:
                res = h.get("result", {})
                art = res.get("artist_names", "")
                tit = res.get("title", "")
                hl = h.get("highlights", [])
                snippet = " ".join(x.get("value", "") for x in hl)
                combined_text = f"{art} {tit} {snippet}".lower()
                matched = sum(1 for w in q_words if w in combined_text)
                if matched < max(1, (len(q_words) + 1) // 2):
                    continue
                for full_q, fallback_t in _extract_genius_candidates(art, tit):
                    if full_q.lower() not in seen:
                        seen.add(full_q.lower())
                        out.append((full_q, fallback_t))
            return out[:4]
    except Exception as e:
        logger.debug(f"Genius lyric search failed for '{query}': {e}")
        return []


async def _fetch_youtube_lyric_candidates(
    session: aiohttp.ClientSession, query: str
) -> list[tuple[str, str]]:
    """Шукає пісню за словами з тексту через швидкий YouTube InnerTube JSON API."""
    q_search = f"{query} пісня" if not query.isascii() else f"{query} song lyrics"
    payload = {
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20240726.00.00",
                "hl": "uk",
                "gl": "UA",
            }
        },
        "query": q_search,
    }
    try:
        async with session.post(
            YOUTUBE_INNERTUBE_SEARCH_URL,
            json=payload,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=aiohttp.ClientTimeout(total=2.8),
        ) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            contents = (
                data.get("contents", {})
                .get("twoColumnSearchResultsRenderer", {})
                .get("primaryContents", {})
                .get("sectionListRenderer", {})
                .get("contents", [])
            )
            out: list[tuple[str, str]] = []
            seen: set[str] = set()
            video_count = 0
            for sec in contents:
                items = sec.get("itemSectionRenderer", {}).get("contents", [])
                for it in items:
                    vr = it.get("videoRenderer")
                    if not vr:
                        continue
                    title = "".join(
                        x.get("text", "") for x in vr.get("title", {}).get("runs", [])
                    )
                    channel = "".join(
                        x.get("text", "") for x in vr.get("ownerText", {}).get("runs", [])
                    )
                    for full_q, fallback_t in _clean_yt_video_candidate(title, channel):
                        if full_q.lower() not in seen:
                            seen.add(full_q.lower())
                            out.append((full_q, fallback_t))
                    video_count += 1
                    if video_count >= 3:
                        break
                if video_count >= 3:
                    break
            return out[:4]
    except Exception as e:
        logger.debug(f"YouTube lyric search failed for '{query}': {e}")
        return []


async def _fetch_lrclib_lyric_candidates(
    session: aiohttp.ClientSession, query: str
) -> list[tuple[str, str]]:
    """Додатковий пошук за текстом через LRCLIB (для латинських запитів)."""
    if not query.isascii():
        return []
    try:
        async with session.get(
            LRCLIB_SEARCH_URL,
            params={"q": query},
            timeout=aiohttp.ClientTimeout(total=2.5),
        ) as resp:
            if resp.status != 200:
                return []
            items = await resp.json()
            if not isinstance(items, list):
                return []
            out: list[tuple[str, str]] = []
            seen: set[str] = set()
            for item in items[:3]:
                art = (item.get("artistName") or "").strip()
                tit = (item.get("trackName") or "").strip()
                if art and tit:
                    full_q = f"{art} {tit}"
                    if full_q.lower() not in seen:
                        seen.add(full_q.lower())
                        out.append((full_q, tit))
            return out
    except Exception:
        return []


async def _search_tracks_by_lyrics(
    session: aiohttp.ClientSession, query: str, limit: int = 10
) -> list[dict]:
    """
    Знаходить треки за словами з пісні (які пам'ятає користувач), паралельно опитуючи
    YouTube InnerTube, Genius Lyric Search та LRCLIB, а потім резолвить знайдені
    пісні у повноцінні треки Deezer (з прев'ю, обкладинками та ID).
    """
    yt_cands, g_cands, lrc_cands = await asyncio.gather(
        _fetch_youtube_lyric_candidates(session, query),
        _fetch_genius_lyric_candidates(session, query),
        _fetch_lrclib_lyric_candidates(session, query),
    )

    merged_candidates: list[tuple[str, str]] = []
    seen_q: set[str] = set()
    for cand in yt_cands[:2] + g_cands[:3] + lrc_cands[:2] + yt_cands[2:]:
        full_q, fallback_t = cand
        if full_q.lower() not in seen_q:
            seen_q.add(full_q.lower())
            merged_candidates.append((full_q, fallback_t))

    if not merged_candidates:
        return []

    async def _resolve_candidate(full_q: str, fallback_t: str) -> list[dict]:
        res = await _deezer_search_raw(session, full_q, limit=2)
        if not res and fallback_t and fallback_t.lower() != full_q.lower():
            res = await _deezer_search_raw(session, fallback_t, limit=2)
        return res

    resolved_batches = await asyncio.gather(
        *[_resolve_candidate(fq, ft) for fq, ft in merged_candidates[:4]]
    )

    tracks: list[dict] = []
    seen_ids: set[int] = set()
    for batch in resolved_batches:
        for tr in batch:
            tid = int(tr["id"])
            if tid not in seen_ids:
                seen_ids.add(tid)
                tracks.append(tr)
                if len(tracks) >= limit:
                    return tracks
    return tracks


async def search_tracks(query: str, limit: int = 10) -> list[dict]:
    """
    Шукає треки за назвою, виконавцем АБО за словами з пісні (текстом, який пам'ятає користувач).
    Використовує комбінацію Deezer API + розпізнавання тексту (Genius + YouTube + LRCLIB) та TTL-кеш.
    """
    if not query or not query.strip():
        return []

    clean_q = query.strip()
    norm_query = " ".join(clean_q.lower().split())
    cache_key = f"{norm_query}:{limit}"
    now = time.monotonic()

    cached = _search_cache.get(cache_key)
    if cached and (now - cached[0]) < SEARCH_CACHE_TTL:
        return cached[1]

    try:
        session = await _get_session()
        q_words = _tokenize_words(clean_q)

        # Для довгих фраз (4+ слів — майже напевно рядок із тексту пісні) запускаємо паралельно
        if len(q_words) >= 4:
            direct_tracks, lyric_tracks = await asyncio.gather(
                _deezer_search_raw(session, clean_q, limit=limit),
                _search_tracks_by_lyrics(session, clean_q, limit=limit),
            )
            primary = lyric_tracks if lyric_tracks else direct_tracks
            secondary = direct_tracks if lyric_tracks else []
        else:
            # Для 1–3 слів спочатку робимо миттєвий прямий запит до Deezer (~100 мс)
            direct_tracks = await _deezer_search_raw(session, clean_q, limit=limit)
            overlap = _direct_overlap_score(q_words, direct_tracks)
            # Якщо прямий пошук не знайшов точного збігу в назві/виконавці — шукаємо по тексту пісні
            if len(q_words) >= 2 and (len(direct_tracks) < 2 or overlap < 0.8):
                lyric_tracks = await _search_tracks_by_lyrics(session, clean_q, limit=limit)
                primary = lyric_tracks if lyric_tracks else direct_tracks
                secondary = direct_tracks if lyric_tracks else []
            else:
                primary = direct_tracks
                secondary = []

        tracks: list[dict] = []
        seen_ids: set[int] = set()
        for tr in primary + secondary:
            tid = int(tr["id"])
            if tid not in seen_ids:
                seen_ids.add(tid)
                tracks.append(tr)
                if len(tracks) >= limit:
                    break

        if tracks:
            if len(_search_cache) > 500:
                keys = list(_search_cache.keys())[:250]
                for k in keys:
                    _search_cache.pop(k, None)
            _search_cache[cache_key] = (now, tracks)

        logger.info(f"Found {len(tracks)} tracks for query: '{clean_q}'")
        return tracks

    except Exception as e:
        logger.error(f"Unexpected error during search for '{clean_q}': {e}")
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
    """Очищує назву треку від службових дужок типу (Official Video), (with ...), [prod. ...] для точного пошуку тексту."""
    cleaned = re.sub(
        r"\s*[\(\[][^)\]]*(?:official|video|audio|lyric|remaster|feat\.?|ft\.?|with\s|prod\.?)[^)\]]*[\)\]]",
        "",
        term,
        flags=re.IGNORECASE,
    )
    return cleaned.strip() or term.strip()


async def _fetch_netease_lyrics(
    session: aiohttp.ClientSession, clean_artist: str, clean_title: str
) -> Optional[str]:
    """
    Швидкий пошук тексту пісні через NetEase Music API (<250 мс, підтримує кирилицю та латиницю без лімітів 429/503).
    """
    queries = [f"{clean_artist} {clean_title}".strip(), clean_title]
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://music.163.com/",
    }
    for q in queries:
        if not q:
            continue
        try:
            async with session.post(
                "https://music.163.com/api/search/get",
                data={"s": q, "type": "1", "offset": "0", "total": "true", "limit": "4"},
                headers=headers,
            ) as resp:
                if resp.status != 200:
                    continue
                res = await resp.json(content_type=None)
                result = res.get("result") if isinstance(res, dict) else None
                if not isinstance(result, dict):
                    continue
                songs = result.get("songs") or []
                for song in songs[:3]:
                    sid = song.get("id")
                    if not sid:
                        continue
                    async with session.get(
                        f"https://music.163.com/api/song/lyric?os=pc&id={sid}&lv=-1",
                        headers=headers,
                    ) as lresp:
                        if lresp.status != 200:
                            continue
                        lres = await lresp.json(content_type=None)
                        raw = ((lres.get("lrc") or {}).get("lyric") or "") if isinstance(lres, dict) else ""
                        if not raw:
                            continue
                        lines = []
                        for line in raw.splitlines():
                            cleaned_line = re.sub(r"\[[^\]]+\]", "", line).strip()
                            if re.match(
                                r"^(?:作词|作曲|编曲|制作人|混音|母带|Lyricist|Composer|Arranger|Producer)\s*[:：]",
                                cleaned_line,
                                re.IGNORECASE,
                            ):
                                continue
                            lines.append(cleaned_line)
                        text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
                        if len(text) > 100:
                            return text
        except Exception as e:
            logger.debug(f"NetEase lyrics lookup failed for '{q}': {e}")
    return None


async def get_track_lyrics(artist: str, title: str) -> Optional[str]:
    """
    Шукає повний текст пісні (Lyrics) через:
    1) LRCLIB API (для латиниці)
    2) NetEase Cloud Music API (універсальне джерело: кирилиця + світові треки, без лімітів 503/429)
    3) lyrics.ovh (резервний фолбек)
    """
    if not title:
        return None

    clean_artist = _clean_song_term(artist) if artist and artist != "Unknown Artist" else ""
    clean_title = _clean_song_term(title)
    cache_key = f"{clean_artist.lower()}::{clean_title.lower()}"

    if cache_key in _lyrics_cache:
        return _lyrics_cache[cache_key]

    session = await _get_session()
    is_ascii_query = f"{clean_artist}{clean_title}".isascii()

    # 1. Прямий запит до LRCLIB /api/get (тільки для ASCII, бо на кирилиці LRCLIB віддає 503)
    if clean_artist and is_ascii_query:
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

    # 2. Швидкий пошук через NetEase Music API (працює і для українських/кириличних, і для світових пісень)
    netease_res = await _fetch_netease_lyrics(session, clean_artist, clean_title)
    if netease_res:
        _lyrics_cache[cache_key] = netease_res
        return netease_res

    # 3. Пошук через LRCLIB /api/search (якщо запит латиницею)
    if is_ascii_query:
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

    # 4. Фолбек на lyrics.ovh
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
