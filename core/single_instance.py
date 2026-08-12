"""Single-instance enforcement for the overlay.

Uses a named Windows mutex (CreateMutexW). A second launch detects the
existing instance and broadcasts a registered window message asking it to
reveal its overlay, then exits quietly.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

log = logging.getLogger(__name__)

_MUTEX_PREFIX = "Local\\OmnisearchAI_"
_ERROR_ALREADY_EXISTS = 183

_kernel32 = ctypes.windll.kernel32 if hasattr(ctypes, "windll") else None
_user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None

_show_message_id: int | None = None


class SingleInstanceLock:
    """Keep a reference to the mutex handle for the process lifetime."""

    def __init__(self, app_id: str = "main") -> None:
        self._handle = None
        self.is_primary = True
        if _kernel32 is not None:
            self._acquire(app_id)
        else:
            self.is_primary = True

    def _acquire(self, app_id: str) -> None:
        kernel32 = _kernel32
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR,
        ]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.GetLastError.restype = wintypes.DWORD

        handle = kernel32.CreateMutexW(None, False, _MUTEX_PREFIX + app_id)
        if not handle:
            log.error("single-instance mutex creation failed")
            return
        if kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            self._handle = None
            self.is_primary = False
            log.info("another instance is already running")
        else:
            self._handle = handle

    def close(self) -> None:
        if _kernel32 is not None and self._handle:
            _kernel32.CloseHandle(self._handle)
            self._handle = None


def wake_existing_instance() -> None:
    """Broadcast a notify-message so the running overlay reveals itself."""
    global _show_message_id
    if _user32 is None:
        return
    if _show_message_id is None:
        reg = _user32.RegisterWindowMessageW
        reg.argtypes = [wintypes.LPCWSTR]
        reg.restype = wintypes.UINT
        _show_message_id = reg(u"OmnisearchAI_ShowOverlay")
    result = wintypes.LRESULT()
    send = _user32.SendMessageTimeoutW
    send.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(wintypes.LRESULT),
    ]
    send.restype = ctypes.c_size_t
    try:
        send(0xFFFF, _show_message_id, 0, 0, 0x0002, 500, ctypes.byref(result))
    except Exception:
        pass