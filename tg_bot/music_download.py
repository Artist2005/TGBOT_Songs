"""
Модуль швидкого завантаження треків через yt-dlp та роботи з FFmpeg.
Оптимізований для максимальної швидкості пошуку на YouTube, багатопотокового
завантаження фрагментів та конвертації в MP3.
"""

import os
import shutil
import logging
import tempfile
import asyncio
import subprocess
from typing import Optional
from concurrent.futures import ThreadPoolExecutor
import yt_dlp

logger = logging.getLogger(__name__)

TEMP_PREFIX = "tgbot_dl_"
YTDLP_CACHE_DIR = os.path.join(tempfile.gettempdir(), "tgbot_ytdlp_cache")

# Окремий пул потоків для завантажень та FFmpeg, щоб не блокувати основний цикл
_executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="music_dl")


def _resolve_ffmpeg_path() -> Optional[str]:
    """Визначає шлях до виконуваного файлу ffmpeg (з .env, локальної папки VM або системного PATH)."""
    env_path = os.getenv("FFMPEG_PATH")
    if env_path and os.path.exists(env_path):
        return env_path

    vm_default = "/home/roman_pinchuk/bin/ffmpeg"
    if os.path.exists(vm_default):
        return vm_default

    which_ffmpeg = shutil.which("ffmpeg")
    if which_ffmpeg:
        return which_ffmpeg

    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass

    return None


FFMPEG_PATH = _resolve_ffmpeg_path()


def _make_match_filter(target_duration: int = 0):
    """Створює фільтр для відсіювання стрімів, Shorts та багатогодинних міксів."""
    max_duration = max(int(target_duration * 2.5), 900) if target_duration > 0 else 900
    min_duration = 25 if (target_duration == 0 or target_duration >= 40) else 10

    def _filter(info_dict, *, incomplete: bool = False):
        if info_dict.get("is_live"):
            return "Skipping live stream"
        duration = info_dict.get("duration")
        if duration:
            if duration > max_duration:
                return f"Track is too long ({duration}s > {max_duration}s)"
            if duration < min_duration:
                return f"Track is too short ({duration}s < {min_duration}s)"
        return None

    return _filter


def _yt_dlp_download(query: str, output_dir: str, target_duration: int = 0) -> Optional[str]:
    """Синхронна функція швидкого завантаження через yt-dlp."""
    output_template = os.path.join(output_dir, "%(id)s.%(ext)s")

    ydl_opts = {
        # Обираємо легкий аудіопотік (m4a/webm до 192k), який качається в рази швидше за відео
        "format": "bestaudio[ext=m4a][abr<=192]/bestaudio[ext=webm][abr<=192]/bestaudio/best",
        "outtmpl": output_template,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            },
        ],
        "postprocessor_args": {
            "extractaudio": ["-threads", "0"],
        },
        "concurrent_fragment_downloads": 4,
        "cachedir": YTDLP_CACHE_DIR,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "lazy_playlist": True,
        "default_search": "ytsearch1",
        "max_filesize": 48 * 1024 * 1024,  # Ліміт Telegram Bot API (50 MB)
        "match_filter": _make_match_filter(target_duration),
        "socket_timeout": 10,
        "retries": 2,
        "fragment_retries": 2,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        },
    }

    if FFMPEG_PATH:
        ydl_opts["ffmpeg_location"] = FFMPEG_PATH

    search_variants = [
        f"ytsearch1:{query} official audio",
        f"ytsearch1:{query}",
    ]

    for search_query in search_variants:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(search_query, download=True)
                if not info:
                    continue
                if "entries" in info:
                    entries = [e for e in (info["entries"] or []) if e]
                    if not entries:
                        continue
                    info = entries[0]

                filename = ydl.prepare_filename(info)
                base, _ = os.path.splitext(filename)
                mp3_filename = f"{base}.mp3"

                if os.path.exists(mp3_filename) and os.path.getsize(mp3_filename) > 0:
                    return mp3_filename
                if os.path.exists(filename) and os.path.getsize(filename) > 0:
                    return filename

                for f in os.listdir(output_dir):
                    full_p = os.path.join(output_dir, f)
                    if f.endswith(".mp3") and os.path.getsize(full_p) > 0:
                        return full_p

        except Exception as e:
            logger.warning(f"yt-dlp attempt failed for '{search_query}': {e}")

    return None


async def download_track(
    artist: str, title: str, target_duration: int = 0
) -> Optional[str]:
    """Асинхронна обгортка для завантаження треку з гарантованим очищенням при помилці."""
    loop = asyncio.get_running_loop()
    query = f"{artist} - {title}"
    temp_dir = tempfile.mkdtemp(prefix=TEMP_PREFIX)

    try:
        file_path = await loop.run_in_executor(
            _executor, _yt_dlp_download, query, temp_dir, target_duration
        )
        if not file_path or not os.path.exists(file_path):
            shutil.rmtree(temp_dir, ignore_errors=True)
            return None
        return file_path
    except Exception as e:
        logger.error(f"Failed to download track '{query}': {e}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None


def _convert_mp3_to_ogg_sync(mp3_bytes: bytes) -> Optional[bytes]:
    """Конвертує байти 30-сек прев'ю MP3 у формат OGG Opus для голосового повідомлення."""
    ffmpeg_bin = FFMPEG_PATH or "ffmpeg"
    try:
        proc = subprocess.run(
            [
                ffmpeg_bin,
                "-hide_banner",
                "-loglevel",
                "error",
                "-threads",
                "0",
                "-i",
                "pipe:0",
                "-c:a",
                "libopus",
                "-b:a",
                "64k",
                "-vbr",
                "on",
                "-application",
                "audio",
                "-f",
                "ogg",
                "pipe:1",
            ],
            input=mp3_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=12,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
        logger.warning(
            f"FFmpeg OGG conversion returned {proc.returncode}: "
            f"{proc.stderr.decode('utf-8', errors='ignore')}"
        )
        return None
    except Exception as e:
        logger.warning(f"Could not convert preview to OGG: {e}")
        return None


async def convert_preview_to_ogg(mp3_bytes: bytes) -> Optional[bytes]:
    """Асинхронно конвертує 30-секундне MP3 прев'ю в OGG Opus для Telegram Voice."""
    if not mp3_bytes:
        return None
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _convert_mp3_to_ogg_sync, mp3_bytes)


def cleanup_file(filepath: str):
    """Видаляє тимчасовий MP3 файл разом із його тимчасовою директорією."""
    if not filepath:
        return
    try:
        parent_dir = os.path.dirname(os.path.abspath(filepath))
        if os.path.basename(parent_dir).startswith(TEMP_PREFIX):
            shutil.rmtree(parent_dir, ignore_errors=True)
        elif os.path.exists(filepath):
            os.remove(filepath)
    except Exception as e:
        logger.warning(f"Error cleaning up file {filepath}: {e}")


def cleanup_temp_dir():
    """Очищує залишкові тимчасові папки tgbot_dl_* у системному Temp при старті бота."""
    base_temp = tempfile.gettempdir()
    removed = 0
    try:
        for name in os.listdir(base_temp):
            if name.startswith(TEMP_PREFIX):
                full_path = os.path.join(base_temp, name)
                if os.path.isdir(full_path):
                    shutil.rmtree(full_path, ignore_errors=True)
                    removed += 1
        if removed:
            logger.info(f"Cleaned up {removed} leftover temporary directories.")
    except Exception as e:
        logger.warning(f"Failed to scan temp directory during cleanup: {e}")
