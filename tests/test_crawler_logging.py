from __future__ import annotations

from types import SimpleNamespace

from scrapy.http import Request, Response

from jobs.crawler.logging import CrawlerRequestLogExtension


def test_crawler_request_log_extension_logs_response(monkeypatch) -> None:
    captured = {}

    def _log_json(logger, event, **fields):
        captured["logger"] = logger.name
        captured["event"] = event
        captured["fields"] = fields

    monkeypatch.setattr("jobs.crawler.logging.log_json", _log_json)

    request = Request("https://example.test/page")
    response = Response(
        "https://example.test/page",
        status=204,
        request=request,
    )
    response.meta["download_latency"] = 0.25

    CrawlerRequestLogExtension().response_received(
        response,
        request,
        SimpleNamespace(source_id=7, name="generic"),
    )

    assert captured["logger"] == "vchat.crawler.requests"
    assert captured["event"] == "crawler_external_request"
    assert captured["fields"]["url"] == "https://example.test/page"
    assert captured["fields"]["http_status"] == 204
    assert captured["fields"]["elapsed_seconds"] == 0.25
    assert captured["fields"]["source_id"] == 7
    assert captured["fields"]["spider"] == "generic"
    assert "access_time" in captured["fields"]
