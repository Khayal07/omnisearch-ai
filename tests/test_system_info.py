"""Verification tests for system helpers and the run entry point."""

import os
import sys

import pytest
from PySide6.QtCore import QRect

from utils.system_info import centered_rect, current_memory_usage_mb


class TestMemoryAudit:
    def test_memory_usage_returns_value(self):
        mb = current_memory_usage_mb()
        assert mb >= 0
        if sys.platform == "win32":
            assert mb > 0


@pytest.mark.skipif(os.name != "nt", reason="regression check: main.py is Windows-targeted")
class TestEntryPoint:
    def test_main_module_imports(self):
        import importlib

        module = importlib.import_module("main")
        assert hasattr(module, "run")

    def test_build_tray_icon(self, qapp):
        from PySide6.QtGui import QIcon
        from main import build_tray_icon

        icon = build_tray_icon()
        assert isinstance(icon, QIcon)
        assert not icon.isNull()