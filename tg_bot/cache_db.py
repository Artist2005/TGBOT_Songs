"""
Модуль кешування метаданих треків, Telegram file_id, вибраного, аудіо-ефектів
та мовних налаштувань через SQLite + RAM-кеш.
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
    """Швидкий SQLite + in-memory кеш для метаданих треків, file_id, ефектів, вибраного та мови."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._file_ids: dict[int, str] = {}
        self._meta: dict[int, dict] = {}
        self._langs: dict[int, str] = {}
        self._favorites: dict[int, list[int]] = {}
        self._effects: dict[tuple[int, str], str] = {}
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

                conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_favorites (
                        user_id INTEGER NOT NULL,
                        deezer_id INTEGER NOT NULL,
                        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (user_id, deezer_id)
                    )
                """)

                conn.execute("""
                    CREATE TABLE IF NOT EXISTS effect_cache (
                        deezer_id INTEGER NOT NULL,
                        effect TEXT NOT NULL,
                        file_id TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (deezer_id, effect)
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
        """Завантажує file_id, останні метадані, вибране, ефекти та мовні налаштування в RAM."""
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

                for row in conn.execute(
                    "SELECT user_id, deezer_id FROM user_favorites ORDER BY added_at DESC"
                ):
                    uid = int(row["user_id"])
                    did = int(row["deezer_id"])
                    self._favorites.setdefault(uid, [])
                    if did not in self._favorites[uid]:
                        self._favorites[uid].append(did)

                for row in conn.execute(
                    "SELECT deezer_id, effect, file_id FROM effect_cache"
                ):
                    self._effects[(int(row["deezer_id"]), str(row["effect"]))] = row["file_id"]

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
            if deezer_id is None:
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
        if deezer_id not in self._meta:
            self._meta[deezer_id] = {
                "id": deezer_id,
                "title": title or "Unknown Title",
                "artist": artist or "Unknown Artist",
                "album": album or "",
                "duration": int(duration or 0),
                "cover_url": cover_url or "",
                "preview_url": "",
            }
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

    # === Вибране (Favorites) ===
    def is_favorite(self, user_id: int, deezer_id: int) -> bool:
        """Перевіряє, чи є трек у вибраному користувача (миттєво з RAM)."""
        return int(deezer_id) in self._favorites.get(int(user_id), [])

    def toggle_favorite(self, user_id: int, deezer_id: int) -> bool:
        """
        Додає або видаляє трек із вибраного користувача.
        Повертає True, якщо трек додано, або False, якщо видалено.
        """
        uid = int(user_id)
        did = int(deezer_id)
        favs = self._favorites.setdefault(uid, [])

        if did in favs:
            favs.remove(did)
            try:
                with closing(self._connect()) as conn:
                    conn.execute(
                        "DELETE FROM user_favorites WHERE user_id = ? AND deezer_id = ?",
                        (uid, did),
                    )
                    conn.commit()
            except sqlite3.Error as e:
                logger.error(f"Failed to remove favorite ({uid}, {did}): {e}")
            return False
        else:
            favs.insert(0, did)
            try:
                with closing(self._connect()) as conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO user_favorites (user_id, deezer_id, added_at)
                        VALUES (?, ?, CURRENT_TIMESTAMP)
                        """,
                        (uid, did),
                    )
                    conn.commit()
            except sqlite3.Error as e:
                logger.error(f"Failed to add favorite ({uid}, {did}): {e}")
            return True

    def get_user_favorites(self, user_id: int, limit: int = 25) -> list[dict]:
        """Повертає список улюблених треків користувача з метаданими."""
        uid = int(user_id)
        fav_ids = self._favorites.get(uid, [])[:limit]
        tracks = []
        for did in fav_ids:
            meta = self.get_track_meta(did)
            if meta:
                tracks.append(meta)
        return tracks

    # === Кеш аудіо-ефектів (Slowed / Speed Up / Bass Boost) ===
    def get_effect_file_id(self, deezer_id: int, effect: str) -> Optional[str]:
        """Отримує закешований file_id для треку з аудіо-ефектом."""
        key = (int(deezer_id), str(effect))
        if key in self._effects:
            return self._effects[key]
        try:
            with closing(self._connect()) as conn:
                cursor = conn.execute(
                    "SELECT file_id FROM effect_cache WHERE deezer_id = ? AND effect = ?",
                    key,
                )
                row = cursor.fetchone()
                if row and row[0]:
                    self._effects[key] = row[0]
                    return row[0]
        except sqlite3.Error as e:
            logger.debug(f"Could not read effect cache {key}: {e}")
        return None

    def save_effect_file_id(self, deezer_id: int, effect: str, file_id: str):
        """Зберігає file_id для обробленого ефектом треку."""
        key = (int(deezer_id), str(effect))
        self._effects[key] = file_id
        try:
            with closing(self._connect()) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO effect_cache (deezer_id, effect, file_id, created_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (key[0], key[1], file_id),
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Failed to save effect cache {key}: {e}")

    # === Мовні налаштування ===
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

    def get_known_user_ids(self) -> list[int]:
        """Повертає список відомих user_id (для фонового отримання file_id в інлайн-режимі)."""
        return list(self._langs.keys())

    def get_cache_size(self) -> int:
        """Повертає кількість закешованих MP3 треків."""
        return len(self._file_ids)

    def get_admin_stats(self) -> dict:
        """Збирає повну статистику бази даних для адмін-панелі."""
        db_size_kb = 0.0
        try:
            if os.path.exists(self.db_path):
                db_size_kb = os.path.getsize(self.db_path) / 1024.0
        except Exception:
            pass

        recent_tracks = []
        total_favs = sum(len(v) for v in self._favorites.values())
        try:
            with closing(self._connect()) as conn:
                conn.row_factory = sqlite3.Row
                for row in conn.execute(
                    "SELECT artist, title, created_at FROM track_cache "
                    "WHERE file_id IS NOT NULL AND file_id != '' "
                    "ORDER BY created_at DESC LIMIT 5"
                ):
                    recent_tracks.append(f"{row['artist']} — {row['title']}")
        except sqlite3.Error as e:
            logger.debug(f"Admin stats query error: {e}")

        return {
            "users_count": len(self._langs),
            "cached_mp3_count": len(self._file_ids),
            "cached_effects_count": len(self._effects),
            "known_tracks_count": len(self._meta),
            "favorites_count": total_favs,
            "db_size_kb": round(db_size_kb, 1),
            "recent_tracks": recent_tracks,
        }

