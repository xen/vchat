from __future__ import annotations

import io
import json
import logging

from vchat.logging_utils import JsonLogFormatter, log_json


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
