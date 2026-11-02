"""
Telegram Inline & Direct Music Bot (UA 🇺🇦 / EN 🇬🇧).
Швидкий пошук через Deezer API (Keep-Alive + RAM-кеш), 30-секундні голосові прев'ю (Voice)
та багатопотокове завантаження повних MP3 треків (192 kbps) через yt-dlp з кешуванням у SQLite.
"""

import os
import io
import html
import asyncio
import logging
from uuid import uuid4
from typing import Optional
from dotenv import load_dotenv

from telegram import (
    Update,
    User,
    InlineQueryResultArticle,
    InlineQueryResultAudio,
    InlineQueryResultVoice,
    InlineQueryResultCachedAudio,
    InputTextMessageContent,
    InputMediaAudio,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode, ChatAction
from telegram.error import BadRequest, Forbidden, TelegramError
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
    get_track_by_id,
    download_url_bytes,
    format_duration,
    warmup_session,
    close_session,
)
from music_download import (
    download_track,
    convert_preview_to_ogg,
    cleanup_file,
    cleanup_temp_dir,
)
from cache_db import CacheDB

# === Configuration ===
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
STORAGE_CHAT_ID = os.getenv("STORAGE_CHAT_ID")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Ініціалізація SQLite + RAM кешу
cache = CacheDB()

# Локи для уникнення дублювання паралельних завантажень одного й того ж треку
_download_locks: dict[int, asyncio.Lock] = {}

SILENT_FALLBACK_URL = (
    "https://raw.githubusercontent.com/anars/blank-audio/master/1-second-of-silence.mp3"
)

# === Двомовні тексти (UA / EN) ===
TEXTS = {
    "uk": {
        "welcome": (
            "🎵 <b>Вітаю у Music Explorer Bot!</b>\n\n"
            "Шукайте музику, слухайте <b>30-секундне голосове прев'ю</b> без зайвого скачування "
            "та завантажуйте лише ті треки, які вам підходять!\n\n"
            "✨ <b>Як користуватися:</b>\n"
            "1️⃣ <b>Прямо в цьому чаті:</b> просто напишіть назву пісні або виконавця.\n"
            "2️⃣ <b>У будь-якому іншому чаті чи групі:</b> введіть <code>@{bot_name} назва пісні</code>.\n"
            "3️⃣ Прослухайте 30-секундне голосове прев'ю 🎤.\n"
            "4️⃣ Натисніть <b>«📥 Завантажити MP3»</b>, щоб отримати весь трек!\n\n"
            "🔍 <b>Приклади:</b>\n"
            "• <code>@{bot_name} Imagine Dragons Believer</code>\n"
            "• <code>@{bot_name} Океан Ельзи Обійми</code>\n\n"
            "⚡ <i>Треки з кешу відправляються миттєво! Змінити мову: /lang</i>"
        ),
        "lang_prompt": "🌐 <b>Оберіть мову інтерфейсу / Choose your language:</b>",
        "lang_changed": "✅ Мову змінено на <b>Українську 🇺🇦</b>",
        "searching": "🔍 Шукаю: <b>{query}</b>...",
        "no_results_chat": (
            "❌ За запитом <b>{query}</b> нічого не знайдено.\n"
            "Спробуйте іншу назву пісні або виконавця."
        ),
        "search_results_header": (
            "🎶 <b>Результати пошуку для:</b> <code>{query}</code>\n"
            "Оберіть трек для 30-сек голосового прев'ю або завантаження MP3:"
        ),
        "no_results_inline_title": "❌ Нічого не знайдено",
        "no_results_inline_desc": "Не знайдено треків за запитом '{query}'",
        "no_results_inline_msg": (
            "🔍 <b>Нічого не знайдено за запитом:</b> <code>{query}</code>\n"
            "Спробуйте іншу назву пісні."
        ),
        "album_label": "Альбом",
        "duration_label": "Тривалість",
        "quality_label": "Якість",
        "btn_download": "📥 Завантажити MP3{dur}",
        "btn_open_download": "📥 Завантажити MP3",
        "preview_toast": "🎤 Завантажую 30-сек голосове прев'ю...",
        "download_toast": "⏳ Завантажую повний MP3...",
        "invalid_data": "❌ Невірні дані запиту",
        "no_preview_available": (
            "🎧 <b>{artist} — {title}</b>\n"
            "<i>(Для цього треку 30-сек прев'ю недоступне в Deezer)</i>\n"
            "👇 Натисніть, щоб завантажити MP3:"
        ),
        "downloading_chat": (
            "⏳ <b>Завантажую повний MP3 (192 kbps)...</b>\n"
            "🎵 <b>{artist} — {title}</b>"
        ),
        "downloading_inline": (
            "⏳\u00A0<b>Завантажую\u00A0MP3\u00A0(192\u00A0kbps)...</b>\n"
            "🎵\u00A0<b>{artist}\u00A0—\u00A0{title}</b>\n"
            "<i>Зачекайте\u00A0кілька\u00A0секунд...</i>"
        ),
        "download_failed": "❌ <b>Не вдалося завантажити трек:</b>\n🎵 {artist} — {title}",
        "deep_link_prompt": (
            "⚠️\u00A0<b>Щоб\u00A0завантажити\u00A0цей\u00A0трек:</b>\n"
            "Натисніть\u00A0кнопку\u00A0нижче\u00A0(<b>{artist}\u00A0—\u00A0{title}</b>):"
        ),
        "ready_fallback": (
            "✅\u00A0<b>Повний\u00A0MP3\u00A0готовий!</b>\n"
            "Натисніть\u00A0кнопку\u00A0нижче\u00A0або\u00A0<code>@{bot_name}\u00A0{title}</code>:"
        ),
    },
    "en": {
        "welcome": (
            "🎵 <b>Welcome to Music Explorer Bot!</b>\n\n"
            "Search for music, listen to a <b>30-second voice preview</b> without downloading heavy files, "
            "and download only the full MP3 tracks you want!\n\n"
            "✨ <b>How to use:</b>\n"
            "1️⃣ <b>Directly in this chat:</b> just send a song title or artist name.\n"
            "2️⃣ <b>In any other chat or group:</b> type <code>@{bot_name} song title</code>.\n"
            "3️⃣ Listen to the 30-second voice preview 🎤.\n"
            "4️⃣ Tap <b>«📥 Download MP3»</b> to get the full track!\n\n"
            "🔍 <b>Examples:</b>\n"
            "• <code>@{bot_name} Imagine Dragons Believer</code>\n"
            "• <code>@{bot_name} Three Days Grace Animal</code>\n\n"
            "⚡ <i>Cached tracks are delivered instantly! Change language: /lang</i>"
        ),
        "lang_prompt": "🌐 <b>Choose your language / Оберіть мову інтерфейсу:</b>",
        "lang_changed": "✅ Language set to <b>English 🇬🇧</b>",
        "searching": "🔍 Searching: <b>{query}</b>...",
        "no_results_chat": (
            "❌ No results found for <b>{query}</b>.\n"
            "Try another song title or artist."
        ),
        "search_results_header": (
            "🎶 <b>Search results for:</b> <code>{query}</code>\n"
            "Select a track for a 30-sec voice preview or full MP3:"
        ),
        "no_results_inline_title": "❌ No Results Found",
        "no_results_inline_desc": "No tracks found for '{query}'",
        "no_results_inline_msg": (
            "🔍 <b>No results found for:</b> <code>{query}</code>\n"
            "Try a different search query."
        ),
        "album_label": "Album",
        "duration_label": "Duration",
        "quality_label": "Quality",
        "btn_download": "📥 Download MP3{dur}",
        "btn_open_download": "📥 Download MP3",
        "preview_toast": "🎤 Loading 30-sec voice preview...",
        "download_toast": "⏳ Downloading full MP3...",
        "invalid_data": "❌ Invalid request data",
        "no_preview_available": (
            "🎧 <b>{artist} — {title}</b>\n"
            "<i>(30-sec preview is unavailable on Deezer for this track)</i>\n"
            "👇 Tap below to download the full MP3:"
        ),
        "downloading_chat": (
            "⏳ <b>Downloading full MP3 (192 kbps)...</b>\n"
            "🎵 <b>{artist} — {title}</b>"
        ),
        "downloading_inline": (
            "⏳\u00A0<b>Downloading\u00A0MP3\u00A0(192\u00A0kbps)...</b>\n"
            "🎵\u00A0<b>{artist}\u00A0—\u00A0{title}</b>\n"
            "<i>Please\u00A0wait\u00A0a\u00A0few\u00A0seconds...</i>"
        ),
        "download_failed": "❌ <b>Could not download track:</b>\n🎵 {artist} — {title}",
        "deep_link_prompt": (
            "⚠️\u00A0<b>To\u00A0download\u00A0this\u00A0track:</b>\n"
            "Tap\u00A0the\u00A0button\u00A0below\u00A0(<b>{artist}\u00A0—\u00A0{title}</b>):"
        ),
        "ready_fallback": (
            "✅\u00A0<b>Full\u00A0MP3\u00A0is\u00A0ready!</b>\n"
            "Tap\u00A0below\u00A0or\u00A0type\u00A0<code>@{bot_name}\u00A0{title}</code>:"
        ),
    },
}


def _get_lang(user: Optional[User]) -> str:
    """Визначає мову користувача (з кешу налаштувань або Telegram language_code)."""
    if not user:
        return "uk"
    saved = cache.get_user_lang(user.id)
    if saved in ("uk", "en"):
        return saved
    code = (user.language_code or "").lower()
    if code.startswith(("uk", "ru", "be")):
        return "uk"
    return "en"


def _build_lang_keyboard(current_lang: str) -> InlineKeyboardMarkup:
    """Клавіатура перемикання мови (UA / EN)."""
    uk_label = "✅ 🇺🇦 Українська" if current_lang == "uk" else "🇺🇦 Українська"
    en_label = "✅ 🇬🇧 English" if current_lang == "en" else "🇬🇧 English"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(uk_label, callback_data="lang:uk"),
                InlineKeyboardButton(en_label, callback_data="lang:en"),
            ]
        ]
    )


def _get_track_lock(deezer_id: int) -> asyncio.Lock:
    """Повертає asyncio.Lock для конкретного deezer_id."""
    if deezer_id not in _download_locks:
        if len(_download_locks) > 500:
            unlocked = [k for k, v in _download_locks.items() if not v.locked()]
            for k in unlocked[:250]:
                _download_locks.pop(k, None)
        _download_locks[deezer_id] = asyncio.Lock()
    return _download_locks[deezer_id]


async def _resolve_track_meta(
    deezer_id: int, context: ContextTypes.DEFAULT_TYPE
) -> dict:
    """
    Миттєво отримує метадані треку:
    1) З RAM-кешу (cache.get_track_meta / bot_data)
    2) З локальної бази SQLite
    3) Напряму з Deezer API за deezer_id
    """
    db_meta = cache.get_track_meta(deezer_id)
    if db_meta and db_meta.get("title") and db_meta.get("title") != "Unknown Title":
        return db_meta

    deezer_key = str(deezer_id)
    meta = context.application.bot_data.get(deezer_key)
    if meta and meta.get("title") and meta.get("title") != "Unknown Title":
        return meta

    api_meta = await get_track_by_id(deezer_id)
    if api_meta:
        cache.save_track_meta(api_meta)
        context.application.bot_data[deezer_key] = api_meta
        return api_meta

    return {
        "id": deezer_id,
        "artist": "Unknown Artist",
        "title": "Unknown Title",
        "album": "",
        "duration": 0,
        "cover_url": "",
        "preview_url": "",
    }


def _build_full_caption(
    artist: str, title: str, album: str = "", duration: int = 0, lang: str = "uk"
) -> str:
    """Формує безпечний HTML-підпис для повного MP3 треку."""
    t = TEXTS.get(lang, TEXTS["uk"])
    safe_artist = html.escape(artist)
    safe_title = html.escape(title)
    lines = [f"🎵 <b>{safe_artist} — {safe_title}</b>"]
    if album:
        lines.append(f"💿 <b>{t['album_label']}:</b> {html.escape(album)}")
    if duration > 0:
        lines.append(f"⏱ <b>{t['duration_label']}:</b> {format_duration(duration)}")
    lines.append(f"⚡ <b>{t['quality_label']}:</b> 192 kbps MP3")
    return "\n".join(lines)


def _build_preview_caption(
    artist: str, title: str, duration: int = 0, lang: str = "uk"
) -> str:
    """
    Формує безпечний HTML-підпис для 30-секундного голосового прев'ю.
    Використовує нерозривні пробіли (\u00A0), щоб бульбашка голосового повідомлення
    мала таку саму ширину, як і кнопка завантаження під нею.
    """
    safe_artist = html.escape(artist)
    safe_title = html.escape(title)
    duration_str = format_duration(duration) if duration > 0 else "—"
    nb = "\u00A0"
    if lang == "en":
        return (
            f"🎧{nb}<b>{safe_artist}{nb}—{nb}{safe_title}</b>\n"
            f"🎤{nb}<b>Preview:</b>{nb}30s{nb}•{nb}<b>Full\u00A0track:</b>{nb}{duration_str}\n"
            f"👇{nb}Tap{nb}below{nb}to{nb}download{nb}full{nb}MP3:"
        )
    return (
        f"🎧{nb}<b>{safe_artist}{nb}—{nb}{safe_title}</b>\n"
        f"🎤{nb}<b>Прев'ю:</b>{nb}30{nb}сек{nb}•{nb}<b>Трек:</b>{nb}{duration_str}\n"
        f"👇{nb}Натисніть,{nb}щоб{nb}завантажити{nb}MP3:"
    )


def _build_download_keyboard(
    deezer_id: int, duration: int = 0, lang: str = "uk"
) -> InlineKeyboardMarkup:
    """Компактна кнопка завантаження повного MP3, вирівняна по ширині повідомлення."""
    t = TEXTS.get(lang, TEXTS["uk"])
    dur_label = f" ({format_duration(duration)})" if duration > 0 else ""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t["btn_download"].format(dur=dur_label),
                    callback_data=f"dl:{deezer_id}",
                )
            ]
        ]
    )


# === Command /start ===
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вітальне повідомлення з вибором мови або обробка deep-link (?start=track_<id>)."""
    if not update.message:
        return

    lang = _get_lang(update.effective_user)
    if update.effective_user and cache.get_user_lang(update.effective_user.id) is None:
        cache.set_user_lang(update.effective_user.id, lang)

    if context.args and len(context.args) > 0:
        arg = context.args[0]
        if arg.startswith("track_"):
            deezer_id_str = arg.replace("track_", "").strip()
            if deezer_id_str.isdigit():
                await _send_or_download_to_chat(
                    chat_id=update.effective_chat.id,
                    deezer_id=int(deezer_id_str),
                    context=context,
                    lang=lang,
                )
                return

    bot_name = html.escape(context.bot.username or "bot")
    await update.message.reply_text(
        TEXTS[lang]["welcome"].format(bot_name=bot_name),
        parse_mode=ParseMode.HTML,
        reply_markup=_build_lang_keyboard(lang),
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
                reply_markup=_build_lang_keyboard(lang),
            )
        except Exception:
            pass


# === Пошук текстом у приватному чаті з ботом ===
async def text_search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробляє звичайні текстові повідомлення як пошуковий запит."""
    if not update.message or not update.message.text:
        return

    query = update.message.text.strip()
    if len(query) < 2 or query.startswith("/"):
        return

    lang = _get_lang(update.effective_user)
    if update.effective_user and cache.get_user_lang(update.effective_user.id) is None:
        cache.set_user_lang(update.effective_user.id, lang)
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

    buttons = []
    for track in tracks:
        context.application.bot_data[str(track["id"])] = track
        dur_str = format_duration(track["duration"])
        is_cached = cache.get_file_id(track["id"]) is not None
        prefix = "⚡" if is_cached else "🎤"
        label = f"{prefix} {track['artist']} — {track['title']} ({dur_str})"
        if len(label) > 55:
            label = label[:52] + "..."
        buttons.append(
            [InlineKeyboardButton(label, callback_data=f"pv:{track['id']}")]
        )

    await status_msg.edit_text(
        t["search_results_header"].format(query=html.escape(query)),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )

    # Зберігаємо метадані пакетно у фоні без затримки відповіді
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
    if len(parts) < 2 or not parts[1].isdigit():
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

    # Спочатку пробуємо завантажити вже відомий preview_url (економить зайвий запит до Deezer)
    preview_url = track.get("preview_url")
    mp3_bytes = await download_url_bytes(preview_url) if preview_url else None

    if not mp3_bytes:
        fresh_track = await get_track_by_id(deezer_id)
        if fresh_track:
            cache.save_track_meta(fresh_track)
            context.application.bot_data[str(deezer_id)] = fresh_track
            preview_url = fresh_track.get("preview_url")
            if preview_url:
                mp3_bytes = await download_url_bytes(preview_url)

    caption = _build_preview_caption(artist, title, duration, lang=lang)
    reply_markup = _build_download_keyboard(deezer_id, duration, lang=lang)

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


# === Inline Query Handler (@bot назва пісні) ===
async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Миттєвий обробник інлайн-пошуку (@bot_name запит)."""
    if not update.inline_query:
        return

    query = update.inline_query.query.strip()
    if not query or len(query) < 2:
        return

    lang = _get_lang(update.inline_query.from_user)
    t = TEXTS[lang]

    logger.info(
        f"Inline query: '{query}' from user {update.inline_query.from_user.id} (lang={lang})"
    )

    tracks = await search_tracks(query, limit=10)

    if not tracks:
        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title=t["no_results_inline_title"],
                description=t["no_results_inline_desc"].format(query=query),
                input_message_content=InputTextMessageContent(
                    t["no_results_inline_msg"].format(query=html.escape(query)),
                    parse_mode=ParseMode.HTML,
                ),
            )
        ]
        await update.inline_query.answer(results, cache_time=10, is_personal=True)
        return

    results = []
    for track in tracks:
        deezer_id = int(track["id"])
        duration = int(track.get("duration") or 0)
        duration_str = format_duration(duration)

        context.application.bot_data[str(deezer_id)] = track

        # Миттєва перевірка в RAM-кеші (0 мс затримки)
        cached_file_id = cache.get_file_id(deezer_id)

        if cached_file_id:
            cached_res = InlineQueryResultCachedAudio(
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
            )
            results.append(cached_res)
        else:
            preview_url = track.get("preview_url") or SILENT_FALLBACK_URL
            caption = _build_preview_caption(
                track["artist"], track["title"], duration, lang=lang
            )
            keyboard = _build_download_keyboard(deezer_id, duration, lang=lang)
            preview_tag = "[30s Preview]" if lang == "en" else "[Прев'ю 30 сек]"

            # Використовуємо InlineQueryResultAudio, щоб Telegram дозволяв замінити
            # 30-секундне прев'ю на повний MP3 прямо в тому ж повідомленні чату (in-place)
            results.append(
                InlineQueryResultAudio(
                    id=str(deezer_id),
                    audio_url=preview_url,
                    title=f"🎧 {preview_tag} {track['title']} ({duration_str})",
                    performer=track["artist"],
                    audio_duration=30,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            )

    await update.inline_query.answer(results, cache_time=15, is_personal=True)

    # Пакетне збереження метаданих у SQLite виконується у фоні ПІСЛЯ відправки відповіді
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
            # Паралельно качаємо MP3 через yt-dlp та обкладинку з Deezer (0 мс зайвої затримки)
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
                        read_timeout=120,
                        write_timeout=120,
                    )
                    if sent and sent.audio:
                        file_id = sent.audio.file_id
                        cache.save_file_id(
                            deezer_id, title, artist, file_id, album, duration, cover_url
                        )
                        return file_id, True, False

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
                except Exception as upload_err:
                    logger.warning(f"Upload to {chat_dest} failed: {upload_err}")

            return None, False, needs_start

        finally:
            if file_path:
                cleanup_file(file_path)


async def _send_or_download_to_chat(
    chat_id: int, deezer_id: int, context: ContextTypes.DEFAULT_TYPE, lang: str = "uk"
):
    """Надсилає повний MP3 у вказаний чат (з кешу або завантажує новий)."""
    t = TEXTS.get(lang, TEXTS["uk"])
    track = await _resolve_track_meta(deezer_id, context)
    artist = track["artist"]
    title = track["title"]
    album = track.get("album", "")
    duration = int(track.get("duration") or 0)
    caption = _build_full_caption(artist, title, album, duration, lang=lang)

    cached_file_id = cache.get_file_id(deezer_id)
    if cached_file_id:
        try:
            await context.bot.send_audio(
                chat_id=chat_id,
                audio=cached_file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
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
        user_id=chat_id,
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
        )


# === Callback Query Handler (Кнопка «📥 Завантажити MP3 / Download MP3») ===
async def download_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробник натискання кнопки завантаження повного MP3 під прев'ю."""
    callback_query = update.callback_query
    if not callback_query or not callback_query.data:
        return

    lang = _get_lang(callback_query.from_user)
    t = TEXTS[lang]

    parts = callback_query.data.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
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
    elif callback_query.message:
        try:
            await callback_query.message.edit_caption(
                caption=loading_caption,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            try:
                await callback_query.message.edit_text(
                    text=loading_caption,
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
                )
            except Exception as e:
                logger.warning(f"Failed sending cached audio in chat, clearing cache: {e}")
                cache.delete_file_id(deezer_id)
                return

        try:
            await callback_query.message.delete()
        except Exception:
            pass
        return

    # Сценарій 2: Кнопку натиснуто в Інлайн-повідомленні
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
        )
    except BadRequest as e:
        logger.warning(f"Inline voice->audio edit fallback triggered: {e}")
        try:
            await context.bot.send_audio(
                chat_id=user_id,
                audio=file_id,
                caption=full_caption,
                parse_mode=ParseMode.HTML,
            )
        except Forbidden:
            pass

        bot_username = context.bot.username or (await context.bot.get_me()).username
        deep_link = f"https://t.me/{bot_username}?start=track_{deezer_id}"
        try:
            await context.bot.edit_message_caption(
                inline_message_id=callback_query.inline_message_id,
                caption=(
                    f"{full_caption}\n\n"
                    + t["ready_fallback"].format(
                        bot_name=html.escape(bot_username),
                        title=html.escape(title),
                    )
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(t["btn_open_download"], url=deep_link)]]
                ),
            )
        except Exception:
            pass
    except TelegramError as e:
        logger.error(f"Failed to edit inline message media: {e}", exc_info=True)


# === Global Error Handler ===
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Unhandled error: {context.error}", exc_info=context.error)


async def post_init(application: Application):
    """Прогріває HTTP-пул до Deezer при запуску бота для миттєвого першого пошуку."""
    await warmup_session()


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
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("lang", lang_command))
    app.add_handler(InlineQueryHandler(inline_query_handler))
    app.add_handler(CallbackQueryHandler(lang_callback_handler, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(preview_callback_handler, pattern=r"^pv:"))
    app.add_handler(CallbackQueryHandler(download_callback_handler, pattern=r"^dl:"))
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
