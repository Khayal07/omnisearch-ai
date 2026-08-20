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
    QRect,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QCursor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from utils.system_info import centered_rect, screen_for_cursor
from core.ai_engine import AIEngine

from .history import HistoryStore
from .components import HistoryRow, SearchBar, StreamingMarkdown

log = logging.getLogger(__name__)

_STYLES_DIR = Path(__file__).resolve().parent.parent / "styles"
_FOCUS_OUT_GRACE_MS = 150
_FADE_IN_MS = 120
_FADE_OUT_MS = 90
_MIN_H = 122
_MAX_H = 760
_RESIZE_MS = 180
_SIZE_UPDATE_DELAY_MS = 50
_CARD_PAD = 24
_OUTER_PAD = 42
_ROW_SPACING = 10
_HISTORY_ROW_H = 46
_HISTORY_MAX_H = 260


def theme_stylesheet(theme: str) -> str:
    """Read the QSS for ``theme``, falling back to the dark theme if missing."""
    path = _STYLES_DIR / f"{theme}_theme.qss"
    if not path.is_file():
        path = _STYLES_DIR / "dark_theme.qss"
    return path.read_text(encoding="utf-8")


def format_conversation(messages: list[dict]) -> str:
    """Render conversation turns as a Markdown transcript."""
    blocks = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content") or ""
        if role == "user":
            blocks.append(f"**Siz**\n\n{content}")
        elif role == "assistant":
            provider = msg.get("provider") or ""
            label = "**OmniSearch**" + (f" · {provider}" if provider else "")
            blocks.append(f"{label}\n\n{content}")
        else:
            blocks.append(content)
    return "\n\n---\n\n".join(blocks)


class Overlay(QWidget):
    """Frameless search overlay. Emits submitted / cancelled for the engine."""

    submitted = Signal(str)
    cancelled = Signal()

    def __init__(self, parent=None, animations: bool = True) -> None:
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
        self._animations_enabled = bool(animations)
        self._animating = False
        self._resize_animating = False
        self._resize_anim: QPropertyAnimation | None = None
        self._size_timer: QTimer | None = None
        self._programmatic_hide = False
        self._busy = False
        self._engine: AIEngine | None = None
        self._history: HistoryStore | None = None
        self._last_submitted = ""
        self._active_conversation: int | None = None

        self._build_ui()
        self.apply_theme("dark")
        self.setDefaultSize()
        self.submitted.connect(self._on_submit)

    def set_animations(self, enabled: bool) -> None:
        self._animations_enabled = bool(enabled)
        if self._resize_animating and self._resize_anim is not None:
            self._resize_anim.stop()
            self._resize_animating = False
        if not self._animations_enabled and self._animating:
            self._animation.stop()
            self.setWindowOpacity(1.0)
            self._animating = False

    # -- UI construction --------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 14, 28, 28)
        outer.setSpacing(0)

        self.card = QFrame(self)
        self.card.setObjectName("card")
        self._apply_card_shadow()

        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        # Header bar: brand on the left, actions on the right. Hidden in compact
        # mode so the idle state stays a single slim search bar.
        self.header = QWidget(self.card)
        self.header.setObjectName("headerBar")
        header = QHBoxLayout(self.header)
        header.setContentsMargins(2, 0, 2, 0)
        header.setSpacing(10)

        brand_row = QHBoxLayout()
        brand_row.setSpacing(8)
        self.brand_dot = QLabel(self.header)
        self.brand_dot.setObjectName("brandDot")
        self.brand_dot.setFixedSize(10, 10)
        self.brand = QLabel("OmniSearch AI", self.header)
        self.brand.setObjectName("brandLabel")
        brand_row.addWidget(self.brand_dot)
        brand_row.addWidget(self.brand)
        header.addLayout(brand_row)
        header.addStretch(1)

        self.new_chat_btn = QPushButton("New chat", self.header)
        self.new_chat_btn.setObjectName("newChatButton")
        self.new_chat_btn.setToolTip("Start a fresh conversation")
        self.new_chat_btn.setCursor(Qt.PointingHandCursor)
        self.new_chat_btn.clicked.connect(self.start_new_chat)

        self.status = QLabel("", self.header)
        self.status.setObjectName("statusLabel")
        self.status.hide()

        header.addWidget(self.new_chat_btn)
        header.addWidget(self.status)
        layout.addWidget(self.header)

        self.search = SearchBar(self.card)
        self.search.setFocusPolicy(Qt.StrongFocus)
        self._add_search_icon()
        layout.addWidget(self.search)

        self.history_list = QListWidget(self.card)
        self.history_list.setObjectName("historyList")
        self.history_list.setMaximumHeight(260)
        self.history_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.history_list.hide()
        self.history_list.itemActivated.connect(self._on_history_selected)
        layout.addWidget(self.history_list)

        self.output = StreamingMarkdown(self.card)
        layout.addWidget(self.output, stretch=1)

        self.hint = QLabel(
            "Enter to ask  ·  Esc to cancel  ·  Ctrl+Scroll to zoom", self.card
        )
        self.hint.setObjectName("footerHint")
        layout.addWidget(self.hint)

        # Idle state is a single slim search bar; header/output/hint appear
        # only when the overlay needs to expand.
        self.header.hide()
        self.output.hide()
        self.hint.hide()

        outer.addWidget(self.card)
        self.search.setFocusProxy(None)
        self.search.textChanged.connect(self._on_text_changed)
        self.search.submitted.connect(self.submitted)

    def _add_search_icon(self, color: str = "#8a93a8") -> None:
        """Leading magnifier glyph drawn programmatically (no asset files)."""
        if getattr(self, "_search_icon_action", None) is not None:
            self.search.removeAction(self._search_icon_action)
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(color), 1.7, Qt.SolidLine, Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(3, 3, 8, 8)
        painter.drawLine(9, 9, 14, 14)
        painter.end()
        self._search_icon_action = self.search.addAction(
            QIcon(pixmap), QLineEdit.ActionPosition.LeadingPosition
        )

    def _apply_card_shadow(self) -> None:
        """Soft drop-shadow so the floating card lifts off the desktop."""
        shadow = QGraphicsDropShadowEffect(self.card)
        shadow.setBlurRadius(40)
        shadow.setXOffset(0)
        shadow.setYOffset(8)
        shadow.setColor(QColor(0, 0, 0, 140))
        self.card.setGraphicsEffect(shadow)

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
        self._last_submitted = text
        self.search.clear()
        self._hide_history()

        store = self._history
        context: list[dict] = []
        if store is not None:
            conv_id = self._active_conversation
            if conv_id is None:
                conv_id = store.create_conversation(title=(text or "")[:60])
                self._active_conversation = conv_id
            prior = store.conversation_messages(conv_id)
            context = [{"role": m["role"], "content": m["content"]} for m in prior]
            store.append_message(conv_id, "user", text)
            self._sync_new_chat_btn()
            self.output.replace_content(format_conversation(prior + [{"role": "user", "content": text}]))
        else:
            self.output.begin_stream()

        self._set_status("thinking…")
        self._engine.submit(text, context=context)

    def start_new_chat(self) -> None:
        """Disconnect from the current conversation; the next query opens a fresh chat."""
        self._active_conversation = None
        self._last_submitted = ""
        self.output.begin_stream()
        self.status.hide()
        self.new_chat_btn.hide()
        self.search.clear()
        self.search.setFocus(Qt.ActiveWindowFocusReason)
        self._update_size_state()

    def _sync_new_chat_btn(self) -> None:
        self.new_chat_btn.setVisible(self._active_conversation is not None)

    def _on_stream_start(self, provider: str) -> None:
        self.status.setText(f"streaming · {provider}")
        self.status.show()
        self._update_size_state()

    def _on_stream_chunk(self, delta: str) -> None:
        self.output.add_stream(delta)
        self._schedule_size_update()

    def _on_stream_done(self, full: str, provider: str = "") -> None:
        anchor = f" · {provider}" if provider else ""
        conv_id = self._active_conversation
        store = self._history
        self._busy = False
        if store is not None and conv_id is not None:
            store.append_message(conv_id, "assistant", full, provider)
            self.output.replace_content(format_conversation(store.conversation_messages(conv_id)))
        else:
            self.output.finalize_stream()
        self._set_status(f"done{anchor}", flash_ms=2500)
        self._update_size_state()
        self._sync_new_chat_btn()

    def _on_stream_failed(self, message: str, provider: str = "") -> None:
        self._busy = False
        self.output.add_stream(f"\n\n**Error** · {message}")
        self.output.finalize_stream(reset_buffer=False)
        self._set_status("error", flash_ms=5000)
        self._update_size_state()

    def _on_stream_cancelled(self) -> None:
        self._busy = False
        self.output.finalize_stream(reset_buffer=False)
        self._set_status("cancelled", flash_ms=1800)
        self._update_size_state()

    def setDefaultSize(self) -> None:
        self.resize(self._width, _MIN_H)

    # -- adaptive sizing ---------------------------------------------------

    def _needs_expansion(self) -> bool:
        return bool(
            self._busy
            or self.search.text().strip()
            or self.output.current_markdown()
            or self.history_list.isVisible()
        )

    @property
    def _min_h(self) -> int:
        return _MIN_H

    def _max_h(self) -> int:
        screen = screen_for_cursor() or self.screen()
        avail = screen.availableGeometry() if screen is not None else None
        if avail is None:
            return _MAX_H
        return max(_MIN_H, min(_MAX_H, int(avail.height() * 0.85)))

    def _target_h(self) -> int:
        """Height the overlay should have for its current visible state."""
        parts: list[int] = []
        if self.header.isVisible():
            parts.append(max(24, self.header.sizeHint().height()))
        parts.append(max(36, self.search.sizeHint().height()))
        if self.history_list.isVisible():
            rows = self.history_list.count()
            parts.append(max(64, min(_HISTORY_MAX_H, rows * _HISTORY_ROW_H + 12)))
        if self.output.isVisible():
            doc_h = int(self.output.document().size().height())
            parts.append(max(70, doc_h + 6))
        if self.hint.isVisible():
            parts.append(max(14, self.hint.sizeHint().height()))
        inner = sum(parts) + _ROW_SPACING * max(0, len(parts) - 1)
        return max(_MIN_H, min(self._max_h(), _OUTER_PAD + _CARD_PAD + inner))

    def _update_size_state(self) -> None:
        expanded = self._needs_expansion()
        self.header.setVisible(expanded)
        self.output.setVisible(expanded)
        self.hint.setVisible(expanded)
        self._resize_to_h(self._target_h())

    def _resize_to_h(self, target: int) -> None:
        target = max(_MIN_H, min(self._max_h(), target))
        # Qt's QLayout sets the top-level minimum size from the layout's
        # minimum while children are visible, and never shrinks it back when
        # they hide. Reset it so we are free to animate the window down.
        self.setMinimumSize(0, 0)
        cur = self.geometry()
        if cur.height() == target:
            return
        new_geo = QRect(cur.x(), cur.y() + (cur.height() - target) // 2,
                        self._width, target)
        if self._animations_enabled:
            if self._resize_animating and self._resize_anim is not None:
                self._resize_anim.stop()
            anim = QPropertyAnimation(self, b"geometry", self)
            anim.setDuration(_RESIZE_MS)
            anim.setStartValue(cur)
            anim.setEndValue(new_geo)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.finished.connect(self._on_resize_finished)
            self._resize_anim = anim
            self._resize_animating = True
            anim.start()
        else:
            self.setGeometry(new_geo)

    def _on_resize_finished(self) -> None:
        self._resize_animating = False

    def _schedule_size_update(self) -> None:
        """Coalesce resize requests fired mid-stream to avoid layout thrash."""
        if self._size_timer is None:
            self._size_timer = QTimer(self)
            self._size_timer.setSingleShot(True)
            self._size_timer.setInterval(_SIZE_UPDATE_DELAY_MS)
            self._size_timer.timeout.connect(self._update_size_state)
        self._size_timer.start()

    # -- interaction between search list and size --------------------------

    def set_history(self, store: HistoryStore) -> None:
        self._history = store

    def _on_text_changed(self, text: str) -> None:
        if self._busy:
            self._hide_history()
            self._update_size_state()
            return
        needle = text.strip()
        store = self._history
        if store is None:
            self._hide_history()
            self._update_size_state()
            return
        results = (
            store.search_conversations(needle, limit=50)
            if needle
            else store.list_conversations(limit=200)
        )
        self._populate_history(results)
        self._update_size_state()

    def _populate_history(self, rows: list) -> None:
        widget = self.history_list
        widget.clear()
        for row in rows:
            title = str(row.get("title")) or "Untitled chat"
            item = QListWidgetItem(title)
            item.setToolTip(title)
            item.setData(Qt.UserRole, row["id"])
            ts = float(row.get("updated_at") or row.get("created_at") or 0)
            row_widget = HistoryRow(title, ts)
            item.setSizeHint(row_widget.sizeHint())
            widget.addItem(item)
            widget.setItemWidget(item, row_widget)
        if widget.count() == 0:
            self._hide_history()
            return
        widget.show()
        widget.setCurrentRow(0)

    def _hide_history(self) -> None:
        self.history_list.hide()
        self._update_size_state()

    def _on_history_selected(self, item) -> None:
        conv_id = item.data(Qt.UserRole)
        if isinstance(conv_id, int):
            self._open_conversation(conv_id)

    def _open_conversation(self, conv_id: int) -> None:
        if self._history is None or self._busy:
            return
        self._active_conversation = conv_id
        messages = self._history.conversation_messages(conv_id)
        self.output.replace_content(format_conversation(messages))
        self._hide_history()
        self._update_size_state()
        self._sync_new_chat_btn()

    # -- theming -----------------------------------------------------------

    def apply_theme(self, theme: str) -> None:
        try:
            self.setStyleSheet(theme_stylesheet(theme))
        except OSError as exc:
            log.warning("failed to load theme %r: %s", theme, exc)
        self._add_search_icon("#8a93a8" if theme == "dark" else "#5b6478")
        self.output.apply_theme(theme)

    # -- summon / dismiss --------------------------------------------------

    def show_at_cursor(self, initial_text: str = "", inject_clipboard: bool = False) -> None:
        """Center on the cursor's screen and open with a fade-in."""
        screen = screen_for_cursor()
        if screen is None:
            self.center()
        else:
            self.setGeometry(
                centered_rect(screen.availableGeometry(), self._width, self._target_h())
            )

        if inject_clipboard:
            self._maybe_pull_clipboard()

        self._prepare_show(initial_text)

    def _prepare_show(self, initial_text: str) -> None:
        self._programmatic_hide = False
        if self._animating:
            self._animation.stop()
        if not self._busy:
            self.status.hide()
            self._hide_history()
        self._update_size_state()
        self._sync_new_chat_btn()

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
        self.setGeometry(centered_rect(geo, self._width, self._target_h()))

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
        if not self._animations_enabled:
            self.setWindowOpacity(1.0)
            return
        self._animate(b"windowOpacity", 0.0, 1.0, _FADE_IN_MS)

    def _fade_out(self) -> None:
        self._programmatic_hide = True
        if not self._animations_enabled:
            self.hide()
            return
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
            if self.history_list.isVisible():
                self._hide_history()
                return
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