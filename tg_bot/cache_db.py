"""
Модуль кешування file_id через SQLite.
Зберігає Telegram file_id для вже відправлених треків,
щоб не завантажувати повторно.
"""

import sqlite3
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.db")


class CacheDB:
    """SQLite кеш для зберігання file_id відправлених треків."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Створює таблицю, якщо не існує."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS track_cache (
                        deezer_id INTEGER PRIMARY KEY,
                        title TEXT,
                        artist TEXT,
                        file_id TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.commit()
                logger.info("Cache DB initialized")
        except sqlite3.Error as e:
            logger.error(f"Failed to initialize cache DB: {e}")

    def get_file_id(self, deezer_id: int) -> Optional[str]:
        """
        Отримує file_id з кешу за Deezer ID.
        
        Args:
            deezer_id: ID треку в Deezer
            
        Returns:
            Telegram file_id або None
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT file_id FROM track_cache WHERE deezer_id = ?",
                    (deezer_id,),
                )
                row = cursor.fetchone()
                if row:
                    logger.info(f"Cache hit for deezer_id={deezer_id}")
                    return row[0]
                return None
        except sqlite3.Error as e:
            logger.error(f"Cache read error: {e}")
            return None

    def save_file_id(
        self, deezer_id: int, title: str, artist: str, file_id: str
    ):
        """
        Зберігає file_id у кеш.
        
        Args:
            deezer_id: ID треку в Deezer
            title: Назва пісні
            artist: Ім'я артиста
            file_id: Telegram file_id
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO track_cache 
                    (deezer_id, title, artist, file_id)
                    VALUES (?, ?, ?, ?)
                    """,
                    (deezer_id, title, artist, file_id),
                )
                conn.commit()
                logger.info(
                    f"Cached file_id for: {artist} - {title} "
                    f"(deezer_id={deezer_id})"
                )
        except sqlite3.Error as e:
            logger.error(f"Cache write error: {e}")

    def get_cache_size(self) -> int:
        """Повертає кількість записів у кеші."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("SELECT COUNT(*) FROM track_cache")
                return cursor.fetchone()[0]
        except sqlite3.Error:
            return 0
