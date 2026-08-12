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
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from config import ConfigManager
from core.ai_engine import AIEngine
from core.hotkey import GlobalHotkey
from core.single_instance import SingleInstanceLock, wake_existing_instance
from ui.overlay import Overlay
from utils.logger import get_logger, setup_logging

log = get_logger()


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

    overlay = Overlay()
    overlay.apply_theme(cfg.get("theme", "dark"))

    engine = AIEngine(cfg)
    overlay.attach_engine(engine)

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

    def _shutdown() -> None:
        hotkey.cleanup()
        engine.stop()
        lock.close()
        log.info("shutting down cleanly")

    app.aboutToQuit.connect(_shutdown)

    return app.exec()


if __name__ == "__main__":
    sys.exit(run())