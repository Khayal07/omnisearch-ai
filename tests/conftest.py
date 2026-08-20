"""Shared pytest fixtures: makes the repo root importable and pins Qt to the
offscreen platform so widget tests run headless on CI."""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_API", "pyside6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def isolate_environ(monkeypatch):
    """Remove provider env vars so tests do not inherit the host environment."""
    for var in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
                "GEMINI_API_KEY", "OLLAMA_BASE_URL", "CUSTOM_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("APPDATA", "")