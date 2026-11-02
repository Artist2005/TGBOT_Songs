"""
Модуль кешування метаданих треків, Telegram file_id та мовних налаштувань через SQLite + RAM-кеш.
Використовує WAL-режим та оперативний кеш у пам'яті для миттєвого доступу (0 мс затримки при пошуку).
"""

import sqlite3
import logging
import os
from contextlib import closing
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.db")


class CacheDB:
    """Швидкий SQLite + in-memory кеш для метаданих треків, file_id та мови користувача."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._file_ids: dict[int, str] = {}
        self._meta: dict[int, dict] = {}
        self._langs: dict[int, str] = {}
        self._init_db()
        self._preload_memory_cache()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self):
        """Створює таблиці та виконує безпечну міграцію старої схеми."""
        try:
            with closing(self._connect()) as conn:
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

                conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_prefs (
                        user_id INTEGER PRIMARY KEY,
                        lang TEXT NOT NULL DEFAULT 'uk',
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
                logger.info("Cache DB initialized successfully (WAL mode)")
        except sqlite3.Error as e:
            logger.error(f"Failed to initialize cache DB: {e}")

    def _preload_memory_cache(self):
        """Завантажує file_id, останні метадані та мовні налаштування в оперативну пам'ять."""
        try:
            with closing(self._connect()) as conn:
                conn.row_factory = sqlite3.Row

                for row in conn.execute(
                    "SELECT deezer_id, title, artist, file_id, album, duration, cover_url, preview_url "
                    "FROM track_cache WHERE file_id IS NOT NULL AND file_id != ''"
                ):
                    did = int(row["deezer_id"])
                    self._file_ids[did] = row["file_id"]
                    self._meta[did] = {
                        "id": did,
                        "title": row["title"] or "Unknown Title",
                        "artist": row["artist"] or "Unknown Artist",
                        "album": row["album"] or "",
                        "duration": int(row["duration"] or 0),
                        "cover_url": row["cover_url"] or "",
                        "preview_url": row["preview_url"] or "",
                    }

                for row in conn.execute(
                    "SELECT deezer_id, title, artist, album, duration, cover_url, preview_url "
                    "FROM track_meta ORDER BY updated_at DESC LIMIT 2000"
                ):
                    did = int(row["deezer_id"])
                    self._meta[did] = {
                        "id": did,
                        "title": row["title"] or "Unknown Title",
                        "artist": row["artist"] or "Unknown Artist",
                        "album": row["album"] or "",
                        "duration": int(row["duration"] or 0),
                        "cover_url": row["cover_url"] or "",
                        "preview_url": row["preview_url"] or "",
                    }

                for row in conn.execute("SELECT user_id, lang FROM user_prefs"):
                    self._langs[int(row["user_id"])] = row["lang"]

                logger.info(
                    f"Preloaded {len(self._file_ids)} cached files and {len(self._meta)} tracks into RAM"
                )
        except sqlite3.Error as e:
            logger.warning(f"Failed to preload memory cache: {e}")

    def save_track_meta(self, track: dict):
        """Зберігає метадані одного треку в RAM та SQLite."""
        self.save_tracks_meta_batch([track])

    def save_tracks_meta_batch(self, tracks: list[dict]):
        """Пакетно зберігає метадані списку треків за одну транзакцію SQLite."""
        rows = []
        for track in tracks:
            deezer_id = track.get("id")
            if not deezer_id:
                continue
            did = int(deezer_id)
            normalized = {
                "id": did,
                "title": track.get("title", "Unknown Title"),
                "artist": track.get("artist", "Unknown Artist"),
                "album": track.get("album", ""),
                "duration": int(track.get("duration") or 0),
                "cover_url": track.get("cover_url", ""),
                "preview_url": track.get("preview_url", ""),
            }
            self._meta[did] = normalized
            rows.append(
                (
                    did,
                    normalized["title"],
                    normalized["artist"],
                    normalized["album"],
                    normalized["duration"],
                    normalized["cover_url"],
                    normalized["preview_url"],
                )
            )

        if not rows:
            return

        try:
            with closing(self._connect()) as conn:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO track_meta
                    (deezer_id, title, artist, album, duration, cover_url, preview_url, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    rows,
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Failed to batch save track meta: {e}")

    def get_track_meta(self, deezer_id: int) -> Optional[dict]:
        """Миттєво отримує метадані треку з RAM або локальної БД."""
        deezer_id = int(deezer_id)
        if deezer_id in self._meta:
            return self._meta[deezer_id]

        try:
            with closing(self._connect()) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT * FROM track_meta WHERE deezer_id = ?",
                    (deezer_id,),
                )
                row = cursor.fetchone()
                if row:
                    meta = {
                        "id": row["deezer_id"],
                        "title": row["title"],
                        "artist": row["artist"],
                        "album": row["album"] or "",
                        "duration": row["duration"] or 0,
                        "cover_url": row["cover_url"] or "",
                        "preview_url": row["preview_url"] or "",
                    }
                    self._meta[deezer_id] = meta
                    return meta

                cursor = conn.execute(
                    "SELECT * FROM track_cache WHERE deezer_id = ?",
                    (deezer_id,),
                )
                row = cursor.fetchone()
                if row:
                    keys = row.keys()
                    meta = {
                        "id": row["deezer_id"],
                        "title": row["title"] or "Unknown Title",
                        "artist": row["artist"] or "Unknown Artist",
                        "album": row["album"] if "album" in keys and row["album"] else "",
                        "duration": row["duration"] if "duration" in keys and row["duration"] else 0,
                        "cover_url": row["cover_url"] if "cover_url" in keys and row["cover_url"] else "",
                        "preview_url": row["preview_url"] if "preview_url" in keys and row["preview_url"] else "",
                    }
                    self._meta[deezer_id] = meta
                    return meta
                return None
        except sqlite3.Error as e:
            logger.error(f"Error reading track meta for {deezer_id}: {e}")
            return None

    def get_file_id(self, deezer_id: int) -> Optional[str]:
        """Миттєво отримує Telegram file_id з RAM-кешу (або SQLite)."""
        deezer_id = int(deezer_id)
        if deezer_id in self._file_ids:
            return self._file_ids[deezer_id]

        try:
            with closing(self._connect()) as conn:
                cursor = conn.execute(
                    "SELECT file_id FROM track_cache WHERE deezer_id = ? AND file_id IS NOT NULL AND file_id != ''",
                    (deezer_id,),
                )
                row = cursor.fetchone()
                if row and row[0]:
                    self._file_ids[deezer_id] = row[0]
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
        """Зберігає file_id завантаженого MP3 у RAM та SQLite."""
        deezer_id = int(deezer_id)
        self._file_ids[deezer_id] = file_id
        try:
            with closing(self._connect()) as conn:
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
        """Видаляє недійсний або застарілий file_id з RAM та SQLite."""
        deezer_id = int(deezer_id)
        self._file_ids.pop(deezer_id, None)
        try:
            with closing(self._connect()) as conn:
                conn.execute(
                    "DELETE FROM track_cache WHERE deezer_id = ?",
                    (deezer_id,),
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Cache delete error: {e}")

    def get_user_lang(self, user_id: int) -> Optional[str]:
        """Повертає збережену мову користувача ('uk' або 'en') або None."""
        user_id = int(user_id)
        if user_id in self._langs:
            return self._langs[user_id]
        try:
            with closing(self._connect()) as conn:
                cursor = conn.execute(
                    "SELECT lang FROM user_prefs WHERE user_id = ?",
                    (user_id,),
                )
                row = cursor.fetchone()
                if row and row[0] in ("uk", "en"):
                    self._langs[user_id] = row[0]
                    return row[0]
        except sqlite3.Error as e:
            logger.debug(f"Could not read user lang for {user_id}: {e}")
        return None

    def set_user_lang(self, user_id: int, lang: str):
        """Зберігає вибрану мову користувача ('uk' або 'en') у RAM та SQLite."""
        user_id = int(user_id)
        lang = "en" if lang == "en" else "uk"
        self._langs[user_id] = lang
        try:
            with closing(self._connect()) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO user_prefs (user_id, lang, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    """,
                    (user_id, lang),
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Failed to save user lang for {user_id}: {e}")

    def get_cache_size(self) -> int:
        """Повертає кількість закешованих MP3 треків."""
        return len(self._file_ids)
