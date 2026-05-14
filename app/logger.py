"""Centralized logging configuration."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import settings

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
_configured = False


def setup_logging() -> None:
    """Configure root logger with console + rotating file handlers.

    Idempotent: calling more than once is a no-op.
    """
    global _configured
    if _configured:
        return

    log_path = Path(settings.LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    formatter = logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT)

    root = logging.getLogger()
    root.setLevel(level)
    # Wipe any handlers added by uvicorn before our setup runs.
    root.handlers.clear()

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    console.setLevel(level)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        filename=str(log_path),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    root.addHandler(file_handler)

    # Tame noisy third-party loggers.
    logging.getLogger("uvicorn.access").setLevel(max(level, logging.INFO))

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a named logger, ensuring `setup_logging` has run."""
    if not _configured:
        setup_logging()
    return logging.getLogger(name)
