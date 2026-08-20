"""Reusable widgets for the overlay window."""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)


def relative_time(timestamp: float, now: float | None = None) -> str:
    """Human-friendly relative timestamp, e.g. 'just now', '5m ago', '2h ago'."""
    now = time.time() if now is None else now
    delta = max(0.0, now - float(timestamp))
    if delta < 45:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    if delta < 7 * 86400:
        return f"{int(delta // 86400)}d ago"
    return time.strftime("%b %d", time.localtime(float(timestamp)))


class HistoryRow(QWidget):
    """Two-line history item: title + relative-time metadata."""

    def __init__(self, title: str, timestamp: float, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 3, 8, 3)
        layout.setSpacing(1)
        self.title = QLabel(title, self)
        self.title.setObjectName("historyTitle")
        self.meta = QLabel(relative_time(timestamp), self)
        self.meta.setObjectName("historyMeta")
        layout.addWidget(self.title)
        layout.addWidget(self.meta)


class SearchBar(QLineEdit):
    """The single search input line.

    Enter submits; the text is also exposed for the quick-action parser.
    """

    submitted = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("searchBar")
        self.setPlaceholderText("Ask anything, or use a quick action like /sum")
        self.setClearButtonEnabled(True)
        self.setAttribute(Qt.WA_MacShowFocusRect, False)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            text = self.text().strip()
            if text:
                self.submitted.emit(text)
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.DeleteStartOfWord):
            cur = self.cursorPosition()
            self.setText(self.text()[:cur])
            self.setCursorPosition(cur)
            event.accept()
            return
        super().keyPressEvent(event)

    def focus_text(self) -> str:
        return self.text().strip()


class StreamingMarkdown(QTextBrowser):
    """Markdown output area that renders streamed token deltas."""

    append_block = Signal(object)

    _DOC_STYLES = {
        "dark": (
            "code { background: rgba(255,255,255,0.08); border-radius: 3px;"
            " padding: 1px 4px; font-family: Consolas, monospace; }"
            "pre { background: rgba(255,255,255,0.05); padding: 8px;"
            " border-radius: 6px; }"
            "a { color: #a8b3c9; }"
            "h1, h2, h3, h4 { color: #f2f3f7; }"
            "strong { color: #f2f3f7; }"
            "blockquote { color: #a6adbd; border-left: 3px solid rgba(138,147,168,60);"
            " padding-left: 8px; margin-left: 0; }"
            "hr { border: none; border-top: 1px solid rgba(255,255,255,14); }"
        ),
        "light": (
            "code { background: rgba(15,20,35,0.06); border-radius: 3px;"
            " padding: 1px 4px; font-family: Consolas, monospace; }"
            "pre { background: rgba(15,20,35,0.04); padding: 8px;"
            " border-radius: 6px; }"
            "a { color: #5b6478; }"
            "h1, h2, h3, h4 { color: #1f2533; }"
            "strong { color: #1f2533; }"
            "blockquote { color: #6a7284; border-left: 3px solid rgba(91,100,120,60);"
            " padding-left: 8px; margin-left: 0; }"
            "hr { border: none; border-top: 1px solid rgba(15,20,35,14); }"
        ),
    }

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("streamOutput")
        self.setOpenExternalLinks(True)
        self.setUndoRedoEnabled(False)
        self.document().setDocumentMargin(10)
        self._buffer = ""
        self.apply_theme("dark")

    def apply_theme(self, theme: str) -> None:
        """Re-apply the inline code/pre/link styles for ``theme``."""
        self.document().setDefaultStyleSheet(
            self._DOC_STYLES.get(theme, self._DOC_STYLES["dark"])
        )

    def begin_stream(self) -> None:
        self._buffer = ""
        self.clear()

    def replace_content(self, markdown: str) -> None:
        """Render previously-created content (e.g. a loaded conversation)
        without starting a new stream."""
        self._buffer = markdown
        self.setMarkdown(markdown)

    def add_stream(self, text: str) -> None:
        self._buffer += text
        self.setMarkdown(self._buffer)

    def finalize_stream(self, reset_buffer: bool = True) -> str:
        markdown = self._buffer
        self.setMarkdown(markdown)
        if reset_buffer:
            self._buffer = ""
        return markdown

    def current_markdown(self) -> str:
        return self._buffer

    def wheelEvent(self, event):
        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import QApplication

        if QApplication.keyboardModifiers() & Qt.ControlModifier:
            delta = event.angleDelta().y()
            current = self.zoomIn if delta > 0 else self.zoomOut
            current(1.05 if delta > 0 else 1 / 1.05)
            event.accept()
            return
        super().wheelEvent(event)