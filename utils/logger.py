"""Rotating file + console logging for all modules."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_HANDLERS_DONE = False


def default_log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "omnisearch-ai" / "logs"


def setup_logging(
    name: str = "omnisearch",
    log_dir: Path | str | None = None,
    level: int = logging.INFO,
) -> logging.Logger:
    """Configure (once) a rotating file handler plus a stderr console handler."""
    global _HANDLERS_DONE
    logger = logging.getLogger(name)
    logger.setLevel(level)
    if _HANDLERS_DONE and logger.handlers:
        return logger

    formatter = logging.Formatter(_LOG_FORMAT)
    log_dir = Path(log_dir) if log_dir else default_log_dir()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / f"{name}.log",
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        pass

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    logger.propagate = False
    _HANDLERS_DONE = True
    return logger


def get_logger(name: str = "omnisearch") -> logging.Logger:
    return logging.getLogger(name)