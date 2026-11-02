"""
Модуль пошуку музики через Deezer Public API.
Безкоштовний, без авторизації, без ключів.
"""

import aiohttp
import logging
from typing import Optional

logger = logging.getLogger(__name__)

DEEZER_SEARCH_URL = "https://api.deezer.com/search/track"


async def search_tracks(query: str, limit: int = 5) -> list[dict]:
    """
    Шукає треки через Deezer API.
    
    Args:
        query: Пошуковий запит (назва пісні, артист тощо)
        limit: Максимальна кількість результатів (до 25)
    
    Returns:
        Список словників з інформацією про треки:
        - id: Deezer ID треку
        - title: Назва пісні
        - artist: Ім'я артиста
        - album: Назва альбому
        - duration: Тривалість у секундах
        - cover_url: URL обкладинки альбому
        - preview_url: URL 30-сек прев'ю (fallback)
    """
    if not query or not query.strip():
        return []

    params = {
        "q": query.strip(),
        "limit": min(limit, 25),
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(DEEZER_SEARCH_URL, params=params) as resp:
                if resp.status != 200:
                    logger.error(f"Deezer API error: {resp.status}")
                    return []

                data = await resp.json()
                tracks = []

                for item in data.get("data", []):
                    track = {
                        "id": item.get("id"),
                        "title": item.get("title", "Unknown"),
                        "artist": item.get("artist", {}).get("name", "Unknown"),
                        "album": item.get("album", {}).get("title", ""),
                        "duration": item.get("duration", 0),
                        "cover_url": item.get("album", {}).get("cover_medium", ""),
                        "preview_url": item.get("preview", ""),
                    }
                    tracks.append(track)

                logger.info(f"Found {len(tracks)} tracks for query: '{query}'")
                return tracks

    except aiohttp.ClientError as e:
        logger.error(f"Network error during Deezer search: {e}")
        return []
    except Exception as e:
        logger.error(f"Unexpected error during search: {e}")
        return []


def format_duration(seconds: int) -> str:
    """Форматує тривалість з секунд у MM:SS."""
    minutes = seconds // 60
    secs = seconds % 60
    return f"{minutes}:{secs:02d}"
