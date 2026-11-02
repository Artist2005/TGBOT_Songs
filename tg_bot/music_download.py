"""
Модуль надійного та швидкого завантаження треків через yt-dlp (YouTube + SoundCloud fallback)
та прямої конвертації через FFmpeg (без потреби у ffprobe).
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
    """
    Створює фільтр для відсіювання стрімів, 30-секундних Go+ прев'ю та багатогодинних міксів.
    """
    max_duration = max(int(target_duration * 2.5), 900) if target_duration > 0 else 900
    min_duration = 45 if (target_duration == 0 or target_duration >= 50) else 15

    def _filter(info_dict, *, incomplete: bool = False):
        if info_dict.get("is_live"):
            return "Skipping live stream"
        duration = info_dict.get("duration")
        if duration:
            if duration > max_duration:
                return f"Track is too long ({duration}s > {max_duration}s)"
            if duration < min_duration:
                return f"Track is too short ({duration}s < {min_duration}s)"

        # Відсіюємо 30-секундні прев'ю SoundCloud Go+
        formats = info_dict.get("formats") or []
        if formats and all(
            str(f.get("format_id", "")).endswith("_preview") for f in formats
        ):
            return "Skipping preview-only track"
        return None

    return _filter


def _convert_to_mp3_ffmpeg(input_path: str, output_mp3_path: str) -> bool:
    """
    Конвертує завантажений аудіофайл (.m4a/.webm/.opus) у MP3 192 kbps напряму через FFmpeg
    (не потребує наявності ffprobe на сервері).
    """
    if input_path.lower().endswith(".mp3") and os.path.exists(input_path) and os.path.getsize(input_path) > 100_000:
        if input_path != output_mp3_path:
            shutil.move(input_path, output_mp3_path)
        return True

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
                "-y",
                "-i",
                input_path,
                "-vn",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "192k",
                output_mp3_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
        if proc.returncode == 0 and os.path.exists(output_mp3_path) and os.path.getsize(output_mp3_path) > 0:
            try:
                if os.path.exists(input_path) and input_path != output_mp3_path:
                    os.remove(input_path)
            except Exception:
                pass
            return True

        logger.warning(
            f"FFmpeg MP3 conversion failed (code {proc.returncode}): "
            f"{proc.stderr.decode('utf-8', errors='ignore')}"
        )
        return False
    except Exception as e:
        logger.warning(f"FFmpeg conversion exception: {e}")
        return False


def _yt_dlp_download(query: str, output_dir: str, target_duration: int = 0) -> Optional[str]:
    """
    Синхронна функція завантаження:
    1) Спочатку шукає на YouTube (з обходом блокування датацентрів GCP через мобільні клієнти).
    2) Якщо YouTube блокує IP віртуалки — миттєво завантажує повний трек із SoundCloud (scsearch5).
    3) Конвертує у MP3 напряму через FFmpeg (без ffprobe).
    """
    output_template = os.path.join(output_dir, "raw_%(id)s.%(ext)s")

    attempts = [
        (
            f"scsearch5:{query}",
            {},
        ),
        (
            f"ytsearch1:{query} official audio",
            {"youtube": {"player_client": ["ios", "android", "mweb"]}},
        ),
        (
            f"ytsearch1:{query}",
            {"youtube": {"player_client": ["android", "tv", "ios"]}},
        ),
    ]

    for search_query, extractor_args in attempts:
        # Очищуємо тимчасову папку перед спробою
        for old_f in os.listdir(output_dir):
            try:
                os.remove(os.path.join(output_dir, old_f))
            except Exception:
                pass

        ydl_opts = {
            "format": "http_mp3_128/hls_mp3_128/bestaudio[ext=mp3]/bestaudio[ext=m4a][abr<=192]/bestaudio[ext=webm][abr<=192]/bestaudio/best",
            "outtmpl": output_template,
            "concurrent_fragment_downloads": 8,
            "cachedir": YTDLP_CACHE_DIR,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "lazy_playlist": True,
            "max_downloads": 1,
            "max_filesize": 48 * 1024 * 1024,
            "match_filter": _make_match_filter(target_duration),
            "socket_timeout": 12,
            "retries": 2,
            "fragment_retries": 2,
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
            },
        }

        if extractor_args:
            ydl_opts["extractor_args"] = extractor_args
        if FFMPEG_PATH:
            ydl_opts["ffmpeg_location"] = FFMPEG_PATH

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    ydl.extract_info(search_query, download=True)
                except yt_dlp.utils.MaxDownloadsReached:
                    # Нормальне завершення при знаходженні першого валідного треку у scsearch5
                    pass

        except Exception as e:
            logger.warning(f"Download attempt failed for '{search_query}': {e}")

        # Перевіряємо, чи з'явився завантажений аудіофайл у папці
        downloaded_files = [
            os.path.join(output_dir, f)
            for f in os.listdir(output_dir)
            if not f.endswith(".part") and not f.endswith(".ytdl")
        ]
        downloaded_files = [
            p for p in downloaded_files if os.path.isfile(p) and os.path.getsize(p) > 50_000
        ]

        if downloaded_files:
            raw_path = downloaded_files[0]
            final_mp3 = os.path.join(output_dir, "track.mp3")
            if _convert_to_mp3_ffmpeg(raw_path, final_mp3):
                logger.info(f"Successfully downloaded & converted via '{search_query}'")
                return final_mp3
            # Якщо FFmpeg не зміг сконвертувати, повертаємо оригінальний аудіофайл (.m4a)
            logger.info(f"Returning raw audio file via '{search_query}': {raw_path}")
            return raw_path

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
