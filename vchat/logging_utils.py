from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping


_RESERVED_LOG_RECORD_ATTRS = frozenset(
    logging.LogRecord(
        name="",
        level=0,
        pathname="",
        lineno=0,
        msg="",
        args=(),
        exc_info=None,
    ).__dict__
) | {"message", "asctime"}


class JsonLogFormatter(logging.Formatter):
    """JSON formatter that preserves arbitrary logging extra fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created,
                tz=timezone.utc,
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _RESERVED_LOG_RECORD_ATTRS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_json_logging(level: int | str | None = None) -> None:
    root = logging.getLogger()
    if level is not None:
        root.setLevel(level)

    formatter = JsonLogFormatter()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        root.addHandler(handler)
        return

    for handler in root.handlers:
        handler.setFormatter(formatter)


def log_json(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    fields: Mapping[str, Any] | None = None,
    **extra_fields: Any,
) -> None:
    payload = {
        (
            key
            if isinstance(key, str)
            and key not in _RESERVED_LOG_RECORD_ATTRS
            and not key.startswith("_")
            else f"field_{key}"
        ): value
        for key, value in dict(fields or {}, **extra_fields).items()
    }
    logger.log(level, event, extra=payload)
