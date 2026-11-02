"""
Модуль кешування метаданих треків та Telegram file_id через SQLite.
Зберігає метадані треків з Deezer та file_id вже відправлених MP3,
щоб не завантажувати повторно і не втрачати назви пісень після рестарту бота.
"""

import sqlite3
import logging
import os
from contextlib import closing
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.db")


class CacheDB:
    """SQLite кеш для зберігання метаданих треків та file_id."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Створює таблиці та виконує безпечну міграцію старої схеми."""
        try:
            with closing(sqlite3.connect(self.db_path)) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS track_cache (
                        deezer_id INTEGER PRIMARY KEY,
                        title TEXT,
                        artist TEXT,
                        file_id TEXT,
                        album TEXT DEFAULT '',
                        duration INTEGER DEFAULT 0,
                        cover_url TEXT DEFAULT '',
                        preview_url TEXT DEFAULT '',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                conn.execute("""
                    CREATE TABLE IF NOT EXISTS track_meta (
                        deezer_id INTEGER PRIMARY KEY,
                        title TEXT NOT NULL,
                        artist TEXT NOT NULL,
                        album TEXT DEFAULT '',
                        duration INTEGER DEFAULT 0,
                        cover_url TEXT DEFAULT '',
                        preview_url TEXT DEFAULT '',
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                cursor = conn.execute("PRAGMA table_info(track_cache)")
                existing_cols = {row[1] for row in cursor.fetchall()}
                migrations = {
                    "album": "ALTER TABLE track_cache ADD COLUMN album TEXT DEFAULT ''",
                    "duration": "ALTER TABLE track_cache ADD COLUMN duration INTEGER DEFAULT 0",
                    "cover_url": "ALTER TABLE track_cache ADD COLUMN cover_url TEXT DEFAULT ''",
                    "preview_url": "ALTER TABLE track_cache ADD COLUMN preview_url TEXT DEFAULT ''",
                }
                for col, sql in migrations.items():
                    if col not in existing_cols:
                        try:
                            conn.execute(sql)
                        except sqlite3.Error:
                            pass

                conn.commit()
                logger.info("Cache DB initialized successfully")
        except sqlite3.Error as e:
            logger.error(f"Failed to initialize cache DB: {e}")

    def save_track_meta(self, track: dict):
        """Зберігає метадані треку з результатів пошуку Deezer."""
        deezer_id = track.get("id")
        if not deezer_id:
            return
        try:
            with closing(sqlite3.connect(self.db_path)) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO track_meta
                    (deezer_id, title, artist, album, duration, cover_url, preview_url, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        int(deezer_id),
                        track.get("title", "Unknown Title"),
                        track.get("artist", "Unknown Artist"),
                        track.get("album", ""),
                        int(track.get("duration") or 0),
                        track.get("cover_url", ""),
                        track.get("preview_url", ""),
                    ),
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Failed to save track meta for {deezer_id}: {e}")

    def get_track_meta(self, deezer_id: int) -> Optional[dict]:
        """Отримує метадані треку з локальної БД."""
        try:
            with closing(sqlite3.connect(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT * FROM track_meta WHERE deezer_id = ?",
                    (deezer_id,),
                )
                row = cursor.fetchone()
                if row:
                    return {
                        "id": row["deezer_id"],
                        "title": row["title"],
                        "artist": row["artist"],
                        "album": row["album"] or "",
                        "duration": row["duration"] or 0,
                        "cover_url": row["cover_url"] or "",
                        "preview_url": row["preview_url"] or "",
                    }

                cursor = conn.execute(
                    "SELECT * FROM track_cache WHERE deezer_id = ?",
                    (deezer_id,),
                )
                row = cursor.fetchone()
                if row:
                    keys = row.keys()
                    return {
                        "id": row["deezer_id"],
                        "title": row["title"] or "Unknown Title",
                        "artist": row["artist"] or "Unknown Artist",
                        "album": row["album"] if "album" in keys and row["album"] else "",
                        "duration": row["duration"] if "duration" in keys and row["duration"] else 0,
                        "cover_url": row["cover_url"] if "cover_url" in keys and row["cover_url"] else "",
                        "preview_url": row["preview_url"] if "preview_url" in keys and row["preview_url"] else "",
                    }
                return None
        except sqlite3.Error as e:
            logger.error(f"Error reading track meta for {deezer_id}: {e}")
            return None

    def get_file_id(self, deezer_id: int) -> Optional[str]:
        """Отримує Telegram file_id з кешу за Deezer ID."""
        try:
            with closing(sqlite3.connect(self.db_path)) as conn:
                cursor = conn.execute(
                    "SELECT file_id FROM track_cache WHERE deezer_id = ? AND file_id IS NOT NULL AND file_id != ''",
                    (deezer_id,),
                )
                row = cursor.fetchone()
                if row and row[0]:
                    logger.info(f"Cache hit for deezer_id={deezer_id}")
                    return row[0]
                return None
        except sqlite3.Error as e:
            logger.error(f"Cache read error: {e}")
            return None

    def save_file_id(
        self,
        deezer_id: int,
        title: str,
        artist: str,
        file_id: str,
        album: str = "",
        duration: int = 0,
        cover_url: str = "",
    ):
        """Зберігає file_id завантаженого MP3 у кеш."""
        try:
            with closing(sqlite3.connect(self.db_path)) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO track_cache
                    (deezer_id, title, artist, file_id, album, duration, cover_url)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (deezer_id, title, artist, file_id, album, duration, cover_url),
                )
                conn.commit()
                logger.info(
                    f"Cached file_id for: {artist} - {title} (deezer_id={deezer_id})"
                )
        except sqlite3.Error as e:
            logger.error(f"Cache write error: {e}")

    def delete_file_id(self, deezer_id: int):
        """Видаляє недійсний або застарілий file_id з кешу."""
        try:
            with closing(sqlite3.connect(self.db_path)) as conn:
                conn.execute(
                    "DELETE FROM track_cache WHERE deezer_id = ?",
                    (deezer_id,),
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Cache delete error: {e}")

    def get_cache_size(self) -> int:
        """Повертає кількість закешованих MP3 треків."""
        try:
            with closing(sqlite3.connect(self.db_path)) as conn:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM track_cache WHERE file_id IS NOT NULL AND file_id != ''"
                )
                return cursor.fetchone()[0]
        except sqlite3.Error:
            return 0
