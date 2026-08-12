"""Settings panel: providers, keys, hotkey recorder, theme, auto-start."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.hotkey import parse_combo
from utils import win_startup

log = logging.getLogger(__name__)

PROVIDER_KEYS = {"openai": "OPENAI", "gemini": "GEMINI", "custom": "CUSTOM"}
KEY_PROVIDERS = ["openai", "gemini", "custom"]


class HotkeyCapture(QLineEdit):
    """Read-only line that records the next chord typed while focused."""

    combo_committed = Signal(str)

    _SPECIAL_KEYS = {
        Qt.Key_Space: "Space",
        Qt.Key_Return: "Return",
        Qt.Key_Enter: "Return",
        Qt.Key_Tab: "Tab",
        Qt.Key_Escape: "Esc",
        Qt.Key_Backspace: "Backspace",
        Qt.Key_Delete: "Delete",
        Qt.Key_Insert: "Insert",
        Qt.Key_Home: "Home",
        Qt.Key_End: "End",
        Qt.Key_PageUp: "PageUp",
        Qt.Key_PageDown: "PageDown",
        Qt.Key_Left: "Left",
        Qt.Key_Right: "Right",
        Qt.Key_Up: "Up",
        Qt.Key_Down: "Down",
        Qt.Key_QuoteLeft: "`",
        Qt.Key_Minus: "-",
        Qt.Key_Equal: "=",
        Qt.Key_BracketLeft: "[",
        Qt.Key_BracketRight: "]",
        Qt.Key_Semicolon: ";",
        Qt.Key_Apostrophe: "'",
        Qt.Key_Comma: ",",
        Qt.Key_Period: ".",
        Qt.Key_Slash: "/",
        Qt.Key_Backslash: "\\",
    }

    _F_BASE = int(Qt.Key_F1)

    @classmethod
    def _key_token(cls, key: int) -> str:
        if key in cls._SPECIAL_KEYS:
            return cls._SPECIAL_KEYS[key]
        if 0x41 <= key <= 0x5A:  # A..Z
            return chr(key)
        if 0x30 <= key <= 0x39:  # 0..9
            return chr(key)
        if 0x01000030 <= key <= 0x01000047:  # F1..F24
            return f"F{key - 0x01000030 + 1}"
        raise ValueError(f"unsupported key {key}")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setPlaceholderText("Press keys to record…")

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.clear()
            event.accept()
            return
        flags = event.modifiers()
        prefix = []
        if flags & Qt.MetaModifier:
            prefix.append("Win")
        if flags & Qt.ControlModifier:
            prefix.append("Ctrl")
        if flags & Qt.AltModifier:
            prefix.append("Alt")
        if flags & Qt.ShiftModifier:
            prefix.append("Shift")
        try:
            token = self._key_token(int(event.key()))
        except ValueError:
            self.clear()
            event.accept()
            return
        text = "+".join([*prefix, token])
        try:
            parse_combo(text)
        except ValueError:
            self.clear()
            event.accept()
            return
        self.setText(text)
        self.combo_committed.emit(text)
        self.clearFocus()
        event.accept()


class SettingsDialog(QDialog):
    """Modal options panel. Instance writes through to the ConfigManager."""

    def __init__(self, config, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self.setObjectName("settingsDialog")
        self.setWindowTitle("OmniSearch AI — Settings")
        self.setModal(True)
        self.setMinimumWidth(520)

        self._build_ui()
        self._load_values()

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        body = QWidget(scroll)
        form = QFormLayout(body)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(12)

        self.provider = QComboBox(body)
        for name in ("openai", "gemini", "ollama", "custom"):
            self.provider.addItem(name)
        self.provider.currentTextChanged.connect(self._provider_changed)
        form.addRow("Provider", self.provider)

        self.base_url = QLineEdit(body)
        self.base_url.setPlaceholderText("https://…  (leave blank for default)")
        form.addRow("Base URL", self.base_url)

        self.model = QLineEdit(body)
        self.model.setPlaceholderText("model identifier")
        form.addRow("Model", self.model)

        key_group = QWidget(body)
        key_layout = QVBoxLayout(key_group)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(6)
        self.key_edits: dict[str, QLineEdit] = {}
        for provider in KEY_PROVIDERS:
            label = f"{provider.title()} API Key"
            edit = QLineEdit(key_group)
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            edit.setPlaceholderText("sk-…  (stored encrypted with Windows DPAPI)")
            edit.setClearButtonEnabled(True)
            key_layout.addWidget(QLabel(label, key_group))
            key_layout.addWidget(edit)
            self.key_edits[provider] = edit
        form.addRow("API Keys", key_group)

        self.hotkey = HotkeyCapture(body)
        form.addRow("Global Hotkey", self.hotkey)

        self.theme = QComboBox(body)
        self.theme.addItem("dark")
        self.theme.addItem("light")
        form.addRow("Theme", self.theme)

        self.animations = QCheckBox(body)
        form.addRow("Animations", self.animations)

        self.inject_clipboard = QCheckBox(body)
        self.inject_clipboard.setToolTip(
            "Auto-insert the current clipboard text when the overlay opens"
        )
        form.addRow("Inject clipboard on open", self.inject_clipboard)

        self.auto_start = QCheckBox(body)
        form.addRow("Launch at login", self.auto_start)

        self.fallback = QLineEdit(body)
        self.fallback.setPlaceholderText("e.g. ollama, openai, gemini")
        form.addRow("Fallback order", self.fallback)

        self.temperature = QDoubleSpinBox(body)
        self.temperature.setRange(0.0, 2.0)
        self.temperature.setSingleStep(0.1)
        self.temperature.setDecimals(2)
        form.addRow("Temperature", self.temperature)

        self.max_tokens = QSpinBox(body)
        self.max_tokens.setRange(128, 32768)
        self.max_tokens.setSingleStep(128)
        form.addRow("Max tokens", self.max_tokens)

        self.system_prompt = QPlainTextEdit(body)
        self.system_prompt.setPlaceholderText("Default system prompt for the AI")
        form.addRow("System prompt", self.system_prompt)

        scroll.setWidget(body)
        outer.addWidget(scroll, stretch=1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel", self)
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save", self)
        save.setObjectName("primaryButton")
        save.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        outer.addLayout(buttons)

    # -- value loading -----------------------------------------------------

    def _load_values(self) -> None:
        cfg = self._config
        provider = str(cfg.get("provider", "openai"))
        index = self.provider.findText(provider)
        if index >= 0:
            self.provider.setCurrentIndex(index)
        self._provider_changed(provider)

        pc = cfg.provider_config(provider)
        self.base_url.setText(str(pc.get("base_url", "")))
        self.model.setText(str(pc.get("model", "")))

        for name in KEY_PROVIDERS:
            if cfg.api_key(name):
                self.key_edits[name].setText(cfg.api_key(name))

        self.hotkey.setText(str(cfg.get("hotkey", "Alt+Space")))
        self.animations.setChecked(bool(cfg.get("animations", True)))
        self.inject_clipboard.setChecked(bool(cfg.get("inject_clipboard", True)))
        self.auto_start.setChecked(win_startup.is_auto_start())
        self.fallback.setText(", ".join(cfg.get("fallback_order", [])))
        self.temperature.setValue(float(cfg.get("temperature", 0.7)))
        self.max_tokens.setValue(int(cfg.get("max_tokens", 2048)))
        self.system_prompt.setPlainText(str(cfg.get("system_prompt", "")))

    def _provider_changed(self, provider: str) -> None:
        pc = self._config.provider_config(provider)
        self.base_url.setText(str(pc.get("base_url", "")))
        self.model.setText(str(pc.get("model", "")))
        for name in KEY_PROVIDERS:
            self.key_edits[name].setEnabled(name == provider)

    # -- persisting --------------------------------------------------------

    def accept(self) -> None:
        cfg = self._config
        provider = self.provider.currentText()
        cfg.set("provider", provider)
        cfg.set("providers.%s.base_url" % provider, self.base_url.text().strip())
        cfg.set("providers.%s.model" % provider, self.model.text().strip())

        for name, edit in self.key_edits.items():
            value = edit.text().strip()
            if value:
                cfg.set_api_key(name, value)
            else:
                cfg.set_api_key(name, None)

        combo = self.hotkey.text().strip() or "Alt+Space"
        try:
            parse_combo(combo)
        except ValueError:
            combo = "Alt+Space"
        cfg.set("hotkey", combo)

        cfg.set("theme", self.theme.currentText())
        cfg.set("animations", self.animations.isChecked())
        cfg.set("inject_clipboard", self.inject_clipboard.isChecked())
        cfg.set("auto_start", self.auto_start.isChecked())

        fallback = [
            part.strip()
            for part in self.fallback.text().split(",")
            if part.strip()
        ]
        cfg.set("fallback_order", fallback or ["ollama", "openai", "gemini"])
        cfg.set("temperature", self.temperature.value())
        cfg.set("max_tokens", self.max_tokens.value())
        cfg.set("system_prompt", self.system_prompt.toPlainText())

        try:
            win_startup.set_auto_start(self.auto_start.isChecked())
        except Exception:
            log.exception("failed to apply auto-start setting")

        super().accept()