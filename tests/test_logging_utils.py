from __future__ import annotations

import io
import json
import logging
from pathlib import Path

from vchat.logging_utils import (
    JsonLogFormatter,
    PlainLogFormatter,
    configure_logging,
    log_json,
)


def test_log_json_outputs_expandable_fields() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())

    logger = logging.getLogger("tests.json_logger")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    log_json(
        logger,
        "event_name",
        url="https://example.test",
        http_status=200,
        fields={"message": "reserved"},
    )

    payload = json.loads(stream.getvalue())
    assert payload["message"] == "event_name"
    assert payload["url"] == "https://example.test"
    assert payload["http_status"] == 200
    assert payload["field_message"] == "reserved"


def test_plain_log_formatter_outputs_text_with_extra_fields() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(PlainLogFormatter(datefmt="%Y-%m-%d %H:%M:%S"))

    logger = logging.getLogger("tests.plain_logger")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    log_json(
        logger,
        "event_name",
        url="https://example.test",
        http_status=200,
    )

    output = stream.getvalue().strip()
    assert "[INFO] tests.plain_logger: event_name" in output
    assert "url='https://example.test'" in output
    assert "http_status=200" in output


def test_configure_logging_uses_text_formatter_by_default() -> None:
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level

    try:
        handler = logging.StreamHandler(io.StringIO())
        root.handlers = [handler]
        configure_logging(logging.INFO)
        assert isinstance(root.handlers[0].formatter, PlainLogFormatter)
    finally:
        root.handlers = original_handlers
        root.setLevel(original_level)


def test_configure_logging_uses_ini_and_switches_formatter() -> None:
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level

    try:
        root.handlers = []
        configure_logging(
            logging.WARNING,
            log_format="json",
            config_path=Path("vchat/logging.ini"),
        )
        assert root.level == logging.WARNING
        assert root.handlers
        assert isinstance(root.handlers[0].formatter, JsonLogFormatter)
    finally:
        root.handlers = original_handlers
        root.setLevel(original_level)
