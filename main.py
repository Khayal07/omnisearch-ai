"""OmniSearch AI entry point.

Bootstrap order matters:
  1. Per-monitor DPI awareness must be claimed before any Qt object exists.
  2. Single-instance mutex decides whether we act as the primary instance.
  3. The native hotkey filter is installed on the QApplication event loop.
"""

from __future__ import annotations

import os
import sys

if sys.platform == "win32":
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    from utils.system_info import set_dpi_awareness

    set_dpi_awareness()

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from config import ConfigManager
from core.ai_engine import AIEngine
from core.hotkey import GlobalHotkey
from core.single_instance import SingleInstanceLock, wake_existing_instance
from ui.history import HistoryStore
from ui.overlay import Overlay
from ui.settings_dialog import SettingsDialog
from utils.logger import get_logger, setup_logging

log = get_logger()


def build_tray_icon() -> QIcon:
    """Programmatic tray icon (no asset files needed)."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#4a6cf7"))
    painter.drawRoundedRect(4, 4, 56, 56, 14, 14)
    painter.setPen(QPen(QColor("white"), 5, Qt.SolidLine, Qt.RoundCap))
    painter.drawEllipse(22, 20, 18, 18)
    painter.drawLine(36, 34, 46, 44)
    painter.setBrush(QColor("white"))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(13, 12, 8, 8)
    painter.end()
    return QIcon(pixmap)


def _configure_qt(app: QApplication) -> None:
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app.setApplicationName("omnisearch-ai")
    app.setApplicationDisplayName("OmniSearch AI")
    app.setOrganizationName("omnisearch")
    app.setQuitOnLastWindowClosed(False)


def run() -> int:
    setup_logging()
    log.info("starting OmniSearch AI")

    lock = SingleInstanceLock()
    if not lock.is_primary:
        log.info("existing instance running — pinging it and exiting")
        wake_existing_instance()
        return 0

    cfg = ConfigManager()
    app = QApplication(sys.argv)
    _configure_qt(app)

    overlay = Overlay(animations=bool(cfg.get("animations", True)))
    overlay.apply_theme(cfg.get("theme", "dark"))

    engine = AIEngine(cfg)
    overlay.attach_engine(engine)

    history = HistoryStore(cfg.history_db_path, limit=cfg.get("history_limit", 500))
    overlay.set_history(history)

    hotkey = GlobalHotkey()
    app.installNativeEventFilter(hotkey)

    def _summon() -> None:
        overlay.show_at_cursor(
            inject_clipboard=bool(cfg.get("inject_clipboard", True))
        )

    hotkey.activated.connect(_summon)
    hotkey.registration_failed.connect(log.warning)

    combo = str(cfg.get("hotkey", "Alt+Space"))
    if not hotkey.register(combo):
        log.warning("falling back to 'Alt+`' for global hotkey")
        cfg.set("hotkey", "Alt+`")
        hotkey.register("Alt+`")

    # -- system tray -----------------------------------------------------

    tray = QSystemTrayIcon(build_tray_icon(), app)
    tray.setToolTip("OmniSearch AI")

    tray_menu = QMenu()
    tray_menu.addAction("Toggle Overlay", _summon)
    tray_menu.addAction("Settings…", lambda: _open_settings())
    tray_menu.addSeparator()
    tray_menu.addAction("Quit", app.quit)
    tray.setContextMenu(tray_menu)

    def _open_settings() -> None:
        dialog = SettingsDialog(cfg, None)
        if dialog.exec() == dialog.DialogCode.Accepted:
            overlay.apply_theme(str(cfg.get("theme", "dark")))
            overlay.set_animations(bool(cfg.get("animations", True)))
            new_combo = str(cfg.get("hotkey", "Alt+Space"))
            if new_combo != hotkey.combo:
                hotkey.register(new_combo)

    tray.activated.connect(
        lambda reason: _summon()
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick)
        else None
    )
    tray.show()

    def _shutdown() -> None:
        hotkey.cleanup()
        engine.stop()
        history.close()
        tray.hide()
        lock.close()
        log.info("shutting down cleanly")

    app.aboutToQuit.connect(_shutdown)

    return app.exec()


if __name__ == "__main__":
    sys.exit(run())