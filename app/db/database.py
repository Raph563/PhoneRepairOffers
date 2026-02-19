from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional


class Database:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS favorites (
                    favorite_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    source_offer_id TEXT NOT NULL,
                    offer_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    UNIQUE(source, source_offer_id)
                );

                CREATE TABLE IF NOT EXISTS search_cache (
                    query_key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    fetched_at INTEGER NOT NULL
                );
                """
            )
            conn.commit()

    def get_cached_search(self, query_key: str, ttl_seconds: int) -> Optional[dict[str, Any]]:
        now_ts = int(time.time())
        min_ts = now_ts - max(1, ttl_seconds)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json, fetched_at FROM search_cache WHERE query_key = ?",
                (query_key,),
            ).fetchone()
            if not row:
                return None
            if int(row["fetched_at"]) < min_ts:
                return None
            return json.loads(row["payload_json"])

    def put_cached_search(self, query_key: str, payload: dict[str, Any]) -> None:
        now_ts = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO search_cache(query_key, payload_json, fetched_at)
                VALUES(?, ?, ?)
                ON CONFLICT(query_key) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    fetched_at = excluded.fetched_at
                """,
                (query_key, json.dumps(payload, ensure_ascii=False), now_ts),
            )
            conn.commit()

    def add_favorite(self, source: str, source_offer_id: str, offer_payload: dict[str, Any]) -> int:
        now_ts = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO favorites(source, source_offer_id, offer_json, created_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(source, source_offer_id) DO UPDATE SET
                    offer_json = excluded.offer_json
                """,
                (source, source_offer_id, json.dumps(offer_payload, ensure_ascii=False), now_ts),
            )
            conn.commit()
            row = conn.execute(
                "SELECT favorite_id FROM favorites WHERE source = ? AND source_offer_id = ?",
                (source, source_offer_id),
            ).fetchone()
            return int(row["favorite_id"])

    def delete_favorite(self, favorite_id: int) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM favorites WHERE favorite_id = ?", (favorite_id,))
            conn.commit()
            return cur.rowcount > 0

    def find_favorite_by_offer(self, source: str, source_offer_id: str) -> Optional[int]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT favorite_id FROM favorites WHERE source = ? AND source_offer_id = ?",
                (source, source_offer_id),
            ).fetchone()
            if not row:
                return None
            return int(row["favorite_id"])

    def list_favorites(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT favorite_id, offer_json, created_at FROM favorites ORDER BY created_at DESC"
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                result.append(
                    {
                        "favoriteId": int(row["favorite_id"]),
                        "createdAt": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(row["created_at"]))
                        ),
                        "offer": json.loads(row["offer_json"]),
                    }
                )
            return result
