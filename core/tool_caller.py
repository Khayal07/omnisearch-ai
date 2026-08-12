"""Local AI tool execution and dispatch.

Bridges the streaming engine's Function Calling definitions to concrete local
actions: app launching (Start Menu index), fast file search (SQLite index)
and file content reading (text / PDF / DOCX). Every handler returns a JSON
string so a model can consume the result as a ``tool`` message.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from core.app_indexer import AppIndexer
from core.file_reader import FileContentReader
from core.file_search import FileSearch

log = logging.getLogger(__name__)


class LocalToolError(Exception):
    """Raised when a tool request cannot be executed."""


class ToolCaller:
    """Executes the local tools exposed to the AI model."""

    def __init__(self, apps_db_path=None, files_db_path=None) -> None:
        self._app_indexer = AppIndexer(apps_db_path) if apps_db_path else None
        self._file_search = FileSearch(files_db_path) if files_db_path else None
        self._file_reader = FileContentReader()

    # -- warm-up ------------------------------------------------------------

    def scan_apps(self) -> None:
        """Populate the Start Menu app index (fast; skips when already warm)."""
        if self._app_indexer is not None:
            self._app_indexer.scan()

    def index_files(self) -> None:
        """Populate the local file index if it is still empty (may block)."""
        if self._file_search is not None:
            self._file_search.ensure_index()

    # -- individual tools ---------------------------------------------------

    def search_files(self, query: str, extension: str | None = None) -> str:
        if self._file_search is None:
            return self._error("search_files", "File search is not available")
        results = self._file_search.search(query, extension=extension, limit=10)
        return json.dumps(results, ensure_ascii=False)

    def launch_app(self, app_name: str) -> str:
        if self._app_indexer is None:
            return self._error("launch_app", "App launcher is not available")
        return self._app_indexer.launch_app(app_name)

    def read_file_content(self, file_path: str, max_chars: int = 6000) -> str:
        result = self._file_reader.read(file_path, max_chars=max_chars)
        return json.dumps(result, ensure_ascii=False)

    # -- dispatch -----------------------------------------------------------

    def _handlers(self) -> dict[str, Any]:
        return {
            "search_files": self.search_files,
            "launch_app": self.launch_app,
            "read_file_content": self.read_file_content,
        }

    @staticmethod
    def _error(name: str, message: str) -> str:
        return json.dumps({"ok": False, "tool": name, "message": message}, ensure_ascii=False)

    def execute(self, name: str, arguments: dict) -> str:
        """Run a single tool and return its JSON result as a string."""
        handler = self._handlers().get(name)
        if handler is None:
            return self._error(name, f"Unknown tool '{name}'")
        try:
            result = handler(**dict(arguments or {}))
            return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            log.exception("tool %s failed", name)
            return self._error(name, f"Execution failed: {exc}")

    def dispatch(self, tool_calls: list[dict]) -> list[dict]:
        """Convert provider ``tool_calls`` into ``role=tool`` chat messages."""
        messages: list[dict] = []
        for call in tool_calls or []:
            call_id = call.get("id") or ""
            function = call.get("function") or {}
            name = function.get("name") or ""
            try:
                arguments = json.loads(function.get("arguments") or "{}")
                if not isinstance(arguments, dict):
                    arguments = {}
            except json.JSONDecodeError:
                arguments = {}
            content = self.execute(name, arguments)
            message: dict = {"role": "tool", "tool_call_id": call_id, "content": content}
            if name:
                message["name"] = name
            messages.append(message)
        return messages