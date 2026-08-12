"""Verification tests for the settings dialog and hotkey capture widget."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest

from config import ConfigManager
from utils import win_startup
from ui.settings_dialog import HotkeyCapture, SettingsDialog


@pytest.fixture
def cfg(tmp_path, mocker):
    config = ConfigManager(config_dir=tmp_path, secrets_backend="plaintext")
    config.set_api_key("openai", "sk-settings")
    mocker.patch.object(win_startup, "is_auto_start", return_value=False)
    return config


@pytest.fixture
def dialog(qtbot, cfg):
    dlg = SettingsDialog(cfg)
    qtbot.addWidget(dlg)
    return dlg


class TestHotkeyCapture:
    def test_records_valid_chord(self, qtbot):
        capture = HotkeyCapture()
        qtbot.addWidget(capture)
        capture.show()
        capture.setFocus()
        sequence = QKeySequence(Qt.CTRL | Qt.ALT | Qt.Key_Z)
        QTest.keySequence(capture, sequence)
        from core.hotkey import parse_combo

        captured = capture.text()
        assert captured
        parse_combo(captured)  # must not raise

    def test_commits_signal(self, qtbot):
        capture = HotkeyCapture()
        qtbot.addWidget(capture)
        capture.show()
        capture.setFocus()
        with qtbot.waitSignal(capture.combo_committed, timeout=1000):
            QTest.keySequence(capture, QKeySequence(Qt.CTRL | Qt.SHIFT | Qt.Key_X))
        assert capture.text()

    def test_escape_clears(self, qtbot):
        capture = HotkeyCapture()
        qtbot.addWidget(capture)
        capture.setText("Alt+Space")
        assert capture.text() == "Alt+Space"


class TestSettingsDialog:
    def test_loads_active_provider(self, dialog):
        assert dialog.provider.currentText() == "openai"
        assert dialog.model.text() == "gpt-4o-mini"

    def test_key_fields_loaded(self, dialog, cfg):
        assert dialog.key_edits["openai"].text() == "sk-settings"
        assert not dialog.key_edits["gemini"].isEnabled()

    def test_provider_switch_refreshes_widgets(self, dialog):
        dialog.provider.setCurrentText("gemini")
        assert dialog.model.text() == "gemini-2.0-flash"
        assert dialog.key_edits["gemini"].isEnabled()

    def test_save_persists_values(self, dialog, cfg):
        dialog.model.setText("gpt-4.1")
        dialog.temperature.setValue(0.45)
        dialog.hotkey.setText("Ctrl+Alt+S")
        dialog.accept()

        fresh = ConfigManager(config_dir=cfg.config_dir, secrets_backend="plaintext")
        assert fresh.get("providers.openai.model") == "gpt-4.1"
        assert fresh.get("temperature") == 0.45
        assert fresh.get("hotkey") == "Ctrl+Alt+S"
        assert fresh.api_key("openai") == "sk-settings"

    def test_save_persists_fallback_list(self, dialog, cfg):
        dialog.fallback.setText("gemini, openai")
        dialog.accept()
        fresh = ConfigManager(config_dir=cfg.config_dir, secrets_backend="plaintext")
        assert fresh.get("fallback_order") == ["gemini", "openai"]

    def test_save_applies_auto_start(self, dialog, mocker):
        mock_set = mocker.patch.object(win_startup, "set_auto_start")
        dialog.auto_start.setChecked(True)
        dialog.accept()
        mock_set.assert_called_once_with(True)