from __future__ import annotations

import logging
from datetime import datetime, timezone

from scrapy import signals

from vchat.logging import log_json


logger = logging.getLogger("vchat.crawler.requests")


class CrawlerRequestLogExtension:
    @classmethod
    def from_crawler(cls, crawler):
        extension = cls()
        crawler.signals.connect(
            extension.response_received,
            signal=signals.response_received,
        )
        return extension

    def response_received(self, response, request, spider) -> None:
        log_json(
            logger,
            "crawler_external_request",
            url=response.url,
            http_status=response.status,
            access_time=datetime.now(timezone.utc).isoformat(),
            method=getattr(request, "method", None),
            elapsed_seconds=response.meta.get("download_latency"),
            source_id=getattr(spider, "source_id", None),
            spider=getattr(spider, "name", None),
        )
