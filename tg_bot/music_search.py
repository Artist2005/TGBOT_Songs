"""
Модуль пошуку музики та отримання метаданих через Deezer Public API.
Безкоштовний, без авторизації, без ключів.
"""

import logging
from typing import Optional
import aiohttp

logger = logging.getLogger(__name__)

DEEZER_SEARCH_URL = "https://api.deezer.com/search/track"
DEEZER_TRACK_URL = "https://api.deezer.com/track/{track_id}"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=5)

_session: Optional[aiohttp.ClientSession] = None


async def _get_session() -> aiohttp.ClientSession:
    """Повертає спільну HTTP-сесію для швидких запитів без перестворення."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)
    return _session


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
            album_obj.get("cover_big")
            or album_obj.get("cover_medium")
            or album_obj.get("cover")
            or ""
        ),
        "preview_url": item.get("preview") or "",
    }


async def search_tracks(query: str, limit: int = 10) -> list[dict]:
    """
    Шукає треки через Deezer API.

    Args:
        query: Пошуковий запит (назва пісні, артист тощо)
        limit: Максимальна кількість результатів (до 25)
    """
    if not query or not query.strip():
        return []

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
    Використовується, якщо метаданих немає в кеші або застарів preview_url.
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
