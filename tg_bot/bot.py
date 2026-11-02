"""
Telegram Inline & Direct Music Bot.
Пошук через Deezer API, 30-секундні голосові прев'ю (Voice)
та завантаження повних MP3 треків (192 kbps) через yt-dlp з кешуванням у SQLite.
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
STORAGE_CHAT_ID = os.getenv("STORAGE_CHAT_ID")  # Опціональний ID технічного чату/каналу для кешування

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Ініціалізація SQLite кешу
cache = CacheDB()

# Локи для уникнення дублювання паралельних завантажень одного й того ж треку
_download_locks: dict[int, asyncio.Lock] = {}

SILENT_FALLBACK_URL = (
    "https://raw.githubusercontent.com/anars/blank-audio/master/1-second-of-silence.mp3"
)


def _get_track_lock(deezer_id: int) -> asyncio.Lock:
    """Повертає asyncio.Lock для конкретного deezer_id."""
    if deezer_id not in _download_locks:
        if len(_download_locks) > 500:
            # Очищуємо старі неактивні локи, щоб уникнути витоку пам'яті
            unlocked = [k for k, v in _download_locks.items() if not v.locked()]
            for k in unlocked[:250]:
                _download_locks.pop(k, None)
        _download_locks[deezer_id] = asyncio.Lock()
    return _download_locks[deezer_id]


async def _resolve_track_meta(
    deezer_id: int, context: ContextTypes.DEFAULT_TYPE
) -> dict:
    """
    Надійно отримує метадані треку:
    1) З оперативної пам'яті (bot_data)
    2) З локальної бази SQLite (track_meta / track_cache)
    3) Напряму з Deezer API за deezer_id (щоб ніколи не було Unknown Artist)
    """
    deezer_key = str(deezer_id)
    meta = context.application.bot_data.get(deezer_key)
    if meta and meta.get("title") and meta.get("title") != "Unknown Title":
        return meta

    db_meta = cache.get_track_meta(deezer_id)
    if db_meta and db_meta.get("title") and db_meta.get("title") != "Unknown Title":
        context.application.bot_data[deezer_key] = db_meta
        return db_meta

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


def _build_full_caption(artist: str, title: str, album: str = "", duration: int = 0) -> str:
    """Формує безпечний HTML-підпис для повного MP3 треку."""
    safe_artist = html.escape(artist)
    safe_title = html.escape(title)
    lines = [f"🎵 <b>{safe_artist} — {safe_title}</b>"]
    if album:
        lines.append(f"💿 <b>Альбом:</b> {html.escape(album)}")
    if duration > 0:
        lines.append(f"⏱ <b>Тривалість:</b> {format_duration(duration)}")
    lines.append("⚡ <b>Якість:</b> 192 kbps MP3")
    return "\n".join(lines)


def _build_preview_caption(artist: str, title: str, duration: int = 0) -> str:
    """Формує безпечний HTML-підпис для 30-секундного голосового прев'ю."""
    safe_artist = html.escape(artist)
    safe_title = html.escape(title)
    duration_str = format_duration(duration) if duration > 0 else "—"
    return (
        f"🎧 <b>{safe_artist} — {safe_title}</b>\n"
        f"🎤 <b>Голосове прев'ю:</b> 30 сек <i>(Повний трек: {duration_str})</i>\n\n"
        f"👇 Натисніть кнопку нижче, щоб завантажити весь трек у MP3:"
    )


def _build_download_keyboard(deezer_id: int, duration: int = 0) -> InlineKeyboardMarkup:
    """Кнопка завантаження повного MP3 під прев'ю."""
    dur_label = f" ({format_duration(duration)})" if duration > 0 else ""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"📥 Завантажити повний MP3{dur_label}",
                    callback_data=f"dl:{deezer_id}",
                )
            ]
        ]
    )


# === Command /start ===
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вітальне повідомлення або обробка deep-link (?start=track_<id>)."""
    if not update.message:
        return

    # Deep-link: якщо користувач перейшов за посиланням для отримання треку
    if context.args and len(context.args) > 0:
        arg = context.args[0]
        if arg.startswith("track_"):
            deezer_id_str = arg.replace("track_", "").strip()
            if deezer_id_str.isdigit():
                await _send_or_download_to_chat(
                    chat_id=update.effective_chat.id,
                    deezer_id=int(deezer_id_str),
                    context=context,
                )
                return

    bot_name = context.bot.username or "bot"
    await update.message.reply_text(
        "🎵 <b>Вітаю у Music Explorer Bot!</b>\n\n"
        "Шукайте музику, слухайте <b>30-секундне голосове прев'ю</b> без зайвого скачування "
        "та завантажуйте лише ті треки, які вам підходять!\n\n"
        "✨ <b>Як користуватися:</b>\n"
        "1️⃣ <b>Прямо в цьому чаті:</b> просто напишіть назву пісні або виконавця.\n"
        f"2️⃣ <b>У будь-якому іншому чаті чи групі:</b> введіть <code>@{html.escape(bot_name)} назва пісні</code>.\n"
        "3️⃣ Прослухайте 30-секундне голосове прев'ю 🎤.\n"
        "4️⃣ Натисніть <b>«📥 Завантажити повний MP3»</b>, щоб отримати весь трек!\n\n"
        "🔍 <b>Приклади:</b>\n"
        f"• <code>@{html.escape(bot_name)} Imagine Dragons Believer</code>\n"
        f"• <code>@{html.escape(bot_name)} Океан Ельзи Обійми</code>\n\n"
        "⚡ <i>Треки з кешу відправляються миттєво!</i>",
        parse_mode=ParseMode.HTML,
    )


# === Пошук текстом у приватному чаті з ботом ===
async def text_search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробляє звичайні текстові повідомлення як пошуковий запит."""
    if not update.message or not update.message.text:
        return

    query = update.message.text.strip()
    if len(query) < 2 or query.startswith("/"):
        return

    status_msg = await update.message.reply_text(
        f"🔍 Шукаю: <b>{html.escape(query)}</b>...",
        parse_mode=ParseMode.HTML,
    )

    tracks = await search_tracks(query, limit=8)
    if not tracks:
        await status_msg.edit_text(
            f"❌ За запитом <b>{html.escape(query)}</b> нічого не знайдено.\n"
            "Спробуйте іншу назву пісні або виконавця.",
            parse_mode=ParseMode.HTML,
        )
        return

    buttons = []
    for track in tracks:
        cache.save_track_meta(track)
        context.application.bot_data[str(track["id"])] = track
        dur_str = format_duration(track["duration"])
        is_cached = cache.get_file_id(track["id"]) is not None
        prefix = "⚡" if is_cached else "🎤"
        label = f"{prefix} {track['artist']} — {track['title']} ({dur_str})"
        if len(label) > 60:
            label = label[:57] + "..."
        buttons.append(
            [InlineKeyboardButton(label, callback_data=f"pv:{track['id']}")]
        )

    await status_msg.edit_text(
        f"🎶 <b>Результати пошуку для:</b> <code>{html.escape(query)}</code>\n"
        "Оберіть трек, щоб прослухати 30-сек голосове прев'ю або завантажити MP3:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# === Callback: Відправка 30-секундного голосового прев'ю у чаті ===
async def preview_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Надсилає 30-секундне прев'ю як голосове повідомлення (Voice OGG Opus) з кнопкою скачування."""
    callback_query = update.callback_query
    if not callback_query or not callback_query.data:
        return

    parts = callback_query.data.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        await callback_query.answer("❌ Невірні дані треку", show_alert=True)
        return

    deezer_id = int(parts[1])
    chat_id = callback_query.message.chat_id if callback_query.message else callback_query.from_user.id

    track = await _resolve_track_meta(deezer_id, context)
    artist = track["artist"]
    title = track["title"]
    duration = int(track.get("duration") or 0)

    # Якщо трек вже є в кеші — можемо або одразу відправити MP3, або показати голосове прев'ю.
    # За вимогою користувача: завжди даємо можливість послухати 30-сек голосове прев'ю,
    # щоб не качати одразу важкий файл, якщо це не та пісня!
    await callback_query.answer("🎤 Завантажую 30-сек голосове прев'ю...")

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.RECORD_VOICE)
    except Exception:
        pass

    # Отримуємо свіжий preview_url з Deezer (оскільки підписані посилання Deezer мають термін дії)
    fresh_track = await get_track_by_id(deezer_id)
    if fresh_track:
        cache.save_track_meta(fresh_track)
        context.application.bot_data[str(deezer_id)] = fresh_track
        preview_url = fresh_track.get("preview_url") or track.get("preview_url")
    else:
        preview_url = track.get("preview_url")

    caption = _build_preview_caption(artist, title, duration)
    reply_markup = _build_download_keyboard(deezer_id, duration)

    if preview_url:
        mp3_bytes = await download_url_bytes(preview_url)
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

    # Якщо у треку в Deezer немає прев'ю — повідомляємо і даємо кнопку скачати повний MP3
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🎧 <b>{html.escape(artist)} — {html.escape(title)}</b>\n"
            f"<i>(Для цього треку 30-сек прев'ю недоступне в Deezer)</i>\n\n"
            f"👇 Натисніть кнопку нижче, щоб завантажити повний MP3:"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )


# === Inline Query Handler (@bot назва пісні) ===
async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробник інлайн-пошуку (@bot_name запит)."""
    if not update.inline_query:
        return

    query = update.inline_query.query.strip()
    if not query or len(query) < 2:
        return

    logger.info(f"Inline query: '{query}' from user {update.inline_query.from_user.id}")

    tracks = await search_tracks(query, limit=10)

    if not tracks:
        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="❌ Нічого не знайдено",
                description=f"Не знайдено треків за запитом '{query}'",
                input_message_content=InputTextMessageContent(
                    f"🔍 <b>Нічого не знайдено за запитом:</b> <code>{html.escape(query)}</code>\n"
                    "Спробуйте іншу назву пісні.",
                    parse_mode=ParseMode.HTML,
                ),
            )
        ]
        await update.inline_query.answer(results, cache_time=10)
        return

    results = []
    fallback_results = []
    for track in tracks:
        deezer_id = int(track["id"])
        duration = int(track.get("duration") or 0)
        duration_str = format_duration(duration)

        # Зберігаємо метадані і в SQLite, і в пам'яті (щоб після рестарту бота все працювало)
        cache.save_track_meta(track)
        context.application.bot_data[str(deezer_id)] = track

        cached_file_id = cache.get_file_id(deezer_id)

        if cached_file_id:
            # Трек вже в кеші — віддаємо повне MP3 миттєво
            cached_res = InlineQueryResultCachedAudio(
                id=str(deezer_id),
                audio_file_id=cached_file_id,
                caption=_build_full_caption(
                    track["artist"], track["title"], track.get("album", ""), duration
                ),
                parse_mode=ParseMode.HTML,
            )
            results.append(cached_res)
            fallback_results.append(cached_res)
        else:
            # Новий трек — надсилаємо 30-секундне ГОЛОСОВЕ прев'ю (Voice) з кнопкою завантаження всього треку
            preview_url = track.get("preview_url") or SILENT_FALLBACK_URL
            caption = _build_preview_caption(track["artist"], track["title"], duration)
            keyboard = _build_download_keyboard(deezer_id, duration)

            results.append(
                InlineQueryResultVoice(
                    id=str(deezer_id),
                    voice_url=preview_url,
                    title=f"🎤 {track['artist']} — {track['title']} ({duration_str})",
                    voice_duration=30,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            )
            fallback_results.append(
                InlineQueryResultAudio(
                    id=str(deezer_id),
                    audio_url=preview_url,
                    title=f"🎧 {track['title']} (Прев'ю 30с)",
                    performer=track["artist"],
                    audio_duration=30,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            )

    try:
        await update.inline_query.answer(results, cache_time=15, is_personal=False)
    except BadRequest as e:
        logger.warning(f"InlineQueryResultVoice rejected by Telegram ({e}), using audio preview fallback")
        await update.inline_query.answer(fallback_results, cache_time=15, is_personal=False)


# === Допоміжна функція завантаження та кешування повного MP3 ===
async def _ensure_full_mp3_file_id(
    deezer_id: int,
    track: dict,
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    target_chat_id: Optional[int] = None,
) -> tuple[Optional[str], bool]:
    """
    Повертає (file_id, already_sent_to_target_chat).
    Використовує лок на deezer_id, щоб не качати двічі один і той самий трек.
    """
    cached_id = cache.get_file_id(deezer_id)
    if cached_id:
        return cached_id, False

    lock = _get_track_lock(deezer_id)
    async with lock:
        # Перевіряємо кеш ще раз після входу в лок
        cached_id = cache.get_file_id(deezer_id)
        if cached_id:
            return cached_id, False

        artist = track["artist"]
        title = track["title"]
        album = track.get("album", "")
        duration = int(track.get("duration") or 0)
        cover_url = track.get("cover_url", "")

        file_path = None
        try:
            file_path = await download_track(artist, title, target_duration=duration)
            if not file_path or not os.path.exists(file_path):
                return None, False

            thumb_bytes = await download_url_bytes(cover_url) if cover_url else None
            caption = _build_full_caption(artist, title, album, duration)

            # Якщо завантаження викликане зі звичайного чату (target_chat_id задано),
            # відправляємо аудіо одразу туди і отримуємо file_id без зайвих видалень!
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
                        return file_id, True

            # Для інлайн-режиму: завантажуємо у STORAGE_CHAT_ID (якщо налаштований) або в ПП user_id
            upload_chats = []
            if STORAGE_CHAT_ID:
                try:
                    upload_chats.append((int(STORAGE_CHAT_ID), False))
                except ValueError:
                    upload_chats.append((STORAGE_CHAT_ID, False))
            upload_chats.append((user_id, True))

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
                            return file_id, False
                except Exception as upload_err:
                    logger.warning(f"Upload to {chat_dest} failed: {upload_err}")

            return None, False

        finally:
            if file_path:
                cleanup_file(file_path)


async def _send_or_download_to_chat(
    chat_id: int, deezer_id: int, context: ContextTypes.DEFAULT_TYPE
):
    """Надсилає повний MP3 у вказаний чат (з кешу або завантажує новий)."""
    track = await _resolve_track_meta(deezer_id, context)
    artist = track["artist"]
    title = track["title"]
    album = track.get("album", "")
    duration = int(track.get("duration") or 0)
    caption = _build_full_caption(artist, title, album, duration)

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
        text=(
            f"⏳ <b>Завантажую повний MP3...</b>\n"
            f"🎵 <b>{html.escape(artist)} — {html.escape(title)}</b>"
        ),
        parse_mode=ParseMode.HTML,
    )

    file_id, already_sent = await _ensure_full_mp3_file_id(
        deezer_id=deezer_id,
        track=track,
        user_id=chat_id,
        context=context,
        target_chat_id=chat_id,
    )

    try:
        await status.delete()
    except Exception:
        pass

    if not file_id:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Не вдалося завантажити трек: <b>{html.escape(artist)} — {html.escape(title)}</b>",
            parse_mode=ParseMode.HTML,
        )
    elif not already_sent:
        await context.bot.send_audio(
            chat_id=chat_id,
            audio=file_id,
            caption=caption,
            parse_mode=ParseMode.HTML,
        )


# === Callback Query Handler (Кнопка «📥 Завантажити повний MP3») ===
async def download_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробник натискання кнопки завантаження повного MP3 під прев'ю."""
    callback_query = update.callback_query
    if not callback_query or not callback_query.data:
        return

    parts = callback_query.data.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        await callback_query.answer("❌ Невірні дані запиту", show_alert=True)
        return

    deezer_id = int(parts[1])
    is_inline = callback_query.inline_message_id is not None
    user_id = callback_query.from_user.id

    await callback_query.answer("⏳ Завантажую повний MP3...")

    track = await _resolve_track_meta(deezer_id, context)
    artist = track["artist"]
    title = track["title"]
    album = track.get("album", "")
    duration = int(track.get("duration") or 0)
    full_caption = _build_full_caption(artist, title, album, duration)

    logger.info(
        f"Download request: {artist} - {title} (deezer_id={deezer_id}, inline={is_inline})"
    )

    # Оновлюємо підпис прев'ю на "Завантажується..."
    loading_caption = (
        f"⏳ <b>Завантажую повний MP3 (192 kbps)...</b>\n"
        f"🎵 <b>{html.escape(artist)} — {html.escape(title)}</b>\n\n"
        f"<i>Зачекайте кілька секунд...</i>"
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

    # Сценарій 1: Кнопку натиснуто у звичайному чаті з ботом (не інлайн)
    if not is_inline and callback_query.message:
        chat_id = callback_query.message.chat_id
        file_id, already_sent = await _ensure_full_mp3_file_id(
            deezer_id=deezer_id,
            track=track,
            user_id=user_id,
            context=context,
            target_chat_id=chat_id,
        )

        if not file_id:
            try:
                await callback_query.message.edit_caption(
                    caption=(
                        f"❌ <b>Не вдалося завантажити трек:</b>\n"
                        f"🎵 {html.escape(artist)} — {html.escape(title)}"
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=_build_download_keyboard(deezer_id, duration),
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

        # Видаляємо 30-секундне голосове прев'ю, щоб у чаті залишився лише чистий повний MP3
        try:
            await callback_query.message.delete()
        except Exception:
            pass
        return

    # Сценарій 2: Кнопку натиснуто в Інлайн-повідомленні
    file_id, _ = await _ensure_full_mp3_file_id(
        deezer_id=deezer_id,
        track=track,
        user_id=user_id,
        context=context,
        target_chat_id=None,
    )

    if not file_id:
        bot_username = context.bot.username or (await context.bot.get_me()).username
        deep_link = f"https://t.me/{bot_username}?start=track_{deezer_id}"
        try:
            await context.bot.edit_message_caption(
                inline_message_id=callback_query.inline_message_id,
                caption=(
                    f"⚠️ <b>Щоб завантажити новий трек:</b>\n"
                    f"Натисніть кнопку нижче (відкриє бота і одразу надішле вам "
                    f"<b>{html.escape(artist)} — {html.escape(title)}</b>):"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🎵 Відкрити та скачати MP3", url=deep_link)]]
                ),
            )
        except Exception as e:
            logger.debug(f"Could not set fallback deep-link caption: {e}")
        return

    # Замінюємо 30-секундне прев'ю на повний MP3 прямо в чаті
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
        # Якщо Telegram клієнт не дозволяє замінити Voice-повідомлення на Audio на місці,
        # надсилаємо повний MP3 користувачу і оновлюємо підпис голосового з прямою кнопкою!
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
                    f"✅ <b>Повний MP3 готовий!</b> Натисніть кнопку нижче або викличте "
                    f"<code>@{html.escape(bot_username)} {html.escape(title)}</code>:"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("📥 Отримати повний MP3", url=deep_link)]]
                ),
            )
        except Exception:
            pass
    except TelegramError as e:
        logger.error(f"Failed to edit inline message media: {e}", exc_info=True)


# === Global Error Handler ===
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Unhandled error: {context.error}", exc_info=context.error)


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
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(InlineQueryHandler(inline_query_handler))
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
