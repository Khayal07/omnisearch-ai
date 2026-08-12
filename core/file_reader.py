"""Reads local file content for AI ingestion.

Plain-text encodings are handled without external dependencies; PDF text uses
a minimal FlateDecode stream extractor (works for the majority of generated 
PDFs) and DOCX is unzipped and parsed from ``word/document.xml``.
"""

from __future__ import annotations

import logging
import re
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET

log = logging.getLogger(__name__)

_TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".json", ".csv", ".log", ".ini", ".config",
    ".yaml", ".yml", ".toml", ".xml", ".html", ".htm", ".css", ".js",
    ".ts", ".jsx", ".tsx", ".c", ".cpp", ".h", ".hpp", ".java", ".go",
    ".rs", ".sql", ".sh", ".bat", ".ps1", ".rb", ".php",
}
_PDF, _DOCX = ".pdf", ".docx"
_DEFAULT_MAX_CHARS = 8000
_TRUNCATION_MARK = "\n…[truncated]"


def truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """Cleanly cut ``text`` to ``max_chars``; returns ``(text, was_truncated)``."""
    if max_chars <= 0:
        return "", True
    if len(text) <= max_chars:
        return text, False
    budget = max_chars - len(_TRUNCATION_MARK)
    if budget <= 0:
        return text[: max_chars], True
    cut = text[:budget]
    space = cut.rfind(" ")
    if space > budget * 0.5:
        cut = cut[:space]
    return cut.rstrip() + _TRUNCATION_MARK, True


_UNESCAPE = re.compile(r"\\([()\\])")


class FileContentReader:
    """A context-free reader that returns a result dict for any supported file."""

    @staticmethod
    def supported_extensions() -> frozenset:
        return frozenset(_TEXT_EXTENSIONS | {_PDF, _DOCX})

    def read(self, path: str | Path, max_chars: int = _DEFAULT_MAX_CHARS) -> dict:
        file_path = Path(path)
        if not file_path.exists():
            return {"ok": False, "path": str(file_path), "message": "File not found"}
        if not file_path.is_file():
            return {"ok": False, "path": str(file_path), "message": "Not a file"}

        ext = file_path.suffix.lower()
        try:
            if ext == _DOCX:
                content, kind = self._read_docx(file_path)
            elif ext == _PDF:
                content, kind = self._read_pdf(file_path)
            elif ext in _TEXT_EXTENSIONS:
                content, kind = self._read_text(file_path)
            else:
                return {"ok": False, "path": str(file_path), "message": f"Unsupported file type '{ext}'"}
        except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError, zlib.error) as exc:
            log.debug("failed to read %s: %s", file_path, exc)
            return {"ok": False, "path": str(file_path), "message": f"Read failed: {exc}"}

        clipped, was_truncated = truncate(content, int(max_chars))
        return {
            "ok": True,
            "path": str(file_path),
            "kind": kind,
            "chars": len(content),
            "truncated": was_truncated,
            "content": clipped,
        }

    # -- backends ----------------------------------------------------------

    def _read_text(self, path: Path) -> tuple[str, str]:
        try:
            raw = path.read_bytes()
        except OSError:
            return "", "text"
        for encoding in ("utf-8", "utf-16", "cp1252", "latin-1"):
            try:
                return raw.decode(encoding), "text"
            except (UnicodeDecodeError, UnicodeError):
                continue
        return raw.decode("utf-8", errors="replace"), "text"

    def _read_docx(self, path: Path) -> tuple[str, str]:
        w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        with zipfile.ZipFile(path) as archive:
            xml_bytes = archive.read("word/document.xml")
        root = ET.fromstring(xml_bytes)
        paragraphs = []
        for para in root.iter(f"{w}p"):
            text = "".join(t.text or "" for t in para.iter(f"{w}t"))
            if text.strip():
                paragraphs.append(text)
        return "\n".join(paragraphs), "docx"

    def _read_pdf(self, path: Path) -> tuple[str, str]:
        data = path.read_bytes()
        pieces: list[str] = []
        for stream in re.findall(rb"stream\r?\n(.*?)endstream", data, re.S):
            raw = stream.rstrip(b"\r\n\x00")
            if not raw:
                continue
            try:
                content = zlib.decompress(raw)
            except zlib.error:
                try:
                    content = zlib.decompress(raw + b"\x00\x00\xff\xff")
                except zlib.error:
                    content = raw
            text = content.decode("latin-1", errors="ignore")
            for token in re.finditer(r"\(((?:[^()\\]|\\.)*)\)\s*Tj", text):
                piece = _UNESCAPE.sub(r"\1", token.group(1))
                if piece.strip():
                    pieces.append(piece)
        return "\n".join(pieces), "pdf"