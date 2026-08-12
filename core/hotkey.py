"""Global hotkey management using the native Win32 RegisterHotKey API.

Chosen over low-level keyboard hooks (pynput / keyboard) because RegisterHotKey:
  * does not install a WH_KEYBOARD_LL hook (avoids AV / Defender false positives),
  * needs no third-party dependency,
  * surfaces registration conflicts explicitly as ERROR_HOTKEY_ALREADY_REGISTERED.

The window is registered on the main thread with a NULL window handle, so
WM_HOTKEY is posted to the Qt event loop's message queue; a
QAbstractNativeEventFilter translates it into a Qt signal.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal

log = logging.getLogger(__name__)

# --- Win32 constants -----------------------------------------------------

WM_HOTKEY = 0x0312
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

_HOTKEY_ID = 1
_HWND_BROADCAST = 0xFFFF
_SMTO_ABORTIFHUNG = 0x0002

_MODIFIER_TOKENS: dict[str, int] = {
    "alt": MOD_ALT,
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "super": MOD_WIN,
    "windows": MOD_WIN,
    "meta": MOD_WIN,
}

_MODIFIER_NAMES = {
    MOD_ALT: "Alt",
    MOD_CONTROL: "Ctrl",
    MOD_SHIFT: "Shift",
    MOD_WIN: "Win",
}

_VK_TOKENS: dict[str, int] = {
    "space": 0x20, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "enter": 0x0D, "return": 0x0D, "backspace": 0x08,
    "delete": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pgup": 0x21, "pageup": 0x21, "pgdn": 0x22, "pagedown": 0x22,
    "left": 0x25, "right": 0x27, "up": 0x26, "down": 0x28,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
    "\\": 0xDC,
}

_VK_TOKEN_NAMES = {value: key.title() for key, value in _VK_TOKENS.items()}


class HotkeyError(Exception):
    """Raised when a hotkey cannot be registered."""

    def __init__(self, message: str, error_code: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class HotkeyConflictError(HotkeyError):
    """Raised when another application already owns the combination."""


def _numeral_vk(token: str) -> int | None:
    if token.startswith("f") and token[1:].isdigit():
        n = int(token[1:])
        if 1 <= n <= 24:
            return 0x70 + n - 1
    return None


def parse_combo(combo: str) -> tuple[int, int]:
    """Parse ``"Alt+Space"`` into ``(modifiers, virtual_key)``.

    Raises ValueError on malformed input.
    """
    raw = combo.strip()
    if not raw:
        raise ValueError("hotkey is empty")
    parts = [p.strip().lower() for p in raw.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"invalid hotkey: {combo!r}")

    modifiers = 0
    key_token = ""
    for part in parts[:-1]:
        mod = _MODIFIER_TOKENS.get(part)
        if mod is None:
            raise ValueError(f"unknown modifier: {part!r}")
        if modifiers & mod:
            raise ValueError(f"duplicate modifier: {part!r}")
        modifiers |= mod

    key_token = parts[-1].lower()
    vk = _VK_TOKENS.get(key_token, _numeral_vk(key_token) if _numeral_vk(key_token) else None)
    if vk is None:
        if len(key_token) == 1 and key_token.isalnum():
            vk = ord(key_token.upper())
        else:
            raise ValueError(f"unknown key: {key_token!r}")
    return modifiers, vk


def combo_to_string(modifiers: int, vk: int) -> str:
    tokens = [_MODIFIER_NAMES[m] for m in (MOD_WIN, MOD_CONTROL, MOD_ALT, MOD_SHIFT) if modifiers & m]
    name = _VK_TOKEN_NAMES.get(vk)
    if name is None:
        char = chr(vk)
        if vk >= 0x70 and vk <= 0x87:
            char = f"F{vk - 0x70 + 1}"
        elif char.isalpha():
            char = char.upper()
        name = char
    return "+".join([*tokens, name])


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


class GlobalHotkey(QObject, QAbstractNativeEventFilter):
    """Registers a global hotkey and translates WM_HOTKEY into a Qt signal.

    Because it subclasses both QObject and QAbstractNativeEventFilter it can be
    installed directly as a native event filter on the QApplication.
    """

    activated = Signal()
    registration_failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        QObject.__init__(self, parent)
        QAbstractNativeEventFilter.__init__(self)
        self._user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None
        self._combo: str | None = None
        self._modifiers = 0
        self._vk = 0
        self._registered = False
        self._show_message_id = 0

    # -- registration -----------------------------------------------------

    @property
    def combo(self) -> str | None:
        return self._combo

    @property
    def registered(self) -> bool:
        return self._registered

    def register(self, combo: str) -> bool:
        """Register `combo` globally. Returns True on success."""
        self.unregister()
        modifiers, vk = parse_combo(combo)

        if self._user32 is None:
            self._registered = False
            return False

        user32 = self._user32
        user32.RegisterHotKey.argtypes = [
            wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT,
        ]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL

        # MOD_NOREPEAT stops Key-Up spam while a key is held.
        ok = user32.RegisterHotKey(None, _HOTKEY_ID, modifiers | MOD_NOREPEAT, vk)
        if not ok:
            error = ctypes.WinError()
            code = getattr(error, "winerror", None) or ctypes.get_last_error()
            if code == ERROR_HOTKEY_ALREADY_REGISTERED:
                log.warning("hotkey %r already owned by another application", combo)
                self.registration_failed.emit(
                    f"Hotkey {combo!r} is already in use by another application. "
                    "Change it in Settings."
                )
                return False
            log.error("RegisterHotKey(%r) failed: %s", combo, error)
            self.registration_failed.emit(f"Failed to register hotkey: {error}")
            return False

        self._combo = combo
        self._modifiers = modifiers
        self._vk = vk
        self._registered = True
        log.info("registered global hotkey %r (id=%d)", combo, _HOTKEY_ID)
        return True

    def unregister(self) -> None:
        if not self._registered or self._user32 is None:
            if self._registered:
                self._registered = False
            return
        try:
            self._user32.UnregisterHotKey(None, _HOTKEY_ID)
        except Exception:
            pass
        self._registered = False

    # -- native event handling -------------------------------------------

    @property
    def show_message_id(self) -> int:
        """ID of the broadcast message used to summon an existing instance."""
        if self._show_message_id == 0 and self._user32 is not None:
            reg = self._user32.RegisterWindowMessageW
            reg.argtypes = [wintypes.LPCWSTR]
            reg.restype = wintypes.UINT
            self._show_message_id = reg(u"OmnisearchAI_ShowOverlay")
        return self._show_message_id

    def wake_existing(self) -> None:
        """Ask a running instance to reveal its overlay."""
        if self._user32 is None:
            return
        result = wintypes.LRESULT()
        send = self._user32.SendMessageTimeoutW
        send.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
            wintypes.UINT, wintypes.UINT, ctypes.POINTER(wintypes.LRESULT),
        ]
        send.restype = ctypes.c_size_t
        try:
            send(_HWND_BROADCAST, self.show_message_id, 0, 0,
                 _SMTO_ABORTIFHUNG, 500, ctypes.byref(result))
        except Exception:
            pass

    @staticmethod
    def _as_msg(message) -> MSG | None:
        try:
            return MSG.from_address(int(message))
        except (TypeError, ValueError):
            try:
                return ctypes.cast(message, ctypes.POINTER(MSG)).contents
            except Exception:
                return None

    def nativeEventFilter(self, event_type, message):
        if event_type != b"windows_generic_MSG":
            return False
        msg = self._as_msg(message)
        if msg is None:
            return False
        if msg.message == WM_HOTKEY and msg.wParam == _HOTKEY_ID:
            log.debug("WM_HOTKEY received (combo=%r)", self._combo)
            self.activated.emit()
            return True, 0
        if self._show_message_id and msg.message == self._show_message_id:
            log.debug("show-overlay broadcast received")
            self.activated.emit()
            return True, 0
        return False

    def cleanup(self) -> None:
        self.unregister()