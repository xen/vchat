from __future__ import annotations

import configparser
import json
import logging
import logging.config
from datetime import datetime, timezone
from pathlib import Path
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


def _normalize_level_name(level: int | str | None, default: str = "INFO") -> str:
    if level is None:
        return default
    if isinstance(level, int):
        return logging.getLevelName(level)
    return str(level).upper()


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


class PlainLogFormatter(logging.Formatter):
    """Text formatter that appends arbitrary logging extra fields."""

    def format(self, record: logging.LogRecord) -> str:
        extras = []
        for key, value in record.__dict__.items():
            if key not in _RESERVED_LOG_RECORD_ATTRS and not key.startswith("_"):
                extras.append(f"{key}={value!r}")

        extras_text = f" {' '.join(extras)}" if extras else ""
        message = (
            f"{self.formatTime(record, self.datefmt)} "
            f"[{record.levelname}] {record.name}: {record.getMessage()}{extras_text}"
        )

        if record.exc_info:
            return f"{message}\n{self.formatException(record.exc_info)}"
        return message


def configure_logging(
    level: int | str | None = None,
    *,
    log_format: str = "text",
    config_path: str | Path | None = None,
) -> None:
    if config_path:
        logging_ini = Path(config_path)
        if not logging_ini.is_absolute():
            logging_ini = Path(__file__).resolve().parent.parent / logging_ini
    else:
        logging_ini = Path(__file__).with_name("logging.ini")
    if logging_ini.exists():
        parser = configparser.ConfigParser()
        parser.read(logging_ini)
        level_name = _normalize_level_name(level)

        formatter_name = "json" if str(log_format).lower() == "json" else "plain"
        if parser.has_option("handler_console", "formatter"):
            parser.set("handler_console", "formatter", formatter_name)

        if level is not None and parser.has_section("logger_root"):
            parser.set("logger_root", "level", level_name)

        if parser.has_section("logger_aiohttp_access"):
            parser.set("logger_aiohttp_access", "level", level_name)

        if parser.has_section("logger_aiohttp_server"):
            parser.set("logger_aiohttp_server", "level", level_name)

        logging.config.fileConfig(parser, disable_existing_loggers=False)
        return

    root = logging.getLogger()
    if level is not None:
        root.setLevel(level)

    formatter: logging.Formatter = (
        JsonLogFormatter()
        if str(log_format).lower() == "json"
        else PlainLogFormatter(datefmt="%Y-%m-%d %H:%M:%S")
    )
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        root.addHandler(handler)
        return

    for handler in root.handlers:
        handler.setFormatter(formatter)


def configure_json_logging(level: int | str | None = None) -> None:
    configure_logging(level, log_format="json")


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
