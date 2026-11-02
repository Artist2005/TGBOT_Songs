"""
Модуль надійного та швидкого завантаження треків через yt-dlp (SoundCloud + YouTube),
завантаження за прямими посиланнями (YouTube, Shorts, SoundCloud, TikTok, Reels, Spotify)
та створення аудіо-ефектів (Slowed + Reverb, Speed Up, Bass Boost) через FFmpeg.
"""

import os
import re
import json
import shutil
import logging
import tempfile
import asyncio
import subprocess
import urllib.request
import urllib.parse
from typing import Optional
from concurrent.futures import ThreadPoolExecutor
import yt_dlp

logger = logging.getLogger(__name__)

TEMP_PREFIX = "tgbot_dl_"
YTDLP_CACHE_DIR = os.path.join(tempfile.gettempdir(), "tgbot_ytdlp_cache")

_executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="music_dl")

# Регулярний вираз для підтримуваних музичних/відео платформ
SUPPORTED_URL_RE = re.compile(
    r"(https?://(?:www\.|m\.|music\.|vm\.|vt\.|on\.|open\.)?"
    r"(?:youtube\.com/(?:watch\?[^\s]+|shorts/[^\s]+)|youtu\.be/[^\s]+|"
    r"soundcloud\.com/[^\s]+|tiktok\.com/[^\s]+|"
    r"instagram\.com/(?:reel|reels|p)/[^\s]+|"
    r"spotify\.com/(?:intl-[a-z]{2}/)?track/[^\s]+|spotify\.link/[^\s]+))",
    re.IGNORECASE,
)

AUDIO_EFFECT_FILTERS = {
    "slowed": (
        "asetrate=44100*0.86,aresample=44100,aecho=0.8:0.85:55:0.32,volume=1.08",
        1.0 / 0.86,
    ),
    "speedup": (
        "asetrate=44100*1.18,aresample=44100",
        1.0 / 1.18,
    ),
    "bass": (
        "bass=g=9:f=105:w=0.6,dynaudnorm=p=0.9",
        1.0,
    ),
}


def extract_supported_url(text: str) -> Optional[str]:
    """Повертає перше підтримуване посилання з тексту повідомлення або None."""
    if not text:
        return None
    m = SUPPORTED_URL_RE.search(text.strip())
    return m.group(1) if m else None


def is_spotify_url(url: str) -> bool:
    """Перевіряє, чи є посилання треком Spotify."""
    return bool(url and ("spotify.com/" in url.lower() or "spotify.link/" in url.lower()))


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
    Якщо відома цільова тривалість треку (target_duration), відсіює 10-хвилинні DJ-сети та ремікси.
    """
    if target_duration > 0:
        max_duration = min(max(int(target_duration * 1.45), target_duration + 75), 900)
        min_duration = max(45, int(target_duration * 0.55)) if target_duration >= 60 else 15
    else:
        max_duration = 600
        min_duration = 45

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
    Конвертує завантажений аудіо/відеофайл у MP3 192 kbps напряму через FFmpeg
    (не потребує наявності ffprobe на сервері).
    """
    if (
        input_path.lower().endswith(".mp3")
        and os.path.exists(input_path)
        and os.path.getsize(input_path) > 80_000
    ):
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
    Синхронна функція завантаження за текстовим запитом:
    1) Спочатку шукає повний трек на SoundCloud (scsearch5) — стабільно працює з IP датацентрів GCP.
    2) Якщо не знайдено — шукає на YouTube через мобільні клієнти.
    3) Конвертує у MP3 напряму через FFmpeg.
    """
    output_template = os.path.join(output_dir, "raw_%(id)s.%(ext)s")

    attempts = [
        (
            f"scsearch8:{query}",
            {},
            target_duration,
        ),
    ]
    if target_duration > 0:
        attempts.append(
            (
                f"scsearch10:{query}",
                {},
                0,
            )
        )
    attempts.extend(
        [
            (
                f"ytsearch1:{query} official audio",
                {"youtube": {"player_client": ["ios", "android", "mweb"]}},
                target_duration,
            ),
            (
                f"ytsearch1:{query}",
                {"youtube": {"player_client": ["android", "tv", "ios"]}},
                0,
            ),
        ]
    )

    for search_query, extractor_args, dur_hint in attempts:
        for old_f in os.listdir(output_dir):
            try:
                os.remove(os.path.join(output_dir, old_f))
            except Exception:
                pass

        ydl_opts = {
            "format": "http_mp3_128/hls_mp3_128/bestaudio[ext=mp3]/bestaudio[ext=m4a][abr<=192]/bestaudio[ext=webm][abr<=192]/bestaudio/best",
            "outtmpl": output_template,
            "concurrent_fragment_downloads": 4,
            "cachedir": YTDLP_CACHE_DIR,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "lazy_playlist": True,
            "max_downloads": 1,
            "max_filesize": 48 * 1024 * 1024,
            "match_filter": _make_match_filter(dur_hint),
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
                    pass
        except Exception as e:
            logger.warning(f"Download attempt failed for '{search_query}': {e}")

        downloaded_files = [
            os.path.join(output_dir, f)
            for f in os.listdir(output_dir)
            if not f.endswith(".part") and not f.endswith(".ytdl")
        ]
        downloaded_files = [
            p for p in downloaded_files if os.path.isfile(p) and os.path.getsize(p) > 40_000
        ]

        if downloaded_files:
            raw_path = downloaded_files[0]
            final_mp3 = os.path.join(output_dir, "track.mp3")
            if _convert_to_mp3_ffmpeg(raw_path, final_mp3):
                logger.info(f"Successfully downloaded & converted via '{search_query}'")
                return final_mp3
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


def _fetch_youtube_oembed(url: str) -> Optional[tuple[str, str, str]]:
    """
    Отримує (artist, title, thumbnail_url) через публічний YouTube oEmbed API,
    який ніколи не блокується на IP датацентрів.
    """
    try:
        oembed_url = "https://www.youtube.com/oembed?" + urllib.parse.urlencode(
            {"url": url, "format": "json"}
        )
        req = urllib.request.Request(
            oembed_url,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            raw_title = (data.get("title") or "").strip()
            author = (data.get("author_name") or "").replace(" - Topic", "").strip()
            thumb = (data.get("thumbnail_url") or "").strip()
            if " - " in raw_title:
                parts = raw_title.split(" - ", 1)
                return parts[0].strip(), parts[1].strip(), thumb
            return author or "YouTube", raw_title or "Audio", thumb
    except Exception:
        return None


def _download_direct_url_sync(url: str, output_dir: str) -> Optional[dict]:
    """
    Синхронно завантажує аудіо з прямого посилання (YouTube, Shorts, TikTok, SoundCloud, Reels).
    Якщо YouTube блокує пряме скачування з IP датацентру — автоматично бере назву через oEmbed
    і завантажує трек через пошуковий пайплайн.
    """
    output_template = os.path.join(output_dir, "url_%(id)s.%(ext)s")
    ydl_opts = {
        "format": "http_mp3_128/hls_mp3_128/bestaudio[ext=mp3]/bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": output_template,
        "concurrent_fragment_downloads": 8,
        "cachedir": YTDLP_CACHE_DIR,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "max_filesize": 48 * 1024 * 1024,
        "socket_timeout": 15,
        "retries": 2,
        "extractor_args": {"youtube": {"player_client": ["ios", "android", "tv", "mweb"]}},
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        },
    }
    if FFMPEG_PATH:
        ydl_opts["ffmpeg_location"] = FFMPEG_PATH

    info_meta = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_meta = ydl.extract_info(url, download=True)
    except Exception as e:
        logger.warning(f"Direct URL download error for {url}: {e}")

    if isinstance(info_meta, dict) and "entries" in info_meta and info_meta["entries"]:
        info_meta = info_meta["entries"][0]

    downloaded_files = [
        os.path.join(output_dir, f)
        for f in os.listdir(output_dir)
        if not f.endswith(".part") and not f.endswith(".ytdl")
    ]
    downloaded_files = [
        p for p in downloaded_files if os.path.isfile(p) and os.path.getsize(p) > 25_000
    ]

    if downloaded_files:
        raw_path = downloaded_files[0]
        final_mp3 = os.path.join(output_dir, "direct.mp3")
        mp3_path = final_mp3 if _convert_to_mp3_ffmpeg(raw_path, final_mp3) else raw_path

        raw_title = (info_meta.get("track") or info_meta.get("title") or "Audio Track") if isinstance(info_meta, dict) else "Audio Track"
        raw_artist = (
            info_meta.get("artist")
            or info_meta.get("uploader")
            or info_meta.get("creator")
            or info_meta.get("channel")
            or "Unknown Artist"
        ) if isinstance(info_meta, dict) else "Unknown Artist"
        raw_artist = str(raw_artist).replace(" - Topic", "").strip()
        duration = int(info_meta.get("duration") or 0) if isinstance(info_meta, dict) else 0
        thumb = (info_meta.get("thumbnail") or "") if isinstance(info_meta, dict) else ""

        if " - " in str(raw_title) and not (isinstance(info_meta, dict) and info_meta.get("track")):
            parts = str(raw_title).split(" - ", 1)
            raw_artist, raw_title = parts[0].strip(), parts[1].strip()

        raw_title = re.sub(
            r"\s*[\(\[][^)\]]*(?:official\s*(?:music\s*)?video|official\s*audio|lyric\s*video)[^)\]]*[\)\]]",
            "",
            str(raw_title),
            flags=re.IGNORECASE,
        ).strip() or str(raw_title)

        return {
            "file_path": mp3_path,
            "title": str(raw_title)[:100],
            "artist": str(raw_artist)[:80],
            "duration": duration,
            "thumbnail_url": thumb,
        }

    # Фолбек для YouTube/youtu.be на випадок блокування IP датацентру
    if "youtube.com" in url.lower() or "youtu.be" in url.lower():
        yt_meta = _fetch_youtube_oembed(url)
        if yt_meta:
            artist, title, thumb = yt_meta
            fallback_path = _yt_dlp_download(f"{artist} - {title}", output_dir, target_duration=0)
            if fallback_path and os.path.exists(fallback_path):
                return {
                    "file_path": fallback_path,
                    "title": title[:100],
                    "artist": artist[:80],
                    "duration": 0,
                    "thumbnail_url": thumb,
                }

    return None


async def download_from_url(url: str) -> Optional[dict]:
    """
    Асинхронно завантажує аудіо за прямим посиланням (YouTube, TikTok, SoundCloud, Reels).
    Повертає словник з file_path, title, artist, duration, thumbnail_url або None.
    """
    loop = asyncio.get_running_loop()
    temp_dir = tempfile.mkdtemp(prefix=TEMP_PREFIX)
    try:
        res = await loop.run_in_executor(_executor, _download_direct_url_sync, url, temp_dir)
        if not res or not res.get("file_path") or not os.path.exists(res["file_path"]):
            shutil.rmtree(temp_dir, ignore_errors=True)
            return None
        return res
    except Exception as e:
        logger.error(f"Failed direct URL download '{url}': {e}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None


def _apply_audio_effect_sync(
    input_path: str, effect: str, base_duration: int = 0
) -> Optional[tuple[str, int]]:
    """
    Синхронно застосовує FFmpeg-фільтр (slowed, speedup, bass) до MP3 файлу.
    Повертає (output_mp3_path, new_duration).
    """
    if effect not in AUDIO_EFFECT_FILTERS or not os.path.exists(input_path):
        return None

    afilter, duration_mult = AUDIO_EFFECT_FILTERS[effect]
    temp_dir = tempfile.mkdtemp(prefix=TEMP_PREFIX)
    output_path = os.path.join(temp_dir, f"track_{effect}.mp3")
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
                "-af",
                afilter,
                "-c:a",
                "libmp3lame",
                "-b:a",
                "192k",
                output_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
        if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            new_dur = int(round(base_duration * duration_mult)) if base_duration > 0 else 0
            return output_path, new_dur

        logger.warning(
            f"FFmpeg effect '{effect}' failed ({proc.returncode}): "
            f"{proc.stderr.decode('utf-8', errors='ignore')}"
        )
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None
    except Exception as e:
        logger.warning(f"FFmpeg effect '{effect}' exception: {e}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None


async def apply_audio_effect(
    input_path: str, effect: str, base_duration: int = 0
) -> Optional[tuple[str, int]]:
    """Асинхронно створює версію треку з ефектом (slowed, speedup, bass)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _executor, _apply_audio_effect_sync, input_path, effect, base_duration
    )


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
