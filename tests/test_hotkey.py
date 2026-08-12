"""Verification tests for the native-RegisterHotKey global hotkey manager."""

import ctypes
import os
from ctypes import wintypes

import pytest

from core.hotkey import (
    ERROR_HOTKEY_ALREADY_REGISTERED,
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    MOD_WIN,
    MSG,
    WM_HOTKEY,
    GlobalHotkey,
    HotkeyError,
    combo_to_string,
    parse_combo,
)

VK_SPACE = 0x20
VK_F24 = 0x87

_RAW_HOTKEY_ID = 0x7F00


def _raw_register(modifiers: int, vk: int, hotkey_id: int) -> bool:
    user32 = ctypes.windll.user32
    return bool(
        user32.RegisterHotKey(None, hotkey_id, modifiers | 0x4000, vk)
    )


def _raw_unregister(hotkey_id: int) -> None:
    ctypes.windll.user32.UnregisterHotKey(None, hotkey_id)


# -- parsing --------------------------------------------------------------

class TestParseCombo:
    def test_alt_space(self):
        assert parse_combo("Alt+Space") == (MOD_ALT, VK_SPACE)

    def test_chord_with_modifiers(self):
        assert parse_combo("Ctrl+Shift+Z") == (MOD_CONTROL | MOD_SHIFT, ord("Z"))
        assert parse_combo("Win+Alt+1") == (MOD_WIN | MOD_ALT, ord("1"))

    def test_function_keys(self):
        assert parse_combo("Alt+F4") == (MOD_ALT, 0x73)
        assert parse_combo("Ctrl+F24") == (MOD_CONTROL, VK_F24)

    def test_whitespace_tolerated(self):
        assert parse_combo("  Alt + Space ") == (MOD_ALT, VK_SPACE)

    def test_duplicate_modifier_rejected(self):
        with pytest.raises(ValueError):
            parse_combo("Ctrl+Ctrl+X")

    def test_unknown_token_rejected(self):
        with pytest.raises(ValueError):
            parse_combo("Alt+NotAKey")

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            parse_combo("   ")

    def test_modifier_only_rejected(self):
        with pytest.raises(ValueError):
            parse_combo("Alt")


class TestComboToString:
    @pytest.mark.parametrize(
        "combo",
        [
            "Alt+Space",
            "Ctrl+Shift+Z",
            "Win+Alt+F4",
            "Alt+`",
            "Ctrl+F24",
            "Ctrl+Shift+PageDown",
        ],
    )
    def test_round_trip(self, combo):
        modifiers, vk = parse_combo(combo)
        assert parse_combo(combo_to_string(modifiers, vk)) == (modifiers, vk)


# -- registration (Windows only) ------------------------------------------

@pytest.mark.skipif(os.name != "nt", reason="RegisterHotKey is Windows-only")
class TestRegistration:
    def test_register_and_unregister(self, qapp):
        hk = GlobalHotkey()
        assert hk.register("Ctrl+Shift+F24") is True
        assert hk.registered is True
        assert hk.combo == "Ctrl+Shift+F24"
        hk.unregister()
        assert hk.registered is False

    def test_reregister_after_unregister(self, qapp):
        hk = GlobalHotkey()
        assert hk.register("Ctrl+Shift+F24") is True
        assert hk.register("Alt+F24") is True
        hk.unregister()

    def test_conflict_is_detected(self, qapp):
        assert _raw_register(MOD_CONTROL | MOD_SHIFT, VK_F24, _RAW_HOTKEY_ID)
        try:
            hk = GlobalHotkey()
            messages = []
            hk.registration_failed.connect(lambda text: messages.append(text))
            assert hk.register("Ctrl+Shift+F24") is False
            assert hk.registered is False
            assert messages, "expected a conflict notification"
        finally:
            _raw_unregister(_RAW_HOTKEY_ID)

    def test_unregister_frees_the_combo(self, qapp):
        hk = GlobalHotkey()
        assert hk.register("Ctrl+Shift+F24") is True
        hk.unregister()
        assert _raw_register(MOD_CONTROL | MOD_SHIFT, VK_F24, _RAW_HOTKEY_ID)
        _raw_unregister(_RAW_HOTKEY_ID)


# -- native event filter --------------------------------------------------

class TestNativeFilter:
    def _message(self, msg_id: int, w_param: int = 0):
        msg = MSG()
        msg.hwnd = None
        msg.message = msg_id
        msg.wParam = w_param
        msg.lParam = 0
        return ctypes.pointer(msg)

    def test_hotkey_message_triggers_activation(self, qapp, qtbot):
        hk = GlobalHotkey()
        with qtbot.waitSignal(hk.activated, timeout=1000):
            handled, _ = hk.nativeEventFilter(
                b"windows_generic_MSG", self._message(WM_HOTKEY, w_param=1)
            )
        assert handled is True

    def test_unrelated_message_not_handled(self, qapp):
        hk = GlobalHotkey()
        assert hk.nativeEventFilter(b"windows_generic_MSG", self._message(0x9999)) is False

    def test_wrong_event_type_ignored(self, qapp):
        hk = GlobalHotkey()
        assert hk.nativeEventFilter(b"some_other_type", 0) is False

    def test_int_pointer_form_supported(self, qapp, qtbot):
        hk = GlobalHotkey()
        msg = MSG()
        msg.message = WM_HOTKEY
        msg.wParam = 1
        address = ctypes.addressof(msg)
        handled, _ = hk.nativeEventFilter(b"windows_generic_MSG", address)
        assert handled is True