"""Central, persistent logging for the API and coordinated background work."""

from __future__ import annotations

import json
import logging
import os
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from eplan_runtime import LOG_ROOT


LOG_FILE = Path(os.getenv("EPLAN_LOG_FILE", LOG_ROOT / "eplan-system.jsonl")).resolve()
LOG_LEVEL = os.getenv("EPLAN_LOG_LEVEL", "INFO").upper()
LOG_MAX_BYTES = int(os.getenv("EPLAN_LOG_MAX_BYTES", str(10 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("EPLAN_LOG_BACKUP_COUNT", "5"))

_EXTRA_FIELDS = (
    "request_id",
    "method",
    "path",
    "status_code",
    "duration_ms",
    "client",
    "action",
    "job_id",
    "document_id",
    "batch_id",
    "document_name",
    "progress",
    "stage",
    "current",
    "total",
    "error_type",
)
_configured = False
_request_id: ContextVar[str | None] = ContextVar("eplan_request_id", default=None)


class JsonLineFormatter(logging.Formatter):
    """Produce one searchable JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "component": record.name.removeprefix("eplan."),
            "message": record.getMessage(),
        }
        for field in _EXTRA_FIELDS:
            value = getattr(record, field, None)
            if field == "request_id" and value is None:
                value = _request_id.get()
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging() -> Path:
    """Configure console output plus a bounded set of persistent log files."""
    global _configured
    if _configured:
        return LOG_FILE

    level = getattr(logging, LOG_LEVEL, logging.INFO)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    formatter = JsonLineFormatter()
    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=max(LOG_MAX_BYTES, 1024),
        backupCount=max(LOG_BACKUP_COUNT, 1),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root = logging.getLogger("eplan")
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    root.propagate = False
    _configured = True
    return LOG_FILE


def get_logger(component: str) -> logging.Logger:
    return logging.getLogger(f"eplan.{component}")


def bind_request_id(request_id: str) -> Token[str | None]:
    return _request_id.set(request_id)


def reset_request_id(token: Token[str | None]) -> None:
    _request_id.reset(token)
