"""
Модуль швидкого пошуку музики та отримання метаданих через Deezer Public API.
Використовує пул постійних з'єднань (Keep-Alive + DNS кеш) та in-memory TTL-кеш запитів.
"""

import time
import logging
from typing import Optional
import aiohttp

logger = logging.getLogger(__name__)

DEEZER_SEARCH_URL = "https://api.deezer.com/search/track"
DEEZER_TRACK_URL = "https://api.deezer.com/track/{track_id}"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=6, connect=3)

# In-memory TTL-кеш пошукових запитів: key -> (timestamp, list[dict])
SEARCH_CACHE_TTL = 180  # 3 хвилини (preview_url дійсний кілька годин)
_search_cache: dict[str, tuple[float, list[dict]]] = {}

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
            headers={"Accept": "application/json", "Accept-Encoding": "gzip, deflate"},
        )
    return _session


async def warmup_session():
    """Прогріває TCP/TLS-з'єднання з Deezer API при старті бота."""
    try:
        session = await _get_session()
        async with session.get(DEEZER_SEARCH_URL, params={"q": "a", "limit": 1}) as resp:
            await resp.read()
        logger.info("Deezer API connection pool warmed up")
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
                    # Очищуємо найстарішу половину записів
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


async def get_track_by_id(deezer_id: int) -> Optional[dict]:
    """
    Отримує повну інформацію про трек та свіжий preview_url за Deezer ID.
    """
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
