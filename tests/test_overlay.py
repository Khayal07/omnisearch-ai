"""Verification tests for the frameless overlay window and centering math."""

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import QApplication

from ui.overlay import Overlay
from ui.components import SearchBar, StreamingMarkdown
from utils.system_info import centered_rect


class TestCenteredRect:
    def test_centered_math(self):
        rect = centered_rect(QRect(0, 0, 1920, 1080), 720, 560)
        assert rect.x() == (1920 - 720) // 2
        assert rect.y() == (1080 - 560) // 2
        assert rect.width() == 720
        assert rect.height() == 560

    def test_offset_monitor(self):
        rect = centered_rect(QRect(1920, 0, 2560, 1440), 600, 400)
        assert rect.x() == 1920 + (2560 - 600) // 2
        assert rect.y() == (1440 - 400) // 2


@pytest.fixture
def overlay(qtbot):
    win = Overlay()
    qtbot.addWidget(win)
    return win


class TestOverlayWindow:
    def test_frameless_tool_flags(self, overlay):
        flags = overlay.windowFlags()
        assert flags & Qt.FramelessWindowHint
        assert flags & Qt.Tool
        assert flags & Qt.WindowStaysOnTopHint

    def test_translucent_background(self, overlay):
        assert overlay.testAttribute(Qt.WA_TranslucentBackground)

    def test_default_size(self, overlay):
        assert overlay.width() == 720
        assert overlay.height() == 560

    def test_search_bar_present(self, overlay):
        assert overlay.findChild(SearchBar) is not None
        search = overlay.search
        assert search.placeholderText()

    def test_output_widget_present(self, overlay):
        assert overlay.findChild(StreamingMarkdown) is not None

    def test_theme_stylesheet_applied(self, overlay):
        assert "card" in overlay.styleSheet()
        assert overlay.styleSheet().strip()

    def test_show_at_cursor_centers_on_screen(self, overlay, qapp):
        overlay.show_at_cursor()
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry()
        assert overlay.isVisible()
        assert overlay.x() >= available.x()
        assert overlay.y() >= available.y()
        assert overlay.x() + overlay.width() <= available.x() + available.width()
        assert overlay.y() + overlay.height() <= available.y() + available.height()
        overlay.dismiss()

    def test_dismiss_hides_immediately(self, overlay):
        overlay.show()
        assert overlay.isVisible()
        overlay.dismiss()
        assert not overlay.isVisible()

    def test_esc_hides_after_fade(self, overlay, qtbot):
        from PySide6.QtTest import QTest

        overlay.show()
        assert overlay.isVisible()
        with qtbot.waitSignal(overlay.cancelled, timeout=1000):
            QTest.keyClick(overlay, Qt.Key_Escape)
        qtbot.waitUntil(lambda: not overlay.isVisible(), timeout=1500)

    def test_enter_emits_submitted(self, overlay, qtbot):
        from PySide6.QtTest import QTest

        overlay.show()
        overlay.search.setText("hello")
        with qtbot.waitSignal(overlay.submitted, timeout=1000) as blocker:
            QTest.keyClick(overlay.search, Qt.Key_Return)
        assert blocker.args == ["hello"]
        overlay.dismiss()

    def test_outside_click_fade_out_guards(self, overlay, qtbot):
        """Clicking inside the card must not dismiss the overlay."""
        from PySide6.QtTest import QTest

        overlay.show()
        QTest.mouseClick(overlay.card, Qt.LeftButton, pos=overlay.card.rect().center())
        assert overlay.isVisible()
        overlay.dismiss()