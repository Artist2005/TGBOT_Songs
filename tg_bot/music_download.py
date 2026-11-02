import os
import logging
import tempfile
import asyncio
from typing import Optional
import yt_dlp

logger = logging.getLogger(__name__)

FFMPEG_PATH = "/home/roman_pinchuk/bin/ffmpeg"


def _yt_dlp_download(query: str, output_dir: str) -> Optional[str]:
    """Синхронна функція завантаження через yt-dlp."""
    output_template = os.path.join(output_dir, "%(id)s.%(ext)s")

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': output_template,
        'ffmpeg_location': FFMPEG_PATH,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'default_search': 'ytsearch',
        'socket_timeout': 15,
        'retries': 3,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'mweb', 'ios']
            }
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=True)
            if not info:
                return None
            if 'entries' in info and info['entries']:
                info = info['entries'][0]

            filename = ydl.prepare_filename(info)
            base, _ = os.path.splitext(filename)
            mp3_filename = f"{base}.mp3"

            if os.path.exists(mp3_filename):
                return mp3_filename
            elif os.path.exists(filename):
                return filename
            return None
    except Exception as e:
        logger.error(f"yt-dlp download error for '{query}': {e}")
        return None


async def download_track(artist: str, title: str) -> Optional[str]:
    """Асинхронна обгортка для завантаження треку."""
    loop = asyncio.get_running_loop()
    query = f"{artist} - {title}"
    temp_dir = tempfile.mkdtemp()

    try:
        file_path = await loop.run_in_executor(None, _yt_dlp_download, query, temp_dir)
        return file_path
    except Exception as e:
        logger.error(f"Failed to download track '{query}': {e}")
        return None


def cleanup_file(filepath: str):
    """Видалення тимчасового MP3 файлу та директорії."""
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
            parent_dir = os.path.dirname(filepath)
            if os.path.exists(parent_dir) and not os.listdir(parent_dir):
                os.rmdir(parent_dir)
    except Exception as e:
        logger.warning(f"Error cleaning up file {filepath}: {e}")


def cleanup_temp_dir():
    """Заглушка для очищення тимчасових папок при старті."""
    pass
