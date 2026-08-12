"""Windows Start Menu application indexer and launcher.

Scans the machine-wide and per-user Start Menu folders, resolves ``.lnk``
shortcut targets (best-effort binary parse; falls back to the shortcut path
itself, which ``os.startfile`` handles natively), and persists an ``apps``
table in a tiny local SQLite database for instant fuzzy lookup.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import struct
from pathlib import Path

log = logging.getLogger(__name__)

APP_NAME_CLEAN = re.compile(r"[\s\(\)]+")


def _default_start_menu_dirs() -> list[Path]:
    dirs: list[Path] = []
    machine = os.environ.get("ProgramData")
    if machine:
        dirs.append(Path(machine) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    appdata = os.environ.get("APPDATA")
    if appdata:
        dirs.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return [p for p in dirs if p.is_dir()]


def _resolve_lnk_target(data: bytes) -> str | None:
    """Extract the file target from a binary ``.lnk`` shortcut.

    Walks the Shell link's ``LinkInfo`` block (flags: local + common suffix)
    and decodes the ANSI ``LocalBasePath`` string. Returns ``None`` for
    network / registry shortcuts or non-Windows links.
    """
    if len(data) < 76:
        return None
    signature = data[:20]
    if signature != b"\x4c\x00\x00\x00\x01\x14\x02\x00\x00\x00\x00\x00\xc0\x00\x00\x00\x00\x00\x00\x46":
        return None

    idx = data.find(b"\x1c\x00\x00\x00", 76)
    if idx < 0:
        return None
    link_info = idx
    if idx + 0x1C > len(data):
        return None
    flags = struct.unpack_from("<I", data, link_info + 0x08)[0]
    local_base_offset = struct.unpack_from("<I", data, link_info + 0x10)[0]
    common_offset = struct.unpack_from("<I", data, link_info + 0x18)[0]
    if common_offset == 0xFFFFFFFF or common_offset == 0:
        return None
    offset = local_base_offset
    if not offset:
        return None
    start = link_info + offset
    if flags & 0x02:  # IsUnicode -> UTF-16 path follows the ANSI path
        end = data.find(b"\x00\x00", start)
        if end < 0:
            return None
        raw = data[start:end]
        try:
            path = raw.decode("utf-16-le")
        except UnicodeDecodeError:
            return None
    else:
        end = data.find(b"\x00", start)
        if end < 0:
            return None
        raw = data[start:end]
        try:
            path = raw.decode("cp1252")
        except UnicodeDecodeError:
            return None
    if not path or not path.strip():
        return None
    return path.strip()


class AppIndexer:
    """Indexes Start Menu shortcuts into ``apps`` and launches them."""

    def __init__(self, db_path: Path | str, start_menu_dirs: list[Path] | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS apps (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                path TEXT NOT NULL,
                source TEXT,
                updated_at REAL
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_apps_name ON apps(name)")
        self._conn.commit()
        self.start_menu_dirs = list(start_menu_dirs) if start_menu_dirs is not None else _default_start_menu_dirs()
        self._load_index()

    # -- helpers ----------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def _load_index(self) -> None:
        rows = self._conn.execute("SELECT name, path FROM apps").fetchall()
        self._name_index: dict[str, str] = {}
        for name, path in rows:
            self._name_index[(name or "").casefold()] = path

    def clean_name(self, stem: str) -> str:
        return APP_NAME_CLEAN.sub(" ", stem).strip().title()

    def _remaining(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM apps").fetchone()[0]

    # -- adding / scanning -------------------------------------------------

    def add_app(self, name: str, path: Path | str, source: str | None = None) -> int:
        display = name.strip()
        if not display or not str(path).strip():
            return 0
        now = os.path.getmtime(str(path)) if Path(str(path)).exists() else 0.0
        self._conn.execute(
            """
            INSERT OR REPLACE INTO apps (name, path, source, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (display, str(path), source, now),
        )
        self._conn.commit()
        self._name_index[display.casefold()] = str(path)
        return 1

    def scan(self, force: bool = False) -> int:
        """Walk every Start Menu folder and index ``.lnk`` shortcuts.

        Returns the number of apps added (0 when the cache is already warm
        and ``force`` is False, so startup stays cheap).
        """
        if self._remaining() > 0 and not force:
            return 0
        added = 0
        for root in self.start_menu_dirs:
            for lnk in Path(root).rglob("*.lnk"):
                try:
                    target = self._resolve_target(lnk)
                    name = self.clean_name(lnk.stem)
                    self.add_app(name, target, str(lnk))
                    added += 1
                except OSError as exc:
                    log.debug("skip shortcut %s: %s", lnk, exc)
        if added:
            log.info("indexed %d apps from the Start Menu", added)
        return added

    def _resolve_target(self, lnk: Path) -> Path:
        try:
            data = lnk.read_bytes()
        except OSError:
            data = b""
        target = _resolve_lnk_target(data) if data else None
        resolved = Path(target).resolve() if target and Path(target).exists() else lnk
        return lnk if not target else resolved

    # -- lookups ------------------------------------------------------------

    def search(self, query: str, limit: int = 20) -> list[dict]:
        needle = (query or "").strip()
        if not needle:
            return self.list_apps(limit=limit)
        pattern = f"%{needle}%"
        rows = self._conn.execute(
            """
            SELECT name, path, source, updated_at FROM apps
            WHERE name LIKE ? COLLATE NOCASE
            ORDER BY (name LIKE ? COLLATE NOCASE) DESC, name
            LIMIT ?
            """,
            (pattern, f"{needle}%", limit),
        ).fetchall()
        return [
            {"name": name, "path": path, "source": source, "updated_at": updated_at}
            for name, path, source, updated_at in rows
        ]

    def list_apps(self, limit: int = 200) -> list[dict]:
        rows = self._conn.execute(
            "SELECT name, path, source, updated_at FROM apps ORDER BY name LIMIT ?", (limit,)
        ).fetchall()
        return [
            {"name": name, "path": path, "source": source, "updated_at": updated_at}
            for name, path, source, updated_at in rows
        ]

    def find_app(self, query: str) -> dict | None:
        needle = (query or "").casefold().strip()
        if not needle:
            return None
        if needle in self._name_index:
            path = self._name_index[needle]
            return {"name": query, "path": path}
        for name_cf, path in self._name_index.items():
            if name_cf.startswith(needle):
                return {"name": name_cf, "path": path}
        for name_cf, path in self._name_index.items():
            if needle in name_cf:
                return {"name": name_cf, "path": path}
        return None

    # -- launching ----------------------------------------------------------

    def launch_app(self, name: str) -> str:
        """Launch the best matching installed app. Returns a JSON result string."""
        found = self.find_app(name)
        if found is None:
            suggestions = [row["name"] for row in self.search(name, limit=5)]
            for word in re.split(r"\s+", (name or "").strip()):
                if len(suggestions) >= 5:
                    break
                for row in self.search(word, limit=5):
                    if row["name"] not in suggestions:
                        suggestions.append(row["name"])
            return json.dumps(
                {
                    "ok": False,
                    "name": name,
                    "message": "No installed application matched",
                    "suggestions": suggestions[:5],
                },
                ensure_ascii=False,
            )
        try:
            os.startfile(found["path"])  # type: ignore[attr-defined]
        except (OSError, AttributeError) as exc:
            return json.dumps(
                {"ok": False, "name": found["name"], "message": str(exc)},
                ensure_ascii=False,
            )
        log.info("launched app %r (%s)", found["name"], found["path"])
        return json.dumps(
            {"ok": True, "name": found["name"], "path": found["path"]},
            ensure_ascii=False,
        )