"""Verification tests for the configuration manager and secrets store."""

import json
import os
from pathlib import Path

import pytest

from config import (
    DEFAULT_CONFIG,
    ConfigManager,
    SecretsManager,
    parse_dotenv,
    _deep_merge,
    default_config_dir,
)


@pytest.fixture(autouse=True)
def isolate_environ(monkeypatch):
    """Remove provider env vars so tests do not inherit the host environment."""
    for var in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
                "GEMINI_API_KEY", "OLLAMA_BASE_URL", "CUSTOM_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("APPDATA", "")


# -- parse_dotenv ---------------------------------------------------------

class TestParseDotenv:
    def test_basic_pairs(self):
        assert parse_dotenv("A=1\nB=two\n") == {"A": "1", "B": "two"}

    def test_ignores_comments_and_blank_lines(self):
        assert parse_dotenv("\n# comment\n  \nKEY=value\n") == {"KEY": "value"}

    def test_inline_comment_after_value_kept(self):
        parsed = parse_dotenv("K=v # not stripped")
        assert parsed["K"] == "v # not stripped"

    def test_quotes_stripped(self):
        assert parse_dotenv('K="quoted"\nJ=\'single\'') == {"K": "quoted", "J": "single"}

    def test_export_prefix(self):
        assert parse_dotenv("export TOKEN=abc") == {"TOKEN": "abc"}

    def test_malformed_lines_skipped(self):
        assert parse_dotenv("NOVALUE\n=orphan\nK=1") == {"K": "1"}

    def test_surrounding_whitespace_trimmed(self):
        assert parse_dotenv("   KEY   =   value   ") == {"KEY": "value"}


# -- deep merge -----------------------------------------------------------

class TestDeepMerge:
    def test_nested_merge(self):
        base = {"a": {"b": 1, "c": 2}, "x": 1}
        overlay = {"a": {"c": 3}, "y": 2}
        assert _deep_merge(base, overlay) == {"a": {"b": 1, "c": 3}, "x": 1, "y": 2}

    def test_non_dict_overlay_replaces(self):
        base = {"a": {"b": 1}}
        assert _deep_merge(base, {"a": 5}) == {"a": 5}


# -- ConfigManager --------------------------------------------------------

@pytest.fixture
def cfg(tmp_path):
    return ConfigManager(config_dir=tmp_path, secrets_backend="plaintext")


class TestConfigManager:
    def test_defaults_applied(self, cfg):
        assert cfg.get("provider") == "openai"
        assert cfg.get("hotkey") == "Alt+Space"
        assert cfg.get("providers.openai.model") == "gpt-4o-mini"

    def test_config_dir_created(self, tmp_path):
        target = tmp_path / "nested" / "config"
        cfg = ConfigManager(config_dir=target, secrets_backend="plaintext")
        assert target.is_dir()
        assert cfg.config_path == target / "config.json"

    def test_set_get_roundtrip_in_memory(self, cfg):
        cfg.set("theme", "light")
        assert cfg.get("theme") == "light"

    def test_persistence_roundtrip(self, tmp_path):
        first = ConfigManager(config_dir=tmp_path, secrets_backend="plaintext")
        first.set("temperature", 0.3)
        first.set("providers.openai.model", "custom-model")

        second = ConfigManager(config_dir=tmp_path, secrets_backend="plaintext")
        assert second.get("temperature") == 0.3
        assert second.get("providers.openai.model") == "custom-model"

    def test_corrupt_config_falls_back_to_defaults(self, tmp_path):
        target = tmp_path / "corrupt"
        target.mkdir()
        (target / "config.json").write_text("{ not valid json", encoding="utf-8")
        cfg = ConfigManager(config_dir=target, secrets_backend="plaintext")
        assert cfg.get("provider") == "openai"

    def test_set_unknown_key_creates_path(self, cfg):
        cfg.set("branding.name", "Omni")
        assert cfg.get("branding.name") == "Omni"

    def test_quick_action_template(self, cfg):
        assert cfg.quick_action_template("/sum") == DEFAULT_CONFIG["quick_actions"]["/sum"]
        assert cfg.quick_action_template("/missing") is None

    def test_history_db_path(self, tmp_path):
        cfg = ConfigManager(config_dir=tmp_path, secrets_backend="plaintext")
        assert cfg.history_db_path == tmp_path / "history.db"

    def test_dotenv_file_loaded(self, tmp_path):
        (tmp_path / ".env").write_text("MY_FLAG=yes\nQUOTED=\"val\"", encoding="utf-8")
        cfg = ConfigManager(config_dir=tmp_path, secrets_backend="plaintext")
        assert cfg._env["MY_FLAG"] == "yes"
        assert cfg._env["QUOTED"] == "val"

    def test_env_does_not_override_config_json(self, tmp_path):
        cfg = ConfigManager(config_dir=tmp_path, secrets_backend="plaintext")
        cfg.set("theme", "dark")
        (tmp_path / ".env").write_text("THEME=light\n", encoding="utf-8")
        reloaded = ConfigManager(config_dir=tmp_path, secrets_backend="plaintext")
        assert reloaded.get("theme") == "dark"


# -- provider config / API keys ------------------------------------------

class TestApiKeys:
    def test_provider_config_defaults(self, cfg):
        assert cfg.provider_config("openai")["base_url"] == "https://api.openai.com/v1"

    def test_provider_config_env_override(self, cfg, tmp_path):
        (tmp_path / ".env").write_text("OPENAI_MODEL=env-model\n", encoding="utf-8")
        reloaded = ConfigManager(config_dir=tmp_path, secrets_backend="plaintext")
        assert reloaded.provider_config("openai")["model"] == "env-model"

    def test_provider_config_os_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_BASE_URL", "http://proxy:8000/v1")
        cfg = ConfigManager(config_dir=tmp_path, secrets_backend="plaintext")
        assert cfg.provider_config("openai")["base_url"] == "http://proxy:8000/v1"

    def test_api_key_from_secrets_store(self, tmp_path):
        cfg = ConfigManager(config_dir=tmp_path, secrets_backend="plaintext")
        cfg.set_api_key("openai", "sk-test-123")
        assert cfg.api_key("openai") == "sk-test-123"

    def test_api_key_from_dotenv(self, tmp_path):
        (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-dotenv\n", encoding="utf-8")
        cfg = ConfigManager(config_dir=tmp_path, secrets_backend="plaintext")
        assert cfg.api_key("openai") == "sk-dotenv"

    def test_api_key_from_os_environ(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-osenv")
        cfg = ConfigManager(config_dir=tmp_path, secrets_backend="plaintext")
        assert cfg.api_key("openai") == "sk-osenv"

    def test_secrets_store_wins_over_dotenv(self, tmp_path):
        (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-dotenv\n", encoding="utf-8")
        cfg = ConfigManager(config_dir=tmp_path, secrets_backend="plaintext")
        cfg.set_api_key("openai", "sk-ui")
        assert cfg.api_key("openai") == "sk-ui"

    def test_ollama_has_no_api_key_env(self, cfg):
        assert cfg.api_key("ollama") is None
        assert cfg.api_key_env_name("ollama") == ""

    def test_secrets_persist_across_instances(self, tmp_path):
        first = ConfigManager(config_dir=tmp_path, secrets_backend="plaintext")
        first.set_api_key("gemini", "gz-123")
        second = ConfigManager(config_dir=tmp_path, secrets_backend="plaintext")
        assert second.api_key("gemini") == "gz-123"

    def test_clear_api_key(self, tmp_path):
        cfg = ConfigManager(config_dir=tmp_path, secrets_backend="plaintext")
        cfg.set_api_key("openai", "sk-abc")
        cfg.set_api_key("openai", None)
        assert cfg.api_key("openai") is None


# -- secrets store --------------------------------------------------------

class TestSecretsStore:
    def test_secrets_file_created(self, tmp_path):
        cfg = ConfigManager(config_dir=tmp_path, secrets_backend="plaintext")
        cfg.set_api_key("openai", "sk-x")
        assert (tmp_path / "secrets.dat").is_file()

    def test_secrets_manager_get_from_json(self, tmp_path):
        store = SecretsManager(tmp_path / "secrets.dat", backend="plaintext")
        store.set("a", "b")
        assert SecretsManager(tmp_path / "secrets.dat", backend="plaintext").get("a") == "b"


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-only")
class TestDpapi:
    def test_dpapi_roundtrip(self, tmp_path):
        store = SecretsManager(tmp_path / "secrets.dat", backend="dpapi")
        store.set("key", "secret-value")
        reloaded = SecretsManager(tmp_path / "secrets.dat", backend="dpapi")
        assert reloaded.get("key") == "secret-value"

    def test_dpapi_file_is_not_plaintext(self, tmp_path):
        store = SecretsManager(tmp_path / "secrets.dat", backend="dpapi")
        store.set("key", "top-secret")
        assert b"top-secret" not in (tmp_path / "secrets.dat").read_bytes()

    def test_dpapi_tampered_file_ignored(self, tmp_path):
        store = SecretsManager(tmp_path / "secrets.dat", backend="dpapi")
        store.set("key", "v")
        (tmp_path / "secrets.dat").write_bytes(b"garbage")
        assert SecretsManager(tmp_path / "secrets.dat", backend="dpapi").get("key") is None


# -- paths ----------------------------------------------------------------

class TestPaths:
    def test_default_config_dir_under_appdata(self):
        assert default_config_dir().name == "omnisearch-ai"