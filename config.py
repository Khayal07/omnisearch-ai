"""Configuration manager for OmniSearch AI.

Responsible for:
  * user-editable preferences (config.json, stored under %APPDATA%\\omnisearch-ai)
  * option files (.env style, parsed without external dependencies)
  * sensitive provider API keys, encrypted with the Windows DPAPI
    (CryptProtectData / CryptUnprotectData via ctypes)

API key lookup order: secrets store (Settings UI) > .env > OS environment.
"""

from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path
from typing import Any

APP_NAME = "omnisearch-ai"

DEFAULT_CONFIG: dict[str, Any] = {
    "provider": "openai",
    "model": "",
    "base_url": "",
    "hotkey": "Alt+Space",
    "theme": "dark",
    "animations": True,
    "auto_start": False,
    "inject_clipboard": True,
    "temperature": 0.7,
    "max_tokens": 2048,
    "history_limit": 500,
    "system_prompt": (
        "You are OmniSearch AI, a concise, accurate desktop assistant. "
        "Answer directly using Markdown for formatting."
    ),
    "providers": {
        "openai": {
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
        },
        "gemini": {
            "base_url": "https://generativelanguage.googleapis.com",
            "model": "gemini-2.0-flash",
        },
        "ollama": {
            "base_url": "http://localhost:11434",
            "model": "qwen2.5:7b",
        },
        "custom": {
            "base_url": "",
            "model": "",
        },
    },
    "fallback_order": ["ollama", "openai", "gemini"],
    "quick_actions": {
        "/code": "You are a senior software engineer. Provide concise, correct "
                 "code with no extra commentary.",
        "/explain": "Explain the following clearly and pedagogically, using "
                    "concrete examples.",
        "/sum": "Summarize the following into 3-5 tight bullet points.",
        "/web": "Answer as if you have real-time web access; cite sources "
                "inline where relevant.",
        "/fix": "Find bugs or issues in the following code and show the "
                "corrected version.",
    },
}

# Maps provider -> expected environment keys (base_url / model / api_key).
_ENV_KEYS: dict[str, dict[str, str]] = {
    "openai": {"base_url": "OPENAI_BASE_URL", "model": "OPENAI_MODEL", "key": "OPENAI_API_KEY"},
    "gemini": {"base_url": "GEMINI_BASE_URL", "model": "GEMINI_MODEL", "key": "GEMINI_API_KEY"},
    "ollama": {"base_url": "OLLAMA_BASE_URL", "model": "OLLAMA_MODEL", "key": ""},
    "custom": {"base_url": "CUSTOM_BASE_URL", "model": "CUSTOM_MODEL", "key": "CUSTOM_API_KEY"},
}


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse `.env`-style content into a dictionary.

    Supports blank lines, ``#`` comments, quoting, and an optional ``export``
    prefix. Values are kept as raw strings.
    """
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key] = value
    return result


def default_config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


def _deep_merge(base: dict, overlay: dict) -> dict:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class SecretsBackend:
    """Interface for the encrypted secrets store."""

    def encrypt(self, payload: bytes) -> bytes:
        raise NotImplementedError

    def decrypt(self, payload: bytes) -> bytes:
        raise NotImplementedError


class DpapiBackend(SecretsBackend):
    """Windows DPAPI encryption for current-user secrets (no entropy)."""

    _PROTECT_UI_FORBIDDEN = 0x1

    def __init__(self) -> None:
        try:
            self._crypt32 = ctypes.windll.crypt32
            self._kernel32 = ctypes.windll.kernel32
        except AttributeError:
            raise OSError("Windows DPAPI is only available on Windows")

    @staticmethod
    def _blob(data: bytes) -> tuple:
        buf = ctypes.create_string_buffer(data, len(data))
        ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))
        blob = DpapiBackend._DATA_BLOB(len(data), ptr)
        return blob, buf

    def encrypt(self, payload: bytes) -> bytes:
        blob_in, keepalive = self._blob(payload)
        blob_out = self._DATA_BLOB()
        ok = self._crypt32.CryptProtectData(
            ctypes.byref(blob_in),
            None, None, None, None,
            self._PROTECT_UI_FORBIDDEN,
            ctypes.byref(blob_out),
        )
        if not ok:
            raise ctypes.WinError()
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            self._kernel32.LocalFree(blob_out.pbData)

    def decrypt(self, payload: bytes) -> bytes:
        blob_in, keepalive = self._blob(payload)
        blob_out = self._DATA_BLOB()
        ok = self._crypt32.CryptUnprotectData(
            ctypes.byref(blob_in),
            None, None, None, None, 0,
            ctypes.byref(blob_out),
        )
        if not ok:
            raise ctypes.WinError()
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            self._kernel32.LocalFree(blob_out.pbData)

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]


class PlaintextBackend(SecretsBackend):
    """Fallback backend storing plain bytes (used for tests / non-Windows)."""

    def encrypt(self, payload: bytes) -> bytes:
        return payload

    def decrypt(self, payload: bytes) -> bytes:
        return payload


class SecretsManager:
    """Persists a small JSON dict of secrets, encrypted with DPAPI."""

    def __init__(self, path: Path | str, backend: str = "dpapi") -> None:
        self.path = Path(path)
        if backend == "plaintext":
            self._backend: SecretsBackend = PlaintextBackend()
        else:
            self._backend = DpapiBackend()
        self._data: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            payload = self._backend.decrypt(self.path.read_bytes())
            data = json.loads(payload.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def get(self, key: str) -> str | None:
        value = self._data.get(key)
        return value if value else None

    def set(self, key: str, value: str | None) -> None:
        if value:
            self._data[key] = value
        else:
            self._data.pop(key, None)
        self._flush()

    def _flush(self) -> None:
        payload = json.dumps(self._data).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(self._backend.encrypt(payload))


class ConfigManager:
    """Loads and persists application configuration.

    Preference order (highest first): config.json -> .env files -> defaults.
    API keys: secrets store -> .env -> OS environment.
    """

    def __init__(
        self,
        config_dir: Path | str | None = None,
        secrets_backend: str = "dpapi",
    ) -> None:
        self.config_dir = Path(config_dir) if config_dir else default_config_dir()
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.config_dir / "config.json"
        self.secrets = SecretsManager(self.config_dir / "secrets.dat", backend=secrets_backend)
        self._env: dict[str, str] = {}
        self._data: dict[str, Any] = json.loads(json.dumps(DEFAULT_CONFIG))
        self.load()

    # -- lifecycle ---------------------------------------------------------

    def load(self) -> None:
        self._env = {}
        for dotenv in (self.config_dir / ".env", Path.cwd() / ".env"):
            if dotenv.is_file():
                try:
                    self._env.update(parse_dotenv(dotenv.read_text(encoding="utf-8")))
                except OSError:
                    pass
        if self.config_path.is_file():
            try:
                stored = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(stored, dict):
                    self._data = _deep_merge(self._data, stored)
            except (OSError, json.JSONDecodeError):
                pass

    def save(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # -- value access ------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        node = self._data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, key: str, value: Any) -> None:
        parts = key.split(".")
        node = self._data
        for part in parts[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value
        self.save()

    def quick_action_template(self, name: str) -> str | None:
        return self.get(f"quick_actions.{name}", None)

    # -- provider helpers --------------------------------------------------

    def provider_config(self, name: str) -> dict[str, str]:
        defaults = dict(self.get(f"providers.{name}", {}))
        env = _ENV_KEYS.get(name, {})
        if env.get("base_url") and self._env.get(env["base_url"]):
            defaults["base_url"] = self._env[env["base_url"]]
        elif env.get("base_url") and os.environ.get(env["base_url"]):
            defaults["base_url"] = os.environ[env["base_url"]]
        if env.get("model") and self._env.get(env["model"]):
            defaults["model"] = self._env[env["model"]]
        elif env.get("model") and os.environ.get(env["model"]):
            defaults["model"] = os.environ[env["model"]]
        return defaults

    def api_key_env_name(self, provider: str) -> str:
        return _ENV_KEYS.get(provider, {}).get("key", "")

    def api_key(self, provider: str) -> str | None:
        key = self.secrets.get(f"api_key.{provider}")
        if key:
            return key
        env_name = self.api_key_env_name(provider)
        if env_name:
            if self._env.get(env_name):
                return self._env[env_name]
            return os.environ.get(env_name) or None
        return None

    def set_api_key(self, provider: str, value: str | None) -> None:
        self.secrets.set(f"api_key.{provider}", value)

    # -- derived paths -----------------------------------------------------

    @property
    def history_db_path(self) -> Path:
        return self.config_dir / "history.db"

    @property
    def log_dir(self) -> Path:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / APP_NAME / "logs"