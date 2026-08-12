"""The floating, frameless OmniSearch overlay window.

Rendering strategy:
  * ``Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool`` keeps it out
    of the taskbar and Alt-Tab, floating above every other window.
  * Per-monitor DPI awareness is set in ``main.py`` before app creation and the
    window is centered on the screen that currently holds the cursor.
  * Fade in/out animates ``windowOpacity`` (GPU-composited and cheap) rather
    than QGraphicsOpacityEffect, which falls back to the software renderer.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPropertyAnimation,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from utils.system_info import centered_rect, screen_for_cursor
from core.ai_engine import AIEngine

from .components import SearchBar, StreamingMarkdown

log = logging.getLogger(__name__)

_STYLES_DIR = Path(__file__).resolve().parent.parent / "styles"
_FOCUS_OUT_GRACE_MS = 150
_FADE_IN_MS = 120
_FADE_OUT_MS = 90


class Overlay(QWidget):
    """Frameless search overlay. Emits submitted / cancelled for the engine."""

    submitted = Signal(str)
    cancelled = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.setObjectName("overlayRoot")

        self._width = 720
        self._height = 560
        self._animating = False
        self._programmatic_hide = False
        self._quit_requested = False
        self._busy = False
        self._engine: AIEngine | None = None

        self._build_ui()
        self.apply_theme("dark")
        self.setDefaultSize()
        self.submitted.connect(self._on_submit)

    # -- UI construction --------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(0)

        self.card = QFrame(self)
        self.card.setObjectName("card")

        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        self.search = SearchBar(self.card)
        self.search.setFocusPolicy(Qt.StrongFocus)
        top_row = QHBoxLayout()
        top_row.addWidget(self.search, stretch=1)

        self.status = QLabel("", self.card)
        self.status.setObjectName("statusLabel")
        self.status.hide()
        top_row.addWidget(self.status)
        layout.addLayout(top_row)

        self.output = StreamingMarkdown(self.card)
        layout.addWidget(self.output, stretch=1)

        self.hint = QLabel(
            "Enter to ask  ·  Esc to cancel  ·  Ctrl+Scroll to zoom", self.card
        )
        self.hint.setObjectName("footerHint")
        layout.addWidget(self.hint)

        outer.addWidget(self.card)
        self.search.setFocusProxy(None)
        self.search.textChanged.connect(self._on_text_changed)
        self.search.submitted.connect(self.submitted)

    def attach_engine(self, engine: AIEngine) -> None:
        self._engine = engine
        engine.started.connect(self._on_stream_start)
        engine.chunk.connect(self._on_stream_chunk)
        engine.done.connect(self._on_stream_done)
        engine.failed.connect(self._on_stream_failed)
        engine.cancelled.connect(self._on_stream_cancelled)
        self.cancelled.connect(engine.cancel)

    def _set_status(self, text: str, flash_ms: int = 0) -> None:
        self.status.setText(text)
        self.status.setStyleSheet("")
        self.status.show()
        if flash_ms > 0:
            if getattr(self, "_status_timer", None) is not None:
                self._status_timer.stop()
            self._status_timer = QTimer(self)
            self._status_timer.setSingleShot(True)
            self._status_timer.timeout.connect(self.status.hide)
            self._status_timer.start(flash_ms)

    # -- streaming pipeline ------------------------------------------------

    def _on_submit(self, text: str) -> None:
        if self._busy or self._engine is None:
            return
        self._busy = True
        self.output.begin_stream()
        self._set_status("thinking…")
        self._engine.submit(text)

    def _on_stream_start(self, provider: str) -> None:
        self.status.setText(f"streaming · {provider}")
        self.status.show()

    def _on_stream_chunk(self, delta: str) -> None:
        self.output.add_stream(delta)

    def _on_stream_done(self, full: str, provider: str = "") -> None:
        anchor = f" · {provider}" if provider else ""
        self._busy = False
        self.output.finalize_stream()
        self._set_status(f"done{anchor}", flash_ms=2500)

    def _on_stream_failed(self, message: str, provider: str = "") -> None:
        self._busy = False
        self.output.begin_stream()
        self.output.add_stream(f"**Error** · {message}")
        self.output.finalize_stream()
        self._set_status("error", flash_ms=5000)

    def _on_stream_cancelled(self) -> None:
        self._busy = False
        self.output.finalize_stream(reset_buffer=False)
        self._set_status("cancelled", flash_ms=1800)

    def setDefaultSize(self) -> None:
        self.resize(self._width, self._height)

    # -- interaction between search list and size --------------------------

    def _on_text_changed(self, text: str) -> None:
        # Placeholder: history list + auto-expand arrives in later steps.
        del text

    # -- theming -----------------------------------------------------------

    def apply_theme(self, theme: str) -> None:
        stylesheet_path = _STYLES_DIR / f"{theme}_theme.qss"
        if not stylesheet_path.is_file():
            stylesheet_path = _STYLES_DIR / "dark_theme.qss"
        try:
            self.setStyleSheet(stylesheet_path.read_text(encoding="utf-8"))
        except OSError as exc:
            log.warning("failed to load theme %r: %s", theme, exc)

    # -- summon / dismiss --------------------------------------------------

    def show_at_cursor(self, initial_text: str = "", inject_clipboard: bool = False) -> None:
        """Center on the cursor's screen and open with a fade-in."""
        screen = screen_for_cursor()
        if screen is None:
            self.center()
        else:
            self.setGeometry(centered_rect(screen.availableGeometry(), self._width, self._height))

        if inject_clipboard:
            self._maybe_pull_clipboard()

        self._prepare_show(initial_text)

    def _prepare_show(self, initial_text: str) -> None:
        self._programmatic_hide = False
        if self._animating:
            self._animation.stop()
        if not self._busy:
            if self.output.current_markdown():
                self.output.begin_stream()
            self.status.hide()

        self.show()
        self.raise_()
        self.activateWindow()
        self.search.clear()
        if initial_text:
            self.search.setText(initial_text)
        self.search.setFocus(Qt.ActiveWindowFocusReason)
        self._fade_in()

    def center(self) -> None:
        screen = screen_for_cursor()
        geo = screen.availableGeometry() if screen is not None else self.screen().availableGeometry()
        self.setGeometry(centered_rect(geo, self._width, self._height))

    def _maybe_pull_clipboard(self) -> None:
        try:
            text = QApplication.clipboard().text()
        except Exception:
            return
        if text and len(text.strip()) > 0:
            self.search.setText(text.strip())

    def dismiss(self) -> None:
        """Hide without animating (used on explicit close)."""
        self._programmatic_hide = True
        self.hide()
        self._emit_cancelled()

    def _emit_cancelled(self) -> None:
        self.cancelled.emit()

    # -- animations --------------------------------------------------------

    def _fade_in(self) -> None:
        self._animate(b"windowOpacity", 0.0, 1.0, _FADE_IN_MS)

    def _fade_out(self) -> None:
        self._programmatic_hide = True
        self._animate(
            b"windowOpacity", 1.0, 0.0, _FADE_OUT_MS, on_finish=self.hide
        )

    def _animate(self, prop, start, end, duration, on_finish=None) -> None:
        self.setWindowOpacity(start)
        self._animating = True
        anim = QPropertyAnimation(self, prop, self)
        anim.setDuration(duration)
        anim.setStartValue(start)
        anim.setEndValue(end)
        anim.setEasingCurve(QEasingCurve.OutCubic if end > start else QEasingCurve.InCubic)

        def _done():
            self._animating = False
            self.setWindowOpacity(1.0)
            if on_finish:
                on_finish()

        anim.finished.connect(_done)

        self._animation = anim
        anim.start()
        self._current_animation = anim

    # -- event handling ----------------------------------------------------

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            event.accept()
            self._fade_out()
            self._emit_cancelled()
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            text = self.search.text().strip()
            if text:
                self.submitted.emit(text)
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if self.card and not self.card.rect().contains(self.card.mapFromGlobal(event.globalPosition().toPoint())):
            self._fade_out()
        super().mousePressEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        if self._programmatic_hide or self._animating:
            return
        QTimer.singleShot(_FOCUS_OUT_GRACE_MS, self._on_focus_out_grace)

    def _on_focus_out_grace(self) -> None:
        """Dismiss only if focus truly left the app (menus/popups ignored)."""
        if self._programmatic_hide or self._animating or not self.isVisible():
            return
        popup = QApplication.activePopupWidget()
        if popup is not None:
            return  # a context menu / popup is open
        focused = QApplication.focusWidget()
        if focused is not None and (
            self.isAncestorOf(focused) or focused.window() is self
        ):
            return  # focus returned to our widgets
        self._fade_out()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange and self.windowState() & Qt.WindowActive:
            if not self.search.hasFocus():
                self.search.setFocus(Qt.ActiveWindowFocusReason)

    def close_overlay(self) -> None:
        self.dismiss()

    def cleanup(self) -> None:
        self._programmatic_hide = True
        self.hide()