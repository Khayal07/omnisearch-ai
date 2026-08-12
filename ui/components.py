"""Reusable widgets for the overlay window."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QLineEdit, QTextBrowser


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

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("streamOutput")
        self.setOpenExternalLinks(True)
        self.setUndoRedoEnabled(False)
        self.document().setDocumentMargin(10)
        self._buffer = ""
        self.document().setDefaultStyleSheet(
            "code { background: rgba(255,255,255,0.08); border-radius: 3px;"
            " padding: 1px 4px; font-family: Consolas, monospace; }"
            "pre { background: rgba(255,255,255,0.05); padding: 8px;"
            " border-radius: 6px; } a { color: #6c8cff; }"
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