"""
Telegram Inline Music Bot.
"""

import os
import logging
from uuid import uuid4
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineQueryResultArticle,
    InlineQueryResultAudio,
    InlineQueryResultCachedAudio,
    InputTextMessageContent,
    InputMediaAudio,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    InlineQueryHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from music_search import search_tracks, format_duration
from music_download import download_track, cleanup_file, cleanup_temp_dir
from cache_db import CacheDB

# === Configuration ===
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Ініціалізація SQLite кешу
cache = CacheDB()


# === Command /start ===
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вітальне повідомлення та інструкція."""
    bot_name = context.bot.username
    await update.message.reply_text(
        "🎵 *Welcome to Music Explorer Bot!*\n\n"
        "Search & download high-quality MP3 tracks in any chat seamlessly!\n\n"
        "✨ *How to Use:*\n"
        f"1. Type `@{bot_name} <song title>` in any chat or group.\n"
        "2. Select a track from the inline menu.\n"
        "3. Listen to the 30-second audio preview spoiler.\n"
        "4. Click *«📥 Download Full MP3»* to get the full track directly in that chat!\n\n"
        "🔍 *Examples:*\n"
        f"• `@{bot_name} Imagine Dragons Believer`\n"
        f"• `@{bot_name} Queen Bohemian Rhapsody`\n\n"
        "⚡ *Fast Delivery:* Cached tracks arrive instantly!",
        parse_mode="Markdown",
    )


# === Inline Query Handler ===
async def inline_query_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Обробник інлайн-пошуку (@bot_name запит)."""
    query = update.inline_query.query.strip()

    if not query or len(query) < 2:
        return

    logger.info(f"Inline query: '{query}' from user {update.inline_query.from_user.id}")

    tracks = await search_tracks(query, limit=10)

    if not tracks:
        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="❌ No Results Found",
                description=f"No music matches found for '{query}'",
                input_message_content=InputTextMessageContent(
                    f"🔍 *No results found for:* `{query}`\nTry a different search term.",
                    parse_mode="Markdown",
                ),
            )
        ]
        await update.inline_query.answer(results, cache_time=10)
        return

    results = []
    for track in tracks:
        duration_str = format_duration(track["duration"])
        cached_file_id = cache.get_file_id(track["id"])

        # Зберігаємо метадані треку в пам'ять бота
        context.application.bot_data[str(track["id"])] = {
            "artist": track["artist"],
            "title": track["title"],
            "album": track["album"],
        }

        if cached_file_id:
            # Трек вже в кеші — надсилаємо повне аудіо
            result = InlineQueryResultCachedAudio(
                id=str(track["id"]),
                audio_file_id=cached_file_id,
                caption=f"🎵 *{track['artist']} — {track['title']}*\n⚡ *Quality:* 192 kbps MP3",
                parse_mode="Markdown",
            )
        else:
            # Новий трек — надсилаємо 30-секундну прев'ю-картку з кнопкою
            audio_url = track.get("preview_url") or "https://raw.githubusercontent.com/anars/blank-audio/master/1-second-of-silence.mp3"
            result = InlineQueryResultAudio(
                id=str(track["id"]),
                audio_url=audio_url,
                title=f"🎵 {track['title']}",
                performer=track["artist"],
                caption=f"🎧 *{track['artist']} — {track['title']}*\n"
                        f"⏱ *Audio Preview:* 30 sec (Full track: {duration_str})\n\n"
                        f"👇 Click below to download the full MP3 into this chat:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "📥 Download Full MP3 (192 kbps)",
                            callback_data=f"dl:{track['id']}",
                        )
                    ]
                ]),
            )
        results.append(result)

    await update.inline_query.answer(results, cache_time=30)


# === Callback Query Handler (Download Button) ===
async def download_callback_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Обробник натискання кнопки завантаження."""
    callback_query = update.callback_query
    await callback_query.answer("⏳ Downloading full MP3 into this chat...")

    data = callback_query.data
    if not data or not data.startswith("dl:"):
        return

    parts = data.split(":")
    if len(parts) < 2:
        await callback_query.answer("❌ Invalid request data", show_alert=True)
        return

    deezer_id_str = parts[1]
    deezer_id = int(deezer_id_str)

    track_meta = context.application.bot_data.get(deezer_id_str, {})
    artist = track_meta.get("artist", "Unknown Artist")
    title = track_meta.get("title", "Unknown Title")
    album = track_meta.get("album", "")

    is_inline = callback_query.inline_message_id is not None
    user_id = callback_query.from_user.id

    logger.info(f"Download request: {artist} - {title} (deezer_id={deezer_id}, inline={is_inline})")

    # Зміна опису на "Завантажується..."
    if is_inline:
        try:
            await context.bot.edit_message_caption(
                inline_message_id=callback_query.inline_message_id,
                caption=f"⏳ *Downloading Full MP3...*\n"
                        f"🎵 *{title}*\n"
                        f"👤 *{artist}*\n\n"
                        f"Please wait a few seconds...",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning(f"Could not update caption in inline msg: {e}")

    # 1. Перевірка кешу SQLite
    cached_file_id = cache.get_file_id(deezer_id)
    if cached_file_id:
        try:
            if is_inline:
                await context.bot.edit_message_media(
                    inline_message_id=callback_query.inline_message_id,
                    media=InputMediaAudio(
                        media=cached_file_id,
                        title=title,
                        performer=artist,
                        caption=f"🎵 *{artist} — {title}*\n⚡ *Quality:* 192 kbps MP3",
                        parse_mode="Markdown",
                    ),
                )
            else:
                await context.bot.send_audio(
                    chat_id=callback_query.message.chat_id,
                    audio=cached_file_id,
                    title=title,
                    performer=artist,
                )
            return
        except Exception as e:
            logger.warning(f"Cache expired or failed: {e}")

    # 2. Завантаження треку
    file_path = None
    try:
        file_path = await download_track(artist, title)

        if not file_path or not os.path.exists(file_path):
            if is_inline:
                try:
                    await context.bot.edit_message_caption(
                        inline_message_id=callback_query.inline_message_id,
                        caption=f"❌ *Could not download track:*\n🎵 {artist} - {title}",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
            return

        # 3. Відправка у приватні повідомлення для отримання file_id
        file_id = None
        try:
            with open(file_path, "rb") as audio_file:
                sent = await context.bot.send_audio(
                    chat_id=user_id,
                    audio=audio_file,
                    title=title,
                    performer=artist,
                    caption=f"🎵 {artist} - {title}",
                    disable_notification=True,
                    read_timeout=120,
                    write_timeout=120,
                )
                if sent and sent.audio:
                    file_id = sent.audio.file_id
                    cache.save_file_id(deezer_id, title, artist, file_id)

                    try:
                        await sent.delete()
                    except Exception as del_err:
                        logger.debug(f"Could not delete temporary DM msg: {del_err}")

        except Exception as upload_err:
            logger.warning(f"Could not upload audio for file_id: {upload_err}")

        # 4. Заміна 30s прев'ю в чаті на повне MP3
        if is_inline and file_id:
            try:
                caption_text = f"🎵 *{artist} — {title}*"
                if album:
                    caption_text += f"\n💿 *Album:* {album}"
                caption_text += "\n⚡ *Quality:* 192 kbps MP3"

                await context.bot.edit_message_media(
                    inline_message_id=callback_query.inline_message_id,
                    media=InputMediaAudio(
                        media=file_id,
                        title=title,
                        performer=artist,
                        caption=caption_text,
                        parse_mode="Markdown",
                    ),
                )
            except Exception as e:
                logger.error(f"Failed to edit inline message with file_id: {e}", exc_info=True)

        elif is_inline and not file_id:
            try:
                bot_username = (await context.bot.get_me()).username
                await context.bot.edit_message_caption(
                    inline_message_id=callback_query.inline_message_id,
                    caption=f"⚠️ *Setup Required:*\n"
                            f"To download new songs in group chats, please click /start once in PM: @{bot_username}",
                    parse_mode="Markdown",
                )
            except Exception:
                pass

    except Exception as e:
        logger.error(f"Error processing download: {e}", exc_info=True)

    finally:
        if file_path:
            cleanup_file(file_path)


# === Global Error Handler ===
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Unhandled error: {context.error}", exc_info=context.error)


# === Main Application Entry Point ===
def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not found in .env file!")

    logger.info("Starting Music Bot...")
    cleanup_temp_dir()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(InlineQueryHandler(inline_query_handler))
    app.add_handler(CallbackQueryHandler(download_callback_handler, pattern=r"^dl:"))
    app.add_error_handler(error_handler)

    logger.info("Bot is starting polling...")

    # drop_pending_updates=True скидає застряглі сесії Telegram та усуває 409 Conflict
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "inline_query", "callback_query"],
        close_loop=False
    )


if __name__ == "__main__":
    import asyncio

    # Явна ініціалізація Event Loop для сумісності з Python 3.14+
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped cleanly.")
