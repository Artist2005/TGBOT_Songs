"""
Telegram Inline Music Bot (UA / EN).
Швидкий пошук та завантаження MP3 у будь-якому чаті (Inline + Direct Chat):
- Чарти при порожньому @bot та /top
- Завантаження за посиланнями (YouTube, Shorts, SoundCloud, TikTok, Reels, Spotify)
- Вибране / власний плейлист (❤️ + /favorites + @bot fav)
- Тексти пісень (📜 Текст у згортаному блоці)
- Аудіо-ефекти в 1 клік (🌙 Slowed + Reverb, ⚡ Speed Up, 🔊 Bass Boost)
- Схожі треки (📻 Схожі)
"""

import os
import re
import io
import html
import zlib
import asyncio
import logging
import tempfile
from uuid import uuid4
from typing import Optional
from dotenv import load_dotenv

from telegram import (
    Update,
    User,
    BotCommand,
    InlineQueryResultArticle,
    InlineQueryResultAudio,
    InlineQueryResultCachedAudio,
    InlineQueryResultsButton,
    InputTextMessageContent,
    InputMediaAudio,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    InlineQueryHandler,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from music_search import (
    search_tracks,
    get_chart_tracks,
    get_track_by_id,
    get_similar_tracks,
    get_track_lyrics,
    resolve_spotify_query,
    download_url_bytes,
    format_duration,
    warmup_session,
    close_session,
)
from music_download import (
    download_track,
    download_from_url,
    extract_supported_url,
    is_spotify_url,
    apply_audio_effect,
    convert_preview_to_ogg,
    cleanup_file,
    cleanup_temp_dir,
)
from cache_db import CacheDB

# === Configuration ===
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
STORAGE_CHAT_ID = os.getenv("STORAGE_CHAT_ID")

# Доступ до адмін-панелі (/admin, /stats) має ВИКЛЮЧНО власник бота (Telegram ID: 1001308094)
# та додаткові ID, якщо вони прописані через кому в ADMIN_IDS у .env
ADMIN_IDS: set[int] = {1001308094}
for _raw_id in (os.getenv("ADMIN_IDS") or "").split(","):
    _raw_id = _raw_id.strip()
    if _raw_id.isdigit():
        ADMIN_IDS.add(int(_raw_id))


def _is_admin(user_id: Optional[int]) -> bool:
    """Перевіряє, чи є користувач власником/адміністратором бота."""
    return bool(user_id and int(user_id) in ADMIN_IDS)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

cache = CacheDB()
_download_locks: dict[str, asyncio.Lock] = {}

SILENT_FALLBACK_URL = (
    "https://raw.githubusercontent.com/anars/blank-audio/master/1-second-of-silence.mp3"
)

EFFECT_LABELS = {
    "uk": {
        "slowed": "🌙 Slowed + Reverb",
        "speedup": "⚡ Speed Up",
        "bass": "🔊 Bass Boost",
    },
    "en": {
        "slowed": "🌙 Slowed + Reverb",
        "speedup": "⚡ Speed Up",
        "bass": "🔊 Bass Boost",
    },
}

# === Двомовні тексти (Українська / English) ===
TEXTS = {
    "uk": {
        "welcome": (
            "🎵 <b>Вітаємо у Music Explorer Bot!</b>\n\n"
            "Шукайте, слухайте 30-секундні уривки та завантажуйте повні MP3 у будь-якому чаті!\n\n"
            "✨ <b>Можливості:</b>\n"
            "• <b>Інлайн у будь-якому чаті:</b> введіть <code>@{bot_name} назва або слова з пісні</code> (або просто <code>@{bot_name} </code> для Топ-чарту)\n"
            "• <b>Пошук за назвою чи словами:</b> напишіть назву, виконавця або просто рядок із пісні, який пам'ятаєте\n"
            "• <b>Посилання:</b> надішліть лінк із <b>YouTube, Shorts, TikTok, SoundCloud, Reels</b> або <b>Spotify</b> — бот миттєво витягне MP3\n"
            "• <b>Під кожним треком:</b> ❤️ Вибране • 📜 Текст • 🎛 Ефекти (Slowed / Speed Up / Bass) • 📻 Схожі\n\n"
            "📌 <b>Команди:</b> /top — Хіти • /favorites — Вибране • /lang — Мова"
        ),
        "lang_prompt": "🌐 <b>Оберіть мову інтерфейсу / Choose language:</b>",
        "lang_changed": "✅ <b>Мову змінено на Українську!</b>",
        "searching": "🔍 Шукаю: <b>{query}</b>...",
        "no_results_chat": "😔 Нічого не знайдено за запитом: <b>{query}</b>",
        "search_results_header": (
            "🎧 <b>Результати для:</b> <code>{query}</code>\n"
            "<i>Оберіть трек (⚡ — миттєво з кешу, 🎤 — 30 сек прев'ю):</i>"
        ),
        "top_header": (
            "🔥 <b>Топ-10 світових хітів зараз:</b>\n"
            "<i>Натисніть на трек, щоб прослухати або завантажити:</i>"
        ),
        "fav_header": (
            "❤️ <b>Ваш плейлист «Вибране» ({count}):</b>\n"
            "<i>Також доступний у будь-якому чаті через</i> <code>@{bot_name} fav</code>"
        ),
        "fav_empty": (
            "🤍 <b>У вашому «Вибраному» поки порожньо.</b>\n"
            "Натисніть <b>🤍</b> під будь-яким завантаженим треком, щоб зберегти його сюди!"
        ),
        "fav_added_toast": "❤️ Додано у Вибране (/favorites)",
        "fav_removed_toast": "💔 Видалено з Вибраного",
        "fav_inline_btn": "❤️ Моє вибране ({count})",
        "similar_header": "📻 <b>Схожі треки та хіти {artist}:</b>",
        "similar_empty": "😔 Не знайдено схожих треків.",
        "lyrics_header": "📜 <b>{artist} — {title}</b>",
        "lyrics_not_found": "😔 Текст для <b>{artist} — {title}</b> не знайдено.",
        "lyrics_loading_toast": "📜 Шукаю текст пісні...",
        "effect_loading_toast": "🎛 Створюю версію {effect}...",
        "effect_processing": "🎛 Обробляю <b>{effect}</b>:\n🎵 <b>{artist} — {title}</b>...",
        "effect_failed": "❌ Не вдалося обробити аудіо-ефект.",
        "url_downloading": "🔗 Завантажую аудіо за посиланням...",
        "url_failed": "❌ Не вдалося завантажити аудіо за цим посиланням.",
        "preview_toast": "🎤 Надсилаю 30-секундне прев'ю...",
        "no_preview_available": (
            "🎵 <b>{artist} — {title}</b>\n"
            "<i>Прев'ю недоступне. Натисніть нижче, щоб завантажити повний MP3:</i>"
        ),
        "no_results_inline_title": "❌ Нічого не знайдено",
        "no_results_inline_desc": "Не знайдено треків за запитом '{query}'",
        "no_results_inline_msg": "🔍 <b>Нічого не знайдено за запитом:</b> <code>{query}</code>",
        "downloading_chat": "⏳ Завантажую повний MP3:\n🎵 <b>{artist} — {title}</b>",
        "download_failed": "❌ <b>Не вдалося завантажити трек:</b>\n🎵 {artist} — {title}",
        "download_toast": "⏳ Завантажую повний MP3 (192 kbps)...",
        "downloading_inline": (
            "⏳ <b>Завантажую повний MP3...</b>\n"
            "🎵 <b>{artist} — {title}</b>"
        ),
        "deep_link_prompt": (
            "⚠️ <b>Для першого завантаження відкрийте бота один раз:</b>\n"
            "🎵 <b>{artist} — {title}</b>\n\n"
            "👇 Натисніть кнопку нижче — трек надішлеться автоматично:"
        ),
        "ready_fallback": (
            "✅ <b>Повний MP3 готовий!</b>\n"
            "📩 Трек надіслано вам у @{bot_name} (або натисніть кнопку нижче, щоб забрати його):"
        ),
        "btn_download": "📥 Скачати MP3",
        "btn_original": "💿 Оригінал",
        "btn_open_download": "📥 Відкрити бота і забрати MP3",
        "btn_top": "🔥 Топ-чарт",
        "btn_favorites": "❤️ Вибране",
        "btn_fav_add": "🤍 Вибране",
        "btn_fav_saved": "❤️ Вибране",
        "btn_lyrics": "📜 Текст",
        "btn_hide_lyrics": "🔙 Сховати текст",
        "btn_effects": "🎛 Ефекти",
        "btn_similar": "📻 Схожі",
        "btn_back": "🔙 Назад",
        "btn_close": "✖️ Закрити",
        "album_label": "Альбом",
        "duration_label": "Тривалість",
        "quality_label": "Якість",
        "invalid_data": "❌ Некоректні дані треку",
        "cmd_start": "Головне меню та інструкція",
        "cmd_top": "Топ-10 світових хітів",
        "cmd_fav": "Ваші збережені треки (Вибране)",
        "cmd_lang": "Змінити мову (🇺🇦 / 🇬🇧)",
    },
    "en": {
        "welcome": (
            "🎵 <b>Welcome to Music Explorer Bot!</b>\n\n"
            "Search, listen to 30-second previews, and download full MP3 tracks in any chat!\n\n"
            "✨ <b>Features:</b>\n"
            "• <b>Inline in any chat:</b> type <code>@{bot_name} song name or lyrics</code> (or just <code>@{bot_name} </code> for Top Chart)\n"
            "• <b>Search by title or lyrics:</b> send a song title, artist name, or any words you remember from the lyrics\n"
            "• <b>Direct Links:</b> send a link from <b>YouTube, Shorts, TikTok, SoundCloud, Reels</b> or <b>Spotify</b> to get MP3\n"
            "• <b>Under every track:</b> ❤️ Favorites • 📜 Lyrics • 🎛 Effects (Slowed / Speed Up / Bass) • 📻 Similar\n\n"
            "📌 <b>Commands:</b> /top — Top Chart • /favorites — Favorites • /lang — Language"
        ),
        "lang_prompt": "🌐 <b>Choose interface language / Оберіть мову:</b>",
        "lang_changed": "✅ <b>Language set to English!</b>",
        "searching": "🔍 Searching: <b>{query}</b>...",
        "no_results_chat": "😔 No results found for: <b>{query}</b>",
        "search_results_header": (
            "🎧 <b>Results for:</b> <code>{query}</code>\n"
            "<i>Select a track (⚡ — instant from cache, 🎤 — 30s preview):</i>"
        ),
        "top_header": (
            "🔥 <b>Top 10 Global Hits Right Now:</b>\n"
            "<i>Tap a track to preview or download:</i>"
        ),
        "fav_header": (
            "❤️ <b>Your Favorites Playlist ({count}):</b>\n"
            "<i>Also available in any chat via</i> <code>@{bot_name} fav</code>"
        ),
        "fav_empty": (
            "🤍 <b>Your Favorites playlist is empty.</b>\n"
            "Tap <b>🤍</b> under any downloaded track to save it here!"
        ),
        "fav_added_toast": "❤️ Added to Favorites (/favorites)",
        "fav_removed_toast": "💔 Removed from Favorites",
        "fav_inline_btn": "❤️ My Favorites ({count})",
        "similar_header": "📻 <b>Similar tracks & hits by {artist}:</b>",
        "similar_empty": "😔 No similar tracks found.",
        "lyrics_header": "📜 <b>{artist} — {title}</b>",
        "lyrics_not_found": "😔 Lyrics for <b>{artist} — {title}</b> were not found.",
        "lyrics_loading_toast": "📜 Fetching lyrics...",
        "effect_loading_toast": "🎛 Creating {effect} version...",
        "effect_processing": "🎛 Processing <b>{effect}</b>:\n🎵 <b>{artist} — {title}</b>...",
        "effect_failed": "❌ Could not process audio effect.",
        "url_downloading": "🔗 Downloading audio from link...",
        "url_failed": "❌ Could not download audio from this link.",
        "preview_toast": "🎤 Sending 30s voice preview...",
        "no_preview_available": (
            "🎵 <b>{artist} — {title}</b>\n"
            "<i>Preview unavailable. Tap below to download the full MP3:</i>"
        ),
        "no_results_inline_title": "❌ No Results Found",
        "no_results_inline_desc": "No music matches found for '{query}'",
        "no_results_inline_msg": "🔍 <b>No results found for:</b> <code>{query}</code>",
        "downloading_chat": "⏳ Downloading full MP3:\n🎵 <b>{artist} — {title}</b>",
        "download_failed": "❌ <b>Could not download track:</b>\n🎵 {artist} — {title}",
        "download_toast": "⏳ Downloading full MP3 (192 kbps)...",
        "downloading_inline": (
            "⏳ <b>Downloading full MP3...</b>\n"
            "🎵 <b>{artist} — {title}</b>"
        ),
        "deep_link_prompt": (
            "⚠️ <b>Please open the bot once for your first download:</b>\n"
            "🎵 <b>{artist} — {title}</b>\n\n"
            "👇 Tap the button below — your track will be sent automatically:"
        ),
        "ready_fallback": (
            "✅ <b>Full MP3 is ready!</b>\n"
            "📩 Sent to your DM in @{bot_name} (or tap below to get it):"
        ),
        "btn_download": "📥 Get MP3",
        "btn_original": "💿 Original",
        "btn_open_download": "📥 Open Bot & Get MP3",
        "btn_top": "🔥 Top Chart",
        "btn_favorites": "❤️ Favorites",
        "btn_fav_add": "🤍 Favorite",
        "btn_fav_saved": "❤️ Favorite",
        "btn_lyrics": "📜 Lyrics",
        "btn_hide_lyrics": "🔙 Hide Lyrics",
        "btn_effects": "🎛 Effects",
        "btn_similar": "📻 Similar",
        "btn_back": "🔙 Back",
        "btn_close": "✖️ Close",
        "album_label": "Album",
        "duration_label": "Duration",
        "quality_label": "Quality",
        "invalid_data": "❌ Invalid track data",
        "cmd_start": "Main menu & help",
        "cmd_top": "Top 10 global hits",
        "cmd_fav": "Your saved Favorite tracks",
        "cmd_lang": "Switch language (🇺🇦 / 🇬🇧)",
    },
}


def _get_lang(user: Optional[User]) -> str:
    """Визначає мову користувача з RAM-кешу або за Telegram language_code."""
    if not user:
        return "uk"
    saved = cache.get_user_lang(user.id)
    if saved in ("uk", "en"):
        return saved
    code = (user.language_code or "").lower()
    if code.startswith("en"):
        return "en"
    return "uk"


def _build_welcome_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Компактне меню швидких дій та перемикання мови для /start."""
    t = TEXTS.get(lang, TEXTS["uk"])
    uk_label = "🇺🇦 Українська ✓" if lang == "uk" else "🇺🇦 Українська"
    en_label = "🇬🇧 English ✓" if lang == "en" else "🇬🇧 English"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(t["btn_top"], callback_data="nav:top"),
                InlineKeyboardButton(t["btn_favorites"], callback_data="nav:fav"),
            ],
            [
                InlineKeyboardButton(uk_label, callback_data="lang:uk"),
                InlineKeyboardButton(en_label, callback_data="lang:en"),
            ],
        ]
    )


def _build_lang_keyboard(current_lang: str) -> InlineKeyboardMarkup:
    """Клавіатура перемикання мови."""
    uk_label = "🇺🇦 Українська ✓" if current_lang == "uk" else "🇺🇦 Українська"
    en_label = "🇬🇧 English ✓" if current_lang == "en" else "🇬🇧 English"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(uk_label, callback_data="lang:uk"),
                InlineKeyboardButton(en_label, callback_data="lang:en"),
            ]
        ]
    )


def _get_track_lock(key: str | int) -> asyncio.Lock:
    """Повертає asyncio.Lock для конкретного ключа, уникаючи подвійного завантаження."""
    k = str(key)
    if k not in _download_locks:
        if len(_download_locks) > 1000:
            _download_locks.clear()
        _download_locks[k] = asyncio.Lock()
    return _download_locks[k]


async def _resolve_track_meta(
    deezer_id: int, context: ContextTypes.DEFAULT_TYPE
) -> dict:
    """
    Надійно знаходить метадані треку з:
    1) RAM bot_data
    2) SQLite / RAM кешу (cache_db)
    3) Прямого запиту до Deezer API (якщо deezer_id > 0)
    """
    mem_track = context.application.bot_data.get(str(deezer_id))
    if mem_track and mem_track.get("title") and mem_track["title"] != "Unknown Title":
        return mem_track

    db_track = cache.get_track_meta(deezer_id)
    if db_track and db_track.get("title") and db_track["title"] != "Unknown Title":
        context.application.bot_data[str(deezer_id)] = db_track
        return db_track

    if deezer_id > 0:
        api_track = await get_track_by_id(deezer_id)
        if api_track:
            cache.save_track_meta(api_track)
            context.application.bot_data[str(deezer_id)] = api_track
            return api_track

    return {
        "id": deezer_id,
        "title": "Unknown Title",
        "artist": "Unknown Artist",
        "album": "",
        "duration": 0,
        "cover_url": "",
        "preview_url": "",
    }


def _stabilize_caption_width(lines: list[str], target_len: int = 34) -> str:
    """
    Додає невидимий фіксатор ширини (\u2800) до останнього рядка підпису,
    щоб бульбашка повідомлення і кнопки під нею в Telegram завжди мали однакову ширину і не «плигали».
    """
    if not lines:
        return "\u2800" * target_len
    plain_last = re.sub(r"<[^>]+>", "", lines[-1])
    pad = max(0, target_len - len(plain_last))
    if pad > 0:
        lines[-1] = lines[-1] + ("\u2800" * pad)
    return "\n".join(lines)


def _build_full_caption(
    artist: str,
    title: str,
    album: str = "",
    duration: int = 0,
    lang: str = "uk",
    effect_label: str = "",
) -> str:
    """Формує лаконічний та безпечний HTML-підпис для повного MP3 треку зі стабільною шириною."""
    t = TEXTS.get(lang, TEXTS["uk"])
    safe_artist = html.escape(artist)
    safe_title = html.escape(title)
    suffix = f" <i>({html.escape(effect_label)})</i>" if effect_label else ""
    lines = [f"🎵 <b>{safe_artist} — {safe_title}</b>{suffix}"]
    if album and not effect_label:
        lines.append(f"💿 <b>{t['album_label']}:</b> {html.escape(album)}")
    if duration > 0:
        lines.append(f"⏱ <b>{t['duration_label']}:</b> {format_duration(duration)}")
    lines.append(f"⚡ <b>{t['quality_label']}:</b> 192 kbps MP3")
    return _stabilize_caption_width(lines)


def _build_preview_caption(
    artist: str, title: str, duration: int = 0, lang: str = "uk"
) -> str:
    """Формує безпечний HTML-підпис для 30-секундного прев'ю зі стабільною шириною."""
    safe_artist = html.escape(artist)
    safe_title = html.escape(title)
    duration_str = format_duration(duration) if duration > 0 else "—"
    if lang == "en":
        lines = [
            f"🎧 <b>{safe_artist} — {safe_title}</b>",
            f"🎤 <b>Preview:</b> 30s • <b>Track:</b> {duration_str}",
        ]
    else:
        lines = [
            f"🎧 <b>{safe_artist} — {safe_title}</b>",
            f"🎤 <b>Прев'ю:</b> 30 сек • <b>Трек:</b> {duration_str}",
        ]
    return _stabilize_caption_width(lines)


def _build_download_keyboard(
    deezer_id: int, duration: int = 0, lang: str = "uk"
) -> InlineKeyboardMarkup:
    """Клавіатура 2×2 для прев'ю (завантаження + основні дії однакового розміру)."""
    return _build_track_actions_keyboard(
        deezer_id,
        user_id=None,
        lang=lang,
        is_inline=False,
        include_download=True,
        duration=duration,
    )


def _build_track_actions_keyboard(
    deezer_id: int,
    user_id: Optional[int] = None,
    lang: str = "uk",
    is_inline: bool = False,
    artist: str = "",
    include_download: bool = False,
    show_original_btn: bool = False,
    duration: int = 0,
) -> InlineKeyboardMarkup:
    """
    Суворо симетрична дворядкова панель дій (2×2 = 4 однакові кнопки 50%/50%):
    - Для повного MP3:
        [ 🤍 Вибране / ❤️ Вибране ]  [ 📜 Текст ]
        [ 🎛 Ефекти ]                [ 📻 Схожі ]
    - Для 30-сек прев'ю (include_download=True):
        [ 📥 Скачати MP3 ]           [ 🤍 Вибране / ❤️ Вибране ]
        [ 📜 Текст ]                 [ 🎛 Ефекти ]
    - Для треку з ефектом в інлайн-режимі (show_original_btn=True):
        [ 💿 Оригінал ]              [ 🤍 Вибране / ❤️ Вибране ]
        [ 📜 Текст ]                 [ 🎛 Ефекти ]
    Усі кнопки мають 1 іконку + короткий підпис однакової довжини, тому ніколи не плигають.
    """
    t = TEXTS.get(lang, TEXTS["uk"])
    is_fav = cache.is_favorite(user_id, deezer_id) if user_id else False
    fav_label = t["btn_fav_saved"] if is_fav else t["btn_fav_add"]

    suffix = "p" if include_download else ""
    fav_cb = f"fav{suffix}:{deezer_id}"
    lyr_cb = f"lyr{suffix}:{deezer_id}"
    fxm_cb = f"fxm{suffix}:{deezer_id}"

    if include_download:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(t["btn_download"], callback_data=f"dl:{deezer_id}"),
                    InlineKeyboardButton(fav_label, callback_data=fav_cb),
                ],
                [
                    InlineKeyboardButton(t["btn_lyrics"], callback_data=lyr_cb),
                    InlineKeyboardButton(t["btn_effects"], callback_data=fxm_cb),
                ],
            ]
        )

    if show_original_btn:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(t["btn_original"], callback_data=f"dl:{deezer_id}"),
                    InlineKeyboardButton(fav_label, callback_data=fav_cb),
                ],
                [
                    InlineKeyboardButton(t["btn_lyrics"], callback_data=lyr_cb),
                    InlineKeyboardButton(t["btn_effects"], callback_data=fxm_cb),
                ],
            ]
        )

    if is_inline:
        similar_query = artist if artist and artist != "Unknown Artist" else ""
        similar_btn = InlineKeyboardButton(
            t["btn_similar"],
            switch_inline_query_current_chat=similar_query,
        )
    else:
        similar_btn = InlineKeyboardButton(
            t["btn_similar"],
            callback_data=f"sim:{deezer_id}",
        )

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(fav_label, callback_data=fav_cb),
                InlineKeyboardButton(t["btn_lyrics"], callback_data=lyr_cb),
            ],
            [
                InlineKeyboardButton(t["btn_effects"], callback_data=fxm_cb),
                similar_btn,
            ],
        ]
    )


def _build_effects_submenu_keyboard(
    deezer_id: int, lang: str = "uk", is_preview: bool = False
) -> InlineKeyboardMarkup:
    """Суворо симетричне дворядкове підменю вибору аудіо-ефектів (2×2 = 4 однакові кнопки)."""
    t = TEXTS.get(lang, TEXTS["uk"])
    back_cb = f"fxbp:{deezer_id}" if is_preview else f"fxb:{deezer_id}"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🌙 Slowed", callback_data=f"fx:slowed:{deezer_id}"),
                InlineKeyboardButton("⚡ Speed Up", callback_data=f"fx:speedup:{deezer_id}"),
            ],
            [
                InlineKeyboardButton("🔊 Bass", callback_data=f"fx:bass:{deezer_id}"),
                InlineKeyboardButton(t["btn_back"], callback_data=back_cb),
            ],
        ]
    )


def _build_track_list_buttons(tracks: list[dict], context: ContextTypes.DEFAULT_TYPE) -> list[list[InlineKeyboardButton]]:
    """Формує охайний список кнопок треків для результатів пошуку, чарту, вибраного або схожих."""
    buttons = []
    for idx, track in enumerate(tracks, start=1):
        did = int(track["id"])
        context.application.bot_data[str(did)] = track
        dur_str = format_duration(track.get("duration", 0))
        is_cached = cache.get_file_id(did) is not None
        prefix = "⚡" if is_cached else "🎤"
        label = f"{prefix} {track['artist']} — {track['title']} ({dur_str})"
        if len(label) > 55:
            label = label[:52] + "..."
        # Якщо трек уже в кеші або має від'ємний ID (з посилання), клік одразу відкриває/надсилає MP3, інакше прев'ю
        action = "dl" if (is_cached or did < 0) else "pv"
        buttons.append(
            [InlineKeyboardButton(label, callback_data=f"{action}:{did}")]
        )
    return buttons


# === Command /start ===
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вітальне повідомлення з вибором мови або обробка deep-link (?start=track_<id> / favorites)."""
    if not update.message:
        return

    lang = _get_lang(update.effective_user)
    if update.effective_user and cache.get_user_lang(update.effective_user.id) is None:
        cache.set_user_lang(update.effective_user.id, lang)

    if context.args and len(context.args) > 0:
        arg = context.args[0]
        if arg == "favorites":
            await favorites_command(update, context)
            return
        if arg.startswith("track_"):
            deezer_id_str = arg.replace("track_", "").strip()
            if deezer_id_str.lstrip("-").isdigit():
                await _send_or_download_to_chat(
                    chat_id=update.effective_chat.id,
                    deezer_id=int(deezer_id_str),
                    context=context,
                    lang=lang,
                    user_id=update.effective_user.id if update.effective_user else None,
                )
                return

    bot_name = html.escape(context.bot.username or "bot")
    await update.message.reply_text(
        TEXTS[lang]["welcome"].format(bot_name=bot_name),
        parse_mode=ParseMode.HTML,
        reply_markup=_build_welcome_keyboard(lang),
    )


# === Command /top ===
async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показує Топ-10 світових хітів Deezer."""
    msg = update.effective_message
    if not msg:
        return
    lang = _get_lang(update.effective_user)
    t = TEXTS[lang]

    tracks = await get_chart_tracks(limit=10)
    if not tracks:
        await msg.reply_text(t["similar_empty"], parse_mode=ParseMode.HTML)
        return

    buttons = _build_track_list_buttons(tracks, context)
    asyncio.get_running_loop().run_in_executor(None, cache.save_tracks_meta_batch, tracks)

    await msg.reply_text(
        t["top_header"],
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# === Command /favorites (/fav) ===
async def favorites_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показує збережені улюблені треки користувача."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    lang = _get_lang(user)
    t = TEXTS[lang]
    fav_tracks = cache.get_user_favorites(user.id, limit=20)

    if not fav_tracks:
        await msg.reply_text(t["fav_empty"], parse_mode=ParseMode.HTML)
        return

    buttons = _build_track_list_buttons(fav_tracks, context)
    bot_name = html.escape(context.bot.username or "bot")
    await msg.reply_text(
        t["fav_header"].format(count=len(fav_tracks), bot_name=bot_name),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# === Command /lang ===
async def lang_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда перемикання мови (Українська / English)."""
    if not update.message:
        return
    lang = _get_lang(update.effective_user)
    await update.message.reply_text(
        TEXTS[lang]["lang_prompt"],
        parse_mode=ParseMode.HTML,
        reply_markup=_build_lang_keyboard(lang),
    )


# === Callback: Навігація з головного меню (nav:top / nav:fav) та закриття (delmsg) ===
async def nav_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробляє кнопки Топ-чарту та Вибраного з вітального повідомлення."""
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    if query.data == "nav:top":
        await top_command(update, context)
    elif query.data == "nav:fav":
        await favorites_command(update, context)
    elif query.data == "delmsg" and query.message:
        try:
            await query.message.delete()
        except Exception:
            pass


# === Callback: Перемикання мови (lang:uk / lang:en) ===
async def lang_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробляє натискання кнопок вибору мови."""
    query = update.callback_query
    if not query or not query.data:
        return

    lang = "en" if query.data.endswith(":en") else "uk"
    if query.from_user:
        cache.set_user_lang(query.from_user.id, lang)

    await query.answer(
        "🇬🇧 Language set to English" if lang == "en" else "🇺🇦 Мову змінено на Українську"
    )

    if query.message:
        bot_name = html.escape(context.bot.username or "bot")
        try:
            await query.message.edit_text(
                f"{TEXTS[lang]['lang_changed']}\n\n"
                f"{TEXTS[lang]['welcome'].format(bot_name=bot_name)}",
                parse_mode=ParseMode.HTML,
                reply_markup=_build_welcome_keyboard(lang),
            )
        except Exception:
            pass


# === Обробка прямих посилань (YouTube, Shorts, TikTok, SoundCloud, Reels, Spotify) ===
async def _handle_direct_url(
    url: str, update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
):
    """Завантажує аудіо за посиланням та надсилає MP3 з панеллю дій."""
    msg = update.message
    if not msg:
        return
    t = TEXTS[lang]
    user_id = update.effective_user.id if update.effective_user else msg.chat_id

    # 1. Якщо це посилання на трек Spotify — знаходимо трек на Deezer або за назвою
    if is_spotify_url(url):
        status_msg = await msg.reply_text(t["url_downloading"], parse_mode=ParseMode.HTML)
        sp_query = await resolve_spotify_query(url)
        if sp_query:
            tracks = await search_tracks(sp_query, limit=1)
            if tracks:
                track = tracks[0]
                cache.save_track_meta(track)
                context.application.bot_data[str(track["id"])] = track
                try:
                    await status_msg.delete()
                except Exception:
                    pass
                await _send_or_download_to_chat(
                    chat_id=msg.chat_id,
                    deezer_id=int(track["id"]),
                    context=context,
                    lang=lang,
                    user_id=user_id,
                )
                return
        try:
            await status_msg.edit_text(t["url_failed"], parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return

    # 2. Детермінований від'ємний ID для кешування прямих посилань
    synthetic_id = -int((zlib.crc32(url.strip().encode("utf-8")) & 0x7FFFFFFF) or 1)
    cached_file_id = cache.get_file_id(synthetic_id)
    if cached_file_id:
        track = await _resolve_track_meta(synthetic_id, context)
        caption = _build_full_caption(
            track["artist"], track["title"], "", int(track.get("duration") or 0), lang=lang
        )
        try:
            await context.bot.send_audio(
                chat_id=msg.chat_id,
                audio=cached_file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=_build_track_actions_keyboard(
                    synthetic_id, user_id=user_id, lang=lang, is_inline=False, artist=track["artist"]
                ),
            )
            return
        except Exception:
            cache.delete_file_id(synthetic_id)

    status_msg = await msg.reply_text(t["url_downloading"], parse_mode=ParseMode.HTML)
    dl_info = await download_from_url(url)

    if not dl_info or not dl_info.get("file_path"):
        try:
            await status_msg.edit_text(t["url_failed"], parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return

    file_path = dl_info["file_path"]
    title = dl_info.get("title") or "Audio Track"
    artist = dl_info.get("artist") or "Unknown Artist"
    duration = int(dl_info.get("duration") or 0)
    thumb_url = dl_info.get("thumbnail_url") or ""

    track_meta = {
        "id": synthetic_id,
        "title": title,
        "artist": artist,
        "album": "",
        "duration": duration,
        "cover_url": thumb_url,
        "preview_url": "",
    }
    cache.save_track_meta(track_meta)
    context.application.bot_data[str(synthetic_id)] = track_meta

    try:
        thumb_bytes = await download_url_bytes(thumb_url) if thumb_url else None
        caption = _build_full_caption(artist, title, "", duration, lang=lang)
        keyboard = _build_track_actions_keyboard(
            synthetic_id, user_id=user_id, lang=lang, is_inline=False, artist=artist
        )
        with open(file_path, "rb") as audio_f:
            sent = await context.bot.send_audio(
                chat_id=msg.chat_id,
                audio=audio_f,
                title=title,
                performer=artist,
                duration=duration if duration > 0 else None,
                thumbnail=thumb_bytes,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                read_timeout=120,
                write_timeout=120,
            )
            if sent and sent.audio:
                cache.save_file_id(
                    synthetic_id, title, artist, sent.audio.file_id, "", duration, thumb_url
                )
        try:
            await status_msg.delete()
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Failed sending direct URL audio: {e}")
        try:
            await status_msg.edit_text(t["url_failed"], parse_mode=ParseMode.HTML)
        except Exception:
            pass
    finally:
        cleanup_file(file_path)


# === Пошук текстом або за посиланням у приватному чаті з ботом ===
async def text_search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробляє прямі посилання (YouTube/TikTok/SoundCloud/Spotify) або текстовий пошук."""
    if not update.message or not update.message.text:
        return

    query = update.message.text.strip()
    if len(query) < 2 or query.startswith("/"):
        return

    lang = _get_lang(update.effective_user)
    if update.effective_user and cache.get_user_lang(update.effective_user.id) is None:
        cache.set_user_lang(update.effective_user.id, lang)

    # Перевіряємо, чи надіслано підтримуване посилання
    direct_url = extract_supported_url(query)
    if direct_url:
        await _handle_direct_url(direct_url, update, context, lang)
        return

    t = TEXTS[lang]
    status_msg = await update.message.reply_text(
        t["searching"].format(query=html.escape(query)),
        parse_mode=ParseMode.HTML,
    )

    tracks = await search_tracks(query, limit=8)
    if not tracks:
        await status_msg.edit_text(
            t["no_results_chat"].format(query=html.escape(query)),
            parse_mode=ParseMode.HTML,
        )
        return

    buttons = _build_track_list_buttons(tracks, context)

    await status_msg.edit_text(
        t["search_results_header"].format(query=html.escape(query)),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )

    asyncio.get_running_loop().run_in_executor(None, cache.save_tracks_meta_batch, tracks)


# === Callback: Відправка 30-секундного голосового прев'ю у чаті ===
async def preview_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Надсилає 30-секундне прев'ю як голосове повідомлення (Voice OGG Opus) з кнопкою скачування."""
    callback_query = update.callback_query
    if not callback_query or not callback_query.data:
        return

    lang = _get_lang(callback_query.from_user)
    t = TEXTS[lang]

    parts = callback_query.data.split(":")
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await callback_query.answer(t["invalid_data"], show_alert=True)
        return

    deezer_id = int(parts[1])
    chat_id = (
        callback_query.message.chat_id
        if callback_query.message
        else callback_query.from_user.id
    )

    track = await _resolve_track_meta(deezer_id, context)
    artist = track["artist"]
    title = track["title"]
    duration = int(track.get("duration") or 0)

    await callback_query.answer(t["preview_toast"])

    preview_url = track.get("preview_url")
    mp3_bytes = await download_url_bytes(preview_url) if preview_url else None

    if not mp3_bytes and deezer_id > 0:
        fresh_track = await get_track_by_id(deezer_id)
        if fresh_track:
            cache.save_track_meta(fresh_track)
            context.application.bot_data[str(deezer_id)] = fresh_track
            preview_url = fresh_track.get("preview_url")
            if preview_url:
                mp3_bytes = await download_url_bytes(preview_url)

    caption = _build_preview_caption(artist, title, duration, lang=lang)
    reply_markup = _build_track_actions_keyboard(
        deezer_id,
        user_id=callback_query.from_user.id if callback_query.from_user else None,
        lang=lang,
        is_inline=False,
        artist=artist,
        include_download=True,
        duration=duration,
    )

    if mp3_bytes:
        ogg_bytes = await convert_preview_to_ogg(mp3_bytes)
        voice_payload = ogg_bytes if ogg_bytes else mp3_bytes
        ext = "ogg" if ogg_bytes else "mp3"
        voice_file = io.BytesIO(voice_payload)
        voice_file.name = f"preview_{deezer_id}.{ext}"

        try:
            await context.bot.send_voice(
                chat_id=chat_id,
                voice=voice_file,
                duration=30,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
            return
        except Exception as e:
            logger.warning(f"Failed to send voice preview for {deezer_id}: {e}")

    await context.bot.send_message(
        chat_id=chat_id,
        text=t["no_preview_available"].format(
            artist=html.escape(artist), title=html.escape(title)
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )


# === Inline Query Handler (@bot назва пісні / порожній запит -> Топ-чарт / fav -> Вибране) ===
async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Миттєвий обробник інлайн-пошуку (@bot_name запит):
    - Порожній запит -> Топ-10 світових хітів Deezer (з кнопкою переходу до Вибраного, якщо є)
    - 'fav' / '❤️' / 'вибране' -> особистий плейлист Вибраного користувача
    - Будь-який інший текст -> пошук треків на Deezer
    """
    if not update.inline_query:
        return

    query = update.inline_query.query.strip()
    user = update.inline_query.from_user
    lang = _get_lang(user)
    t = TEXTS[lang]

    is_fav_query = query.lower() in ("fav", "favorites", "❤️", "вибране", "улюблене")
    is_chart_query = len(query) == 0

    if not is_chart_query and not is_fav_query and len(query) < 2:
        return

    if is_fav_query:
        tracks = cache.get_user_favorites(user.id, limit=15)
    elif is_chart_query:
        tracks = await get_chart_tracks(limit=10)
    else:
        logger.info(f"Inline query: '{query}' from user {user.id} (lang={lang})")
        tracks = await search_tracks(query, limit=10)

    fav_count = len(cache.get_user_favorites(user.id, limit=50))
    inline_button = (
        InlineQueryResultsButton(
            text=t["fav_inline_btn"].format(count=fav_count),
            start_parameter="favorites",
        )
        if fav_count > 0 and is_chart_query
        else None
    )

    if not tracks:
        empty_desc = (
            t["fav_empty"]
            if is_fav_query
            else t["no_results_inline_desc"].format(query=query or "Top")
        )
        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title=t["no_results_inline_title"],
                description=re.sub(r"<[^>]+>", "", empty_desc),
                input_message_content=InputTextMessageContent(
                    t["fav_empty"]
                    if is_fav_query
                    else t["no_results_inline_msg"].format(query=html.escape(query)),
                    parse_mode=ParseMode.HTML,
                ),
            )
        ]
        await update.inline_query.answer(
            results, cache_time=5, is_personal=True, button=inline_button
        )
        return

    results = []
    for idx, track in enumerate(tracks, start=1):
        deezer_id = int(track["id"])
        duration = int(track.get("duration") or 0)
        duration_str = format_duration(duration)

        context.application.bot_data[str(deezer_id)] = track

        cached_file_id = cache.get_file_id(deezer_id)
        rank_prefix = f"🔥 #{idx} " if is_chart_query else ""

        if cached_file_id:
            results.append(
                InlineQueryResultCachedAudio(
                    id=str(deezer_id),
                    audio_file_id=cached_file_id,
                    caption=_build_full_caption(
                        track["artist"],
                        track["title"],
                        track.get("album", ""),
                        duration,
                        lang=lang,
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=_build_track_actions_keyboard(
                        deezer_id,
                        user_id=user.id,
                        lang=lang,
                        is_inline=True,
                        artist=track["artist"],
                    ),
                )
            )
        else:
            preview_url = track.get("preview_url") or SILENT_FALLBACK_URL
            caption = _build_preview_caption(
                track["artist"], track["title"], duration, lang=lang
            )
            keyboard = _build_track_actions_keyboard(
                deezer_id,
                user_id=user.id,
                lang=lang,
                is_inline=True,
                artist=track["artist"],
                include_download=True,
                duration=duration,
            )

            results.append(
                InlineQueryResultAudio(
                    id=str(deezer_id),
                    audio_url=preview_url,
                    title=f"🎵 {rank_prefix}{track['title']} ({duration_str})",
                    performer=track["artist"],
                    audio_duration=30,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            )

    cache_ttl = 2
    await update.inline_query.answer(
        results, cache_time=cache_ttl, is_personal=True, button=inline_button
    )

    if not is_fav_query:
        asyncio.get_running_loop().run_in_executor(
            None, cache.save_tracks_meta_batch, tracks
        )


# === Допоміжна функція завантаження та кешування повного MP3 ===
async def _ensure_full_mp3_file_id(
    deezer_id: int,
    track: dict,
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    target_chat_id: Optional[int] = None,
    lang: str = "uk",
) -> tuple[Optional[str], bool, bool]:
    """
    Повертає (file_id, already_sent_to_target_chat, needs_bot_start).
    Використовує лок на deezer_id та паралельно завантажує MP3 і обкладинку альбому.
    """
    cached_id = cache.get_file_id(deezer_id)
    if cached_id:
        return cached_id, False, False

    lock = _get_track_lock(deezer_id)
    async with lock:
        cached_id = cache.get_file_id(deezer_id)
        if cached_id:
            return cached_id, False, False

        artist = track["artist"]
        title = track["title"]
        album = track.get("album", "")
        duration = int(track.get("duration") or 0)
        cover_url = track.get("cover_url", "")

        file_path = None
        try:
            if cover_url:
                file_path, thumb_bytes = await asyncio.gather(
                    download_track(artist, title, target_duration=duration),
                    download_url_bytes(cover_url),
                )
            else:
                file_path = await download_track(artist, title, target_duration=duration)
                thumb_bytes = None

            if not file_path or not os.path.exists(file_path):
                return None, False, False

            caption = _build_full_caption(artist, title, album, duration, lang=lang)

            if target_chat_id is not None:
                keyboard = _build_track_actions_keyboard(
                    deezer_id,
                    user_id=user_id,
                    lang=lang,
                    is_inline=False,
                    artist=artist,
                )
                upload_lock = _get_track_lock(f"upload:{target_chat_id}")
                async with upload_lock:
                    for attempt in range(3):
                        try:
                            with open(file_path, "rb") as audio_f:
                                sent = await context.bot.send_audio(
                                    chat_id=target_chat_id,
                                    audio=audio_f,
                                    title=title,
                                    performer=artist,
                                    duration=duration if duration > 0 else None,
                                    thumbnail=thumb_bytes,
                                    caption=caption,
                                    parse_mode=ParseMode.HTML,
                                    reply_markup=keyboard,
                                    read_timeout=120,
                                    write_timeout=120,
                                )
                                if sent and sent.audio:
                                    file_id = sent.audio.file_id
                                    cache.save_file_id(
                                        deezer_id, title, artist, file_id, album, duration, cover_url
                                    )
                                    return file_id, True, False
                        except RetryAfter as ra:
                            await asyncio.sleep(float(ra.retry_after) + 0.5)
                        except (TimedOut, NetworkError) as net_err:
                            logger.warning(f"Upload attempt {attempt + 1} timed out: {net_err}")
                            await asyncio.sleep(1.0)
                        except Exception as chat_up_err:
                            logger.warning(f"Direct chat upload failed: {chat_up_err}")
                            break

            upload_chats = []
            if STORAGE_CHAT_ID:
                try:
                    upload_chats.append((int(STORAGE_CHAT_ID), False))
                except ValueError:
                    upload_chats.append((STORAGE_CHAT_ID, False))
            upload_chats.append((user_id, True))
            for known_uid in cache.get_known_user_ids():
                if known_uid != user_id:
                    upload_chats.append((known_uid, True))

            needs_start = False
            for chat_dest, delete_after in upload_chats:
                upload_lock = _get_track_lock(f"upload:{chat_dest}")
                async with upload_lock:
                    for attempt in range(3):
                        try:
                            with open(file_path, "rb") as audio_f:
                                sent = await context.bot.send_audio(
                                    chat_id=chat_dest,
                                    audio=audio_f,
                                    title=title,
                                    performer=artist,
                                    duration=duration if duration > 0 else None,
                                    thumbnail=thumb_bytes,
                                    caption=caption,
                                    parse_mode=ParseMode.HTML,
                                    disable_notification=True,
                                    read_timeout=120,
                                    write_timeout=120,
                                )
                                if sent and sent.audio:
                                    file_id = sent.audio.file_id
                                    cache.save_file_id(
                                        deezer_id, title, artist, file_id, album, duration, cover_url
                                    )
                                    if delete_after:
                                        try:
                                            await sent.delete()
                                        except Exception as del_err:
                                            logger.debug(f"Could not delete temp DM msg: {del_err}")
                                    return file_id, False, False
                        except Forbidden:
                            needs_start = True
                            break
                        except RetryAfter as ra:
                            await asyncio.sleep(float(ra.retry_after) + 0.5)
                        except (TimedOut, NetworkError) as net_err:
                            logger.warning(f"Upload to {chat_dest} attempt {attempt + 1} timed out: {net_err}")
                            await asyncio.sleep(1.0)
                        except Exception as upload_err:
                            logger.warning(f"Upload to {chat_dest} failed: {upload_err}")
                            break

            return None, False, needs_start

        finally:
            if file_path:
                cleanup_file(file_path)


async def _send_or_download_to_chat(
    chat_id: int,
    deezer_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    lang: str = "uk",
    user_id: Optional[int] = None,
):
    """Надсилає повний MP3 у вказаний чат (з кешу або завантажує новий) з панеллю дій."""
    t = TEXTS.get(lang, TEXTS["uk"])
    uid = user_id or chat_id
    track = await _resolve_track_meta(deezer_id, context)
    artist = track["artist"]
    title = track["title"]
    album = track.get("album", "")
    duration = int(track.get("duration") or 0)
    caption = _build_full_caption(artist, title, album, duration, lang=lang)
    keyboard = _build_track_actions_keyboard(
        deezer_id, user_id=uid, lang=lang, is_inline=False, artist=artist
    )

    cached_file_id = cache.get_file_id(deezer_id)
    if cached_file_id:
        try:
            await context.bot.send_audio(
                chat_id=chat_id,
                audio=cached_file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            return
        except Exception as e:
            logger.warning(f"Cached file_id failed in chat {chat_id}, re-downloading: {e}")
            cache.delete_file_id(deezer_id)

    status = await context.bot.send_message(
        chat_id=chat_id,
        text=t["downloading_chat"].format(
            artist=html.escape(artist), title=html.escape(title)
        ),
        parse_mode=ParseMode.HTML,
    )

    file_id, already_sent, _ = await _ensure_full_mp3_file_id(
        deezer_id=deezer_id,
        track=track,
        user_id=uid,
        context=context,
        target_chat_id=chat_id,
        lang=lang,
    )

    try:
        await status.delete()
    except Exception:
        pass

    if not file_id:
        await context.bot.send_message(
            chat_id=chat_id,
            text=t["download_failed"].format(
                artist=html.escape(artist), title=html.escape(title)
            ),
            parse_mode=ParseMode.HTML,
        )
    elif not already_sent:
        await context.bot.send_audio(
            chat_id=chat_id,
            audio=file_id,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )


# === Callback Query Handler (Кнопка «📥 Завантажити MP3 / Download MP3») ===
async def download_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробник натискання кнопки завантаження повного MP3."""
    callback_query = update.callback_query
    if not callback_query or not callback_query.data:
        return

    lang = _get_lang(callback_query.from_user)
    t = TEXTS[lang]

    parts = callback_query.data.split(":")
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await callback_query.answer(t["invalid_data"], show_alert=True)
        return

    deezer_id = int(parts[1])
    is_inline = callback_query.inline_message_id is not None
    user_id = callback_query.from_user.id

    await callback_query.answer(t["download_toast"])

    track = await _resolve_track_meta(deezer_id, context)
    artist = track["artist"]
    title = track["title"]
    album = track.get("album", "")
    duration = int(track.get("duration") or 0)
    full_caption = _build_full_caption(artist, title, album, duration, lang=lang)

    logger.info(
        f"Download request: {artist} - {title} (deezer_id={deezer_id}, inline={is_inline}, lang={lang})"
    )

    loading_caption = t["downloading_inline"].format(
        artist=html.escape(artist), title=html.escape(title)
    )
    if is_inline:
        try:
            await context.bot.edit_message_caption(
                inline_message_id=callback_query.inline_message_id,
                caption=loading_caption,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.debug(f"Could not update inline caption: {e}")
    elif callback_query.message and (callback_query.message.voice or callback_query.message.audio):
        try:
            await callback_query.message.edit_caption(
                caption=loading_caption,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    # Сценарій 1: Кнопку натиснуто у звичайному чаті з ботом
    if not is_inline and callback_query.message:
        chat_id = callback_query.message.chat_id
        file_id, already_sent, _ = await _ensure_full_mp3_file_id(
            deezer_id=deezer_id,
            track=track,
            user_id=user_id,
            context=context,
            target_chat_id=chat_id,
            lang=lang,
        )

        if not file_id:
            try:
                await callback_query.message.edit_caption(
                    caption=t["download_failed"].format(
                        artist=html.escape(artist), title=html.escape(title)
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=_build_download_keyboard(deezer_id, duration, lang=lang),
                )
            except Exception:
                pass
            return

        if not already_sent:
            try:
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=file_id,
                    title=title,
                    performer=artist,
                    duration=duration if duration > 0 else None,
                    caption=full_caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=_build_track_actions_keyboard(
                        deezer_id, user_id=user_id, lang=lang, is_inline=False, artist=artist
                    ),
                )
            except Exception as e:
                logger.warning(f"Failed sending cached audio in chat, clearing cache: {e}")
                cache.delete_file_id(deezer_id)
                return

        # Видаляємо попереднє голосове прев'ю (але залишаємо меню списку пошуку/чарту, якщо натиснуто звідти)
        if callback_query.message.voice:
            try:
                await callback_query.message.delete()
            except Exception:
                pass
        return

    # Сценарій 2: Кнопку натиснуто в Інлайн-повідомленні (@bot у будь-якому чаті)
    file_id, _, needs_bot_start = await _ensure_full_mp3_file_id(
        deezer_id=deezer_id,
        track=track,
        user_id=user_id,
        context=context,
        target_chat_id=None,
        lang=lang,
    )

    if not file_id:
        if needs_bot_start:
            bot_username = context.bot.username or (await context.bot.get_me()).username
            deep_link = f"https://t.me/{bot_username}?start=track_{deezer_id}"
            try:
                await context.bot.edit_message_caption(
                    inline_message_id=callback_query.inline_message_id,
                    caption=t["deep_link_prompt"].format(
                        artist=html.escape(artist), title=html.escape(title)
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton(t["btn_open_download"], url=deep_link)]]
                    ),
                )
            except Exception as e:
                logger.debug(f"Could not set fallback deep-link caption: {e}")
        else:
            try:
                await context.bot.edit_message_caption(
                    inline_message_id=callback_query.inline_message_id,
                    caption=t["download_failed"].format(
                        artist=html.escape(artist), title=html.escape(title)
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=_build_download_keyboard(deezer_id, duration, lang=lang),
                )
            except Exception as e:
                logger.debug(f"Could not set inline error caption: {e}")
        return

    inline_actions_kb = _build_track_actions_keyboard(
        deezer_id, user_id=user_id, lang=lang, is_inline=True, artist=artist
    )

    for attempt in range(3):
        try:
            await context.bot.edit_message_media(
                inline_message_id=callback_query.inline_message_id,
                media=InputMediaAudio(
                    media=file_id,
                    title=title,
                    performer=artist,
                    duration=duration if duration > 0 else None,
                    caption=full_caption,
                    parse_mode=ParseMode.HTML,
                ),
                reply_markup=inline_actions_kb,
            )
            break
        except RetryAfter as ra:
            await asyncio.sleep(float(ra.retry_after) + 0.5)
        except (TimedOut, NetworkError) as net_err:
            logger.warning(f"edit_message_media attempt {attempt + 1} timed out: {net_err}")
            await asyncio.sleep(1.0)
        except TelegramError as e:
            if "Message is not modified" in str(e):
                break
            logger.error(f"Failed to edit inline message media: {e}", exc_info=True)
            break


# === Callback: Додавання / видалення з Вибраного (fav:<deezer_id> або favp:<deezer_id>) ===
async def favorite_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Додає або видаляє трек із Вибраного користувача в один клік."""
    query = update.callback_query
    if not query or not query.data or not query.from_user:
        return

    lang = _get_lang(query.from_user)
    t = TEXTS[lang]
    parts = query.data.split(":")
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await query.answer(t["invalid_data"])
        return

    is_preview = parts[0] == "favp"
    deezer_id = int(parts[1])
    user_id = query.from_user.id
    track = await _resolve_track_meta(deezer_id, context)
    cache.save_track_meta(track)

    added = cache.toggle_favorite(user_id, deezer_id)
    await query.answer(t["fav_added_toast"] if added else t["fav_removed_toast"])

    is_inline = query.inline_message_id is not None
    new_kb = _build_track_actions_keyboard(
        deezer_id,
        user_id=user_id,
        lang=lang,
        is_inline=is_inline,
        artist=track.get("artist", ""),
        include_download=is_preview,
        duration=int(track.get("duration") or 0),
    )
    try:
        if is_inline:
            await context.bot.edit_message_reply_markup(
                inline_message_id=query.inline_message_id,
                reply_markup=new_kb,
            )
        elif query.message:
            await query.message.edit_reply_markup(reply_markup=new_kb)
    except Exception:
        pass


def _paginate_lyrics(lyrics: str, max_escaped_chars: int) -> list[str]:
    """
    Розбиває повний текст пісні на сторінки по цілих рядках так,
    щоб жоден рядок чи куплет не обрізався і вміщувався в ліміт Telegram.
    """
    pages: list[str] = []
    current_lines: list[str] = []
    current_len = 0

    for raw_line in lyrics.splitlines():
        esc_line = html.escape(raw_line)
        line_cost = len(esc_line) + 1
        if current_lines and (current_len + line_cost > max_escaped_chars):
            pages.append("\n".join(current_lines).strip())
            current_lines = [esc_line]
            current_len = line_cost
        else:
            current_lines.append(esc_line)
            current_len += line_cost

    if current_lines:
        pages.append("\n".join(current_lines).strip())

    return pages or [html.escape(lyrics[:max_escaped_chars])]


# === Callback: Текст пісні (lyr[p]:<deezer_id>[:<page>] та lyrc[p]:<deezer_id>) ===
async def lyrics_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показує повний текст пісні з посторінковою навігацією (без обрізання куплетів)."""
    query = update.callback_query
    if not query or not query.data:
        return

    lang = _get_lang(query.from_user)
    t = TEXTS[lang]
    if query.data == "lyr:noop":
        await query.answer()
        return

    parts = query.data.split(":")
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await query.answer(t["invalid_data"])
        return

    action = parts[0]
    is_preview = action in ("lyrp", "lyrcp")
    deezer_id = int(parts[1])
    page = int(parts[2]) if (len(parts) >= 3 and parts[2].isdigit()) else 0
    is_page_turn = len(parts) >= 3

    user_id = query.from_user.id if query.from_user else 0
    is_inline = query.inline_message_id is not None
    track = await _resolve_track_meta(deezer_id, context)
    artist = track["artist"]
    title = track["title"]
    album = track.get("album", "")
    duration = int(track.get("duration") or 0)

    # Згортання тексту назад в інлайн-повідомленні
    if action in ("lyrc", "lyrcp") and is_inline:
        await query.answer()
        restored_caption = (
            _build_preview_caption(artist, title, duration, lang=lang)
            if is_preview
            else _build_full_caption(artist, title, album, duration, lang=lang)
        )
        try:
            await context.bot.edit_message_caption(
                inline_message_id=query.inline_message_id,
                caption=restored_caption,
                parse_mode=ParseMode.HTML,
                reply_markup=_build_track_actions_keyboard(
                    deezer_id,
                    user_id=user_id,
                    lang=lang,
                    is_inline=True,
                    artist=artist,
                    include_download=is_preview,
                    duration=duration,
                ),
            )
        except Exception:
            pass
        return

    lyrics = await get_track_lyrics(artist, title)

    if not lyrics:
        not_found_msg = t["lyrics_not_found"].format(
            artist=html.escape(artist), title=html.escape(title)
        )
        if not is_inline and query.message:
            await query.answer()
            await query.message.reply_text(
                not_found_msg,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(t["btn_close"], callback_data="delmsg")]]
                ),
            )
        else:
            await query.answer(re.sub(r"<[^>]+>", "", not_found_msg), show_alert=True)
        return

    await query.answer()

    if is_inline:
        # В інлайн-повідомленні ліміт підпису (caption) у Telegram — 1024 символи,
        # тому розбиваємо довгий текст пісні на зручні сторінки з кнопками ⬅️ / ➡️
        header = _stabilize_caption_width([f"🎵 <b>{html.escape(artist)} — {html.escape(title)}</b>"]) + "\n"
        max_page_chars = max(350, 760 - len(header))
        pages = _paginate_lyrics(lyrics, max_page_chars)
        total_pages = len(pages)
        page = max(0, min(page, total_pages - 1))

        lyr_prefix = "lyrp" if is_preview else "lyr"
        lyrc_prefix = "lyrcp" if is_preview else "lyrc"

        inline_caption = f"{header}<blockquote>{pages[page]}</blockquote>"
        kb_rows = []
        if total_pages > 1:
            prev_btn = (
                InlineKeyboardButton("⬅️", callback_data=f"{lyr_prefix}:{deezer_id}:{page - 1}")
                if page > 0
                else InlineKeyboardButton("·", callback_data="lyr:noop")
            )
            page_btn = InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="lyr:noop")
            next_btn = (
                InlineKeyboardButton("➡️", callback_data=f"{lyr_prefix}:{deezer_id}:{page + 1}")
                if page < total_pages - 1
                else InlineKeyboardButton("·", callback_data="lyr:noop")
            )
            kb_rows.append([prev_btn, page_btn, next_btn])

        kb_rows.append(
            [InlineKeyboardButton(t["btn_hide_lyrics"], callback_data=f"{lyrc_prefix}:{deezer_id}")]
        )

        try:
            await context.bot.edit_message_caption(
                inline_message_id=query.inline_message_id,
                caption=inline_caption,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(kb_rows),
            )
        except Exception as e:
            logger.warning(f"Could not show inline lyrics: {e}")
        return

    # У звичайному чаті ліміт повідомлення — 4096 символів (вміщує всю пісню цілком, або з пагінацією для дуже довгих)
    raw_header = t["lyrics_header"].format(
        artist=html.escape(artist), title=html.escape(title)
    )
    header = _stabilize_caption_width([raw_header])
    pages = _paginate_lyrics(lyrics, 3600)
    total_pages = len(pages)
    page = max(0, min(page, total_pages - 1))

    text_body = f"{header}\n\n<blockquote>{pages[page]}</blockquote>"
    kb_rows = []
    if total_pages > 1:
        prev_btn = (
            InlineKeyboardButton("⬅️", callback_data=f"lyr:{deezer_id}:{page - 1}")
            if page > 0
            else InlineKeyboardButton("·", callback_data="lyr:noop")
        )
        page_btn = InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="lyr:noop")
        next_btn = (
            InlineKeyboardButton("➡️", callback_data=f"lyr:{deezer_id}:{page + 1}")
            if page < total_pages - 1
            else InlineKeyboardButton("·", callback_data="lyr:noop")
        )
        kb_rows.append([prev_btn, page_btn, next_btn])

    kb_rows.append([InlineKeyboardButton(t["btn_close"], callback_data="delmsg")])

    if query.message:
        if is_page_turn:
            try:
                await query.message.edit_text(
                    text_body,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(kb_rows),
                )
                return
            except Exception:
                pass
        await query.message.reply_text(
            text_body,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(kb_rows),
        )


# === Callback: Схожі треки (sim:<deezer_id>) ===
async def similar_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показує добірку схожих треків та топ-хітів виконавця."""
    query = update.callback_query
    if not query or not query.data:
        return

    lang = _get_lang(query.from_user)
    t = TEXTS[lang]
    parts = query.data.split(":")
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await query.answer(t["invalid_data"])
        return

    deezer_id = int(parts[1])
    await query.answer()

    track = await _resolve_track_meta(deezer_id, context)
    artist = track["artist"]
    title = track["title"]

    similar = await get_similar_tracks(deezer_id, artist=artist, title=title, limit=6)
    if not similar:
        if query.message:
            await query.message.reply_text(t["similar_empty"], parse_mode=ParseMode.HTML)
        return

    buttons = _build_track_list_buttons(similar, context)
    buttons.append([InlineKeyboardButton(t["btn_close"], callback_data="delmsg")])
    asyncio.get_running_loop().run_in_executor(None, cache.save_tracks_meta_batch, similar)

    if query.message:
        await query.message.reply_text(
            t["similar_header"].format(artist=html.escape(artist)),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )


# === Callback: Аудіо-ефекти (fxm[p]:<id>, fxb[p]:<id>, fx:<effect>:<id>) ===
async def effects_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обробляє меню ефектів (Slowed + Reverb, Speed Up, Bass Boost) як у чаті, так і в @ім'я_бота інлайн-режимі:
    - fxm:<id> / fxmp:<id> — розгортає підменю ефектів (2×2)
    - fxb:<id> / fxbp:<id> — повертає основну панель дій
    - fx:<effect>:<id> — генерує трек з обраним ефектом (у чаті надсилає MP3, в інлайн-режимі замінює плеєр на місці)
    """
    query = update.callback_query
    if not query or not query.data:
        return

    lang = _get_lang(query.from_user)
    t = TEXTS[lang]
    user_id = query.from_user.id if query.from_user else 0
    is_inline = query.inline_message_id is not None
    parts = query.data.split(":")

    if parts[0] in ("fxm", "fxmp") and len(parts) >= 2 and parts[1].lstrip("-").isdigit():
        deezer_id = int(parts[1])
        is_preview = parts[0] == "fxmp"
        await query.answer()
        sub_kb = _build_effects_submenu_keyboard(deezer_id, lang=lang, is_preview=is_preview)
        try:
            if is_inline:
                await context.bot.edit_message_reply_markup(
                    inline_message_id=query.inline_message_id,
                    reply_markup=sub_kb,
                )
            elif query.message:
                await query.message.edit_reply_markup(reply_markup=sub_kb)
        except Exception:
            pass
        return

    if parts[0] in ("fxb", "fxbp") and len(parts) >= 2 and parts[1].lstrip("-").isdigit():
        deezer_id = int(parts[1])
        is_preview = parts[0] == "fxbp"
        track = await _resolve_track_meta(deezer_id, context)
        await query.answer()
        main_kb = _build_track_actions_keyboard(
            deezer_id,
            user_id=user_id,
            lang=lang,
            is_inline=is_inline,
            artist=track.get("artist", ""),
            include_download=is_preview,
            duration=int(track.get("duration") or 0),
        )
        try:
            if is_inline:
                await context.bot.edit_message_reply_markup(
                    inline_message_id=query.inline_message_id,
                    reply_markup=main_kb,
                )
            elif query.message:
                await query.message.edit_reply_markup(reply_markup=main_kb)
        except Exception:
            pass
        return

    if parts[0] == "fx" and len(parts) >= 3 and parts[2].lstrip("-").isdigit():
        effect = parts[1]
        deezer_id = int(parts[2])
        effect_label = EFFECT_LABELS.get(lang, EFFECT_LABELS["uk"]).get(effect, effect)
        await query.answer(t["effect_loading_toast"].format(effect=effect_label))

        chat_id = query.message.chat_id if query.message else user_id
        track = await _resolve_track_meta(deezer_id, context)
        artist = track["artist"]
        title = track["title"]
        duration = int(track.get("duration") or 0)
        fx_title = f"{title} ({effect_label})"

        # Сценарій А: Інлайн-повідомлення (@ім'я_бота в будь-якому чаті) -> заміна аудіо на місці
        if is_inline:
            cached_fx_id = cache.get_effect_file_id(deezer_id, effect)
            if cached_fx_id:
                try:
                    await context.bot.edit_message_media(
                        inline_message_id=query.inline_message_id,
                        media=InputMediaAudio(
                            media=cached_fx_id,
                            title=fx_title,
                            performer=artist,
                            caption=_build_full_caption(
                                artist, title, "", 0, lang=lang, effect_label=effect_label
                            ),
                            parse_mode=ParseMode.HTML,
                        ),
                        reply_markup=_build_track_actions_keyboard(
                            deezer_id,
                            user_id=user_id,
                            lang=lang,
                            is_inline=True,
                            artist=artist,
                            show_original_btn=True,
                            duration=duration,
                        ),
                    )
                    return
                except Exception:
                    pass

            try:
                await context.bot.edit_message_caption(
                    inline_message_id=query.inline_message_id,
                    caption=t["effect_processing"].format(
                        effect=html.escape(effect_label),
                        artist=html.escape(artist),
                        title=html.escape(title),
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

            lock = _get_track_lock(f"fx:{deezer_id}:{effect}")
            async with lock:
                base_path = None
                fx_path = None
                temp_dl_dir = None
                try:
                    base_file_id = cache.get_file_id(deezer_id)
                    if base_file_id:
                        try:
                            tg_file = await context.bot.get_file(base_file_id)
                            temp_dl_dir = tempfile.mkdtemp(prefix="tgbot_dl_")
                            candidate_path = os.path.join(temp_dl_dir, "base.mp3")
                            await tg_file.download_to_drive(custom_path=candidate_path)
                            if os.path.exists(candidate_path) and os.path.getsize(candidate_path) > 20_000:
                                base_path = candidate_path
                        except Exception:
                            pass

                    if not base_path:
                        base_path = await download_track(artist, title, target_duration=duration)

                    if not base_path or not os.path.exists(base_path):
                        return

                    fx_res = await apply_audio_effect(base_path, effect, base_duration=duration)
                    if not fx_res:
                        return

                    fx_path, new_duration = fx_res
                    caption = _build_full_caption(
                        artist, title, "", new_duration, lang=lang, effect_label=effect_label
                    )

                    upload_chats = []
                    if STORAGE_CHAT_ID:
                        try:
                            upload_chats.append((int(STORAGE_CHAT_ID), False))
                        except ValueError:
                            upload_chats.append((STORAGE_CHAT_ID, False))
                    upload_chats.append((user_id, True))
                    for known_uid in cache.get_known_user_ids():
                        if known_uid != user_id:
                            upload_chats.append((known_uid, True))

                    fx_file_id = None
                    for chat_dest, delete_after in upload_chats:
                        try:
                            with open(fx_path, "rb") as audio_f:
                                sent = await context.bot.send_audio(
                                    chat_id=chat_dest,
                                    audio=audio_f,
                                    title=fx_title,
                                    performer=artist,
                                    duration=new_duration if new_duration > 0 else None,
                                    caption=caption,
                                    parse_mode=ParseMode.HTML,
                                    disable_notification=True,
                                    read_timeout=120,
                                    write_timeout=120,
                                )
                                if sent and sent.audio:
                                    fx_file_id = sent.audio.file_id
                                    cache.save_effect_file_id(deezer_id, effect, fx_file_id)
                                    if delete_after:
                                        try:
                                            await sent.delete()
                                        except Exception:
                                            pass
                                    break
                        except Exception:
                            continue

                    if fx_file_id:
                        await context.bot.edit_message_media(
                            inline_message_id=query.inline_message_id,
                            media=InputMediaAudio(
                                media=fx_file_id,
                                title=fx_title,
                                performer=artist,
                                duration=new_duration if new_duration > 0 else None,
                                caption=caption,
                                parse_mode=ParseMode.HTML,
                            ),
                            reply_markup=_build_track_actions_keyboard(
                                deezer_id,
                                user_id=user_id,
                                lang=lang,
                                is_inline=True,
                                artist=artist,
                                show_original_btn=True,
                                duration=duration,
                            ),
                        )
                finally:
                    if base_path:
                        cleanup_file(base_path)
                    if fx_path:
                        cleanup_file(fx_path)
                    if temp_dl_dir and os.path.exists(temp_dl_dir):
                        cleanup_file(os.path.join(temp_dl_dir, "base.mp3"))
            return

        # Сценарій Б: Звичайний чат з ботом
        if query.message:
            try:
                await query.message.edit_reply_markup(
                    reply_markup=_build_track_actions_keyboard(
                        deezer_id, user_id=user_id, lang=lang, is_inline=False, artist=artist
                    )
                )
            except Exception:
                pass

        # 1. Миттєва перевірка кешу ефекту
        cached_fx_id = cache.get_effect_file_id(deezer_id, effect)
        if cached_fx_id:
            try:
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=cached_fx_id,
                    caption=_build_full_caption(
                        artist, title, "", 0, lang=lang, effect_label=effect_label
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=_build_track_actions_keyboard(
                        deezer_id, user_id=user_id, lang=lang, is_inline=False, artist=artist
                    ),
                )
                return
            except Exception:
                pass

        status_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=t["effect_processing"].format(
                effect=html.escape(effect_label),
                artist=html.escape(artist),
                title=html.escape(title),
            ),
            parse_mode=ParseMode.HTML,
        )

        lock = _get_track_lock(f"fx:{deezer_id}:{effect}")
        async with lock:
            base_path = None
            fx_path = None
            temp_dl_dir = None
            try:
                # Спочатку пробуємо швидко завантажити базовий MP3 із Telegram file_id (до 20 МБ)
                base_file_id = cache.get_file_id(deezer_id)
                if base_file_id:
                    try:
                        tg_file = await context.bot.get_file(base_file_id)
                        temp_dl_dir = tempfile.mkdtemp(prefix="tgbot_dl_")
                        candidate_path = os.path.join(temp_dl_dir, "base.mp3")
                        await tg_file.download_to_drive(custom_path=candidate_path)
                        if os.path.exists(candidate_path) and os.path.getsize(candidate_path) > 20_000:
                            base_path = candidate_path
                    except Exception as e:
                        logger.debug(f"Could not fetch base file from Telegram, re-downloading: {e}")

                if not base_path:
                    base_path = await download_track(artist, title, target_duration=duration)

                if not base_path or not os.path.exists(base_path):
                    await status_msg.edit_text(t["effect_failed"], parse_mode=ParseMode.HTML)
                    return

                fx_res = await apply_audio_effect(base_path, effect, base_duration=duration)
                if not fx_res:
                    await status_msg.edit_text(t["effect_failed"], parse_mode=ParseMode.HTML)
                    return

                fx_path, new_duration = fx_res
                caption = _build_full_caption(
                    artist, title, "", new_duration, lang=lang, effect_label=effect_label
                )
                with open(fx_path, "rb") as audio_f:
                    sent = await context.bot.send_audio(
                        chat_id=chat_id,
                        audio=audio_f,
                        title=fx_title,
                        performer=artist,
                        duration=new_duration if new_duration > 0 else None,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=_build_track_actions_keyboard(
                            deezer_id, user_id=user_id, lang=lang, is_inline=False, artist=artist
                        ),
                        read_timeout=120,
                        write_timeout=120,
                    )
                    if sent and sent.audio:
                        cache.save_effect_file_id(deezer_id, effect, sent.audio.file_id)

                try:
                    await status_msg.delete()
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"Error creating audio effect {effect}: {e}")
                try:
                    await status_msg.edit_text(t["effect_failed"], parse_mode=ParseMode.HTML)
                except Exception:
                    pass
            finally:
                if base_path:
                    cleanup_file(base_path)
                if fx_path:
                    cleanup_file(fx_path)
                if temp_dl_dir and os.path.exists(temp_dl_dir):
                    cleanup_file(os.path.join(temp_dl_dir, "base.mp3"))


# === Адмін-панель (/admin або /stats) — виключно для власника бота ===
def _format_admin_panel(lang: str = "uk") -> tuple[str, InlineKeyboardMarkup]:
    """Формує текст і клавіатуру адмін-панелі."""
    st = cache.get_admin_stats()
    recent_list = (
        "\n".join(f"  • {html.escape(item)}" for item in st["recent_tracks"])
        if st["recent_tracks"]
        else ("  • (ще немає)" if lang == "uk" else "  • (none yet)")
    )

    if lang == "en":
        text = (
            "🛡 <b>Bot Admin Panel</b>\n\n"
            f"👥 <b>Known users:</b> {st['users_count']}\n"
            f"⚡ <b>Cached MP3 tracks:</b> {st['cached_mp3_count']}\n"
            f"🎛 <b>Cached audio effects:</b> {st['cached_effects_count']}\n"
            f"🎵 <b>Indexed track metadata:</b> {st['known_tracks_count']}\n"
            f"❤️ <b>Saved in Favorites:</b> {st['favorites_count']}\n"
            f"💾 <b>Database size (cache.db):</b> {st['db_size_kb']} KB\n\n"
            f"🕐 <b>Last 5 cached tracks:</b>\n{recent_list}"
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔄 Refresh", callback_data="adm:refresh"),
                    InlineKeyboardButton("🧹 Clean Temp", callback_data="adm:cleantmp"),
                ],
                [InlineKeyboardButton("✖️ Close", callback_data="delmsg")],
            ]
        )
    else:
        text = (
            "🛡 <b>Адмін-панель бота</b>\n\n"
            f"👥 <b>Користувачів у базі:</b> {st['users_count']}\n"
            f"⚡ <b>Закешованих MP3 треків:</b> {st['cached_mp3_count']}\n"
            f"🎛 <b>Закешованих аудіо-ефектів:</b> {st['cached_effects_count']}\n"
            f"🎵 <b>Треків у базі метаданих:</b> {st['known_tracks_count']}\n"
            f"❤️ <b>Додавань у «Вибране»:</b> {st['favorites_count']}\n"
            f"💾 <b>Розмір бази (cache.db):</b> {st['db_size_kb']} КБ\n\n"
            f"🕐 <b>Останні 5 закешованих треків:</b>\n{recent_list}"
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔄 Оновити", callback_data="adm:refresh"),
                    InlineKeyboardButton("🧹 Очистити Temp", callback_data="adm:cleantmp"),
                ],
                [InlineKeyboardButton("✖️ Закрити", callback_data="delmsg")],
            ]
        )
    return text, kb


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Закрита команда /admin та /stats (доступна ТІЛЬКИ власнику бота)."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or not _is_admin(user.id):
        # Для сторонніх користувачів команда повністю ігнорується
        return

    lang = _get_lang(user)
    text, kb = _format_admin_panel(lang=lang)
    await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробляє кнопки в адмін-панелі (adm:refresh, adm:cleantmp)."""
    query = update.callback_query
    if not query or not query.data or not query.from_user:
        return

    if not _is_admin(query.from_user.id):
        await query.answer("⛔ Доступ лише для адміністратора", show_alert=True)
        return

    lang = _get_lang(query.from_user)
    if query.data == "adm:cleantmp":
        cleanup_temp_dir()
        await query.answer("🧹 Тимчасові папки очищено!" if lang == "uk" else "🧹 Temp folders cleaned!")
    else:
        await query.answer("🔄 Оновлено" if lang == "uk" else "🔄 Refreshed")

    text, kb = _format_admin_panel(lang=lang)
    if query.message:
        try:
            await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            pass


# === Global Error Handler ===
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Unhandled error: {context.error}", exc_info=context.error)


async def post_init(application: Application):
    """Прогріває HTTP-пул до Deezer та реєструє меню команд бота."""
    await warmup_session()
    try:
        await application.bot.set_my_commands(
            [
                BotCommand("start", TEXTS["uk"]["cmd_start"]),
                BotCommand("top", TEXTS["uk"]["cmd_top"]),
                BotCommand("favorites", TEXTS["uk"]["cmd_fav"]),
                BotCommand("lang", TEXTS["uk"]["cmd_lang"]),
            ]
        )
        await application.bot.set_my_commands(
            [
                BotCommand("start", TEXTS["en"]["cmd_start"]),
                BotCommand("top", TEXTS["en"]["cmd_top"]),
                BotCommand("favorites", TEXTS["en"]["cmd_fav"]),
                BotCommand("lang", TEXTS["en"]["cmd_lang"]),
            ],
            language_code="en",
        )
    except Exception as e:
        logger.debug(f"Could not set bot commands: {e}")


async def post_shutdown(application: Application):
    """Коректне закриття HTTP-сесії при зупинці бота."""
    await close_session()


# === Main Application Entry Point ===
def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not found in .env file!")

    logger.info("Starting Music Bot...")
    cleanup_temp_dir()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .connection_pool_size(64)
        .pool_timeout(60.0)
        .connect_timeout(20.0)
        .read_timeout(120.0)
        .write_timeout(120.0)
        .get_updates_connection_pool_size(8)
        .get_updates_pool_timeout(30.0)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("top", top_command))
    app.add_handler(CommandHandler(["favorites", "fav"], favorites_command))
    app.add_handler(CommandHandler("lang", lang_command))
    app.add_handler(CommandHandler(["admin", "stats"], admin_command))
    app.add_handler(InlineQueryHandler(inline_query_handler))
    app.add_handler(CallbackQueryHandler(nav_callback_handler, pattern=r"^(nav:|delmsg$)"))
    app.add_handler(CallbackQueryHandler(admin_callback_handler, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(lang_callback_handler, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(preview_callback_handler, pattern=r"^pv:"))
    app.add_handler(CallbackQueryHandler(download_callback_handler, pattern=r"^dl:"))
    app.add_handler(CallbackQueryHandler(favorite_callback_handler, pattern=r"^favp?:"))
    app.add_handler(CallbackQueryHandler(lyrics_callback_handler, pattern=r"^lyrc?p?:"))
    app.add_handler(CallbackQueryHandler(similar_callback_handler, pattern=r"^sim:"))
    app.add_handler(CallbackQueryHandler(effects_callback_handler, pattern=r"^fx([mb]p?)?:"))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_search_handler)
    )
    app.add_error_handler(error_handler)

    logger.info("Bot is starting polling...")

    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "inline_query", "callback_query"],
        close_loop=False,
    )


if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped cleanly.")
