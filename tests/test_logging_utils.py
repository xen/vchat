from __future__ import annotations

import io
import json
import logging
from pathlib import Path

from vchat.logging import (
    JsonLogFormatter,
    PlainLogFormatter,
    configure_logging,
    log_json,
)
from vchat.tracing import request_id_ctx


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


def test_log_formatter_includes_context_request_id() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())

    logger = logging.getLogger("tests.request_id_logger")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    token = request_id_ctx.set("req-123")
    try:
        logger.info("event_name")
    finally:
        request_id_ctx.reset(token)

    payload = json.loads(stream.getvalue())
    assert payload["request_id"] == "req-123"


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


def test_log_formatters_escape_newlines_in_untrusted_fields() -> None:
    json_stream = io.StringIO()
    json_handler = logging.StreamHandler(json_stream)
    json_handler.setFormatter(JsonLogFormatter())

    plain_stream = io.StringIO()
    plain_handler = logging.StreamHandler(plain_stream)
    plain_handler.setFormatter(PlainLogFormatter(datefmt="%Y-%m-%d %H:%M:%S"))

    logger = logging.getLogger("tests.log_injection")
    logger.handlers = [json_handler, plain_handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info("event\nforged", extra={"url": "https://example.test/\r\nnext"})

    payload = json.loads(json_stream.getvalue())
    assert payload["message"] == "event\\nforged"
    assert payload["url"] == "https://example.test/\\r\\nnext"

    plain_output = plain_stream.getvalue().strip()
    assert "event\\nforged" in plain_output
    assert "https://example.test/\\\\r\\\\nnext" in plain_output
    assert "event\nforged" not in plain_output


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
