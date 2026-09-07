"""Structured logging to console and file.

Emits human-readable key=value lines on the console and JSON lines to a file so
that a run can be inspected either way (spec 12).  A counting handler tallies
warnings and errors for the ``Run Log`` sheet (spec 10.4).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOGGER_NAME = "over15"

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    """Return the ``extra=`` fields attached to *record*."""
    return {k: v for k, v in record.__dict__.items() if k not in _RESERVED}


class ConsoleFormatter(logging.Formatter):
    """``HH:MM:SS LEVEL  message key=value`` — readable but still structured."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created, tz=UTC).strftime("%H:%M:%S")
        line = f"{stamp} {record.levelname:<7} {record.getMessage()}"
        extras = " ".join(f"{k}={v}" for k, v in _extras(record).items())
        if extras:
            line = f"{line}  {extras}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for machine consumption."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in _extras(record).items():
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                value = repr(value)
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class CountingHandler(logging.Handler):
    """Tallies warnings and errors, and keeps the messages for the Run Log."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return
        if record.levelno >= logging.ERROR:
            self.errors.append(message)
        else:
            self.warnings.append(message)

    @property
    def warning_count(self) -> int:
        return len(self.warnings)

    @property
    def error_count(self) -> int:
        return len(self.errors)

    def summary(self, messages: list[str], limit: int = 5) -> str:
        if not messages:
            return ""
        shown = "; ".join(messages[:limit])
        if len(messages) > limit:
            shown = f"{shown}; (+{len(messages) - limit} more)"
        return shown


def get_logger(name: str | None = None) -> logging.Logger:
    """Return the application logger, or a child of it."""
    return logging.getLogger(LOGGER_NAME if name is None else f"{LOGGER_NAME}.{name}")


def setup_logging(
    level: str = "INFO",
    log_file: str | Path | None = None,
    *,
    stream: Any = None,
) -> CountingHandler:
    """Configure the application logger and return the warning/error counter."""
    logger = get_logger()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    console = logging.StreamHandler(stream if stream is not None else sys.stderr)
    console.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    console.setFormatter(ConsoleFormatter())
    logger.addHandler(console)

    if log_file is not None:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(JsonFormatter())
        logger.addHandler(file_handler)

    counter = CountingHandler()
    logger.addHandler(counter)
    return counter
