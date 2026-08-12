"""Windows launch-at-login support via the HKCU Run registry key.

Uses the standard registry API through ctypes; no external dependency.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

_REG_HKCU = 0x80000001
_KEY_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "OmniSearchAI"

_KEY_QUERY_VALUE = 0x0001
_KEY_SET_VALUE = 0x0002
_KEY_CREATE_SUBKEY = 0x0004
_KEY_READ = 0x00020019
_REG_SZ = 1

_ERROR_FILE_NOT_FOUND = 2

_ADVAPI = None


def _api():
    global _ADVAPI
    if _ADVAPI is None:
        try:
            _ADVAPI = ctypes.windll.advapi32
        except AttributeError:
            _ADVAPI = None
    return _ADVAPI


def run_command() -> str:
    """Command the Run key will execute to relaunch the app."""
    here = Path(__file__).resolve()
    if sys.argv and sys.argv[0]:
        entry = Path(sys.argv[0]).resolve()
        if entry.suffix.lower() == ".py":
            script = str(entry)
        else:
            script = str(here.parent.parent / "main.py")
    else:
        script = str(here.parent.parent / "main.py")
    return f'"{sys.executable}" "{script}"'


def is_auto_start() -> bool:
    api = _api()
    if api is None:
        return False
    key = ctypes.c_void_p()
    if api.RegOpenKeyExW(_REG_HKCU, _KEY_RUN, 0, _KEY_READ, ctypes.byref(key)):
        return False
    try:
        buf = ctypes.create_string_buffer(2048)
        size = ctypes.c_ulong(2048)
        if api.RegQueryValueExW(key, _VALUE_NAME, None, None, buf, ctypes.byref(size)):
            return False
        return True
    finally:
        api.RegCloseKey(key)


def set_auto_start(enabled: bool) -> bool:
    api = _api()
    if api is None:
        return False
    key = ctypes.c_void_p()
    disposition = ctypes.c_ulong()
    if api.RegCreateKeyExW(
        _REG_HKCU, _KEY_RUN, 0, None, 0,
        _KEY_SET_VALUE | _KEY_QUERY_VALUE,
        None, ctypes.byref(key), ctypes.byref(disposition),
    ):
        return False
    try:
        if enabled:
            command = run_command()
            buf = ctypes.create_string_buffer(command.encode("utf-16-le") + b"\x00\x00")
            api.RegSetValueExW(key, _VALUE_NAME, 0, _REG_SZ, buf, len(buf))
        else:
            api.RegDeleteValueW(key, _VALUE_NAME)
        return True
    finally:
        api.RegCloseKey(key)