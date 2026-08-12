"""SQLite-backed query history with recency + substring (fuzzy) search.

Uses the standard-library sqlite3 module; all access happens on the GUI
thread with short transactions.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    query      TEXT    NOT NULL,
    response   TEXT    NOT NULL,
    provider   TEXT    NOT NULL DEFAULT '',
    created_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_created ON history (created_at);
CREATE INDEX IF NOT EXISTS idx_history_query   ON history (query);
"""


class HistoryStore:
    def __init__(self, path: Path | str | None = None, limit: int = 500) -> None:
        self.path = Path(path) if path else Path(__file__).resolve().parent.parent / "history.db"
        self.limit = max(int(limit), 1)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=True)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- writes -----------------------------------------------------------

    def add(self, query: str, response: str, provider: str = "") -> int:
        query = (query or "").strip()
        if not query:
            return 0
        cursor = self._conn.execute(
            "INSERT INTO history (query, response, provider, created_at) VALUES (?, ?, ?, ?)",
            (query, response or "", provider or "", time.time()),
        )
        self._trim()
        self._conn.commit()
        return int(cursor.lastrowid)

    def _trim(self) -> None:
        count = self._conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
        if count > self.limit:
            self._conn.execute(
                "DELETE FROM history WHERE id IN ("
                "SELECT id FROM history ORDER BY created_at DESC LIMIT -1 OFFSET ?)",
                (self.limit,),
            )

    def clear(self) -> None:
        self._conn.execute("DELETE FROM history")
        self._conn.commit()

    # -- reads ------------------------------------------------------------

    def recent(self, limit: int = 25) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, query, provider, created_at FROM history "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (max(int(limit), 1),),
        ).fetchall()
        return [dict(row) for row in rows]

    def search(self, needle: str, limit: int = 12) -> list[dict[str, Any]]:
        """Case-insensitive substring match, ordered by last-used first."""
        needle = (needle or "").strip()
        if not needle:
            return self.recent(limit)
        pattern = f"%{needle}%"
        rows = self._conn.execute(
            "SELECT id, query, provider, created_at FROM history "
            "WHERE query LIKE ? COLLATE NOCASE "
            "ORDER BY instr(lower(query), lower(?)) ASC, created_at DESC, id DESC "
            "LIMIT ?",
            (pattern, needle, max(int(limit), 1)),
        ).fetchall()
        return [dict(row) for row in rows]

    def response_for(self, history_id: int) -> str | None:
        row = self._conn.execute(
            "SELECT response FROM history WHERE id = ?", (history_id,)
        ).fetchone()
        return row["response"] if row else None

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM history").fetchone()[0])

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass