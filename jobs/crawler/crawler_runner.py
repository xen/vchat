#!/usr/bin/env python
"""
Standalone script to run Scrapy crawler.
Usage: python -m jobs.crawler.crawler_runner <url> <source_id> [page_limit]
"""

import json
import sys
from pathlib import Path

# Ensure project root is importable when executed as a script
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from scrapy.crawler import CrawlerProcess
from scrapy.settings import Settings

from jobs.crawler import settings as my_settings
from jobs.crawler.spiders.generic import GenericSpider
from jobs.crawler.spiders.list import ListSpider
from jobs.crawler.spiders.sitemap import CustomSitemapSpider
from vchat.source_settings import (
    DEFAULT_CRAWLER_CONCURRENT_REQUESTS,
    DEFAULT_CRAWLER_DOWNLOAD_DELAY,
    DEFAULT_CRAWLER_USER_AGENT,
)


def main():
    if len(sys.argv) < 3:
        print(
            "Usage: python -m jobs.crawler.crawler_runner <url> <source_id> [page_limit] [source_type] [config_json]"
        )
        sys.exit(1)

    url = sys.argv[1]
    source_id = int(sys.argv[2])
    page_limit = int(sys.argv[3]) if len(sys.argv) > 3 else None

    source_type = "site"
    if len(sys.argv) > 4:
        source_type = sys.argv[4]

    config = {}
    if len(sys.argv) > 5:
        try:
            config = json.loads(sys.argv[5])
        except json.JSONDecodeError:
            print("Invalid config JSON")
            config = {}

    if page_limit is not None and page_limit <= 0:
        print("max_pages must be a positive integer")
        sys.exit(1)

    print(
        f"Starting crawler for URL: {url}, Source ID: {source_id}, Max pages: {page_limit}, Type: {source_type}"
    )

    settings = Settings()
    settings.setmodule(my_settings)
    settings.set(
        "USER_AGENT",
        str(config.get("crawler_user_agent") or DEFAULT_CRAWLER_USER_AGENT),
    )
    concurrent_requests = config.get(
        "crawler_concurrent_requests", DEFAULT_CRAWLER_CONCURRENT_REQUESTS
    )
    try:
        settings.set("CONCURRENT_REQUESTS", max(1, int(concurrent_requests)))
    except (TypeError, ValueError):
        print(
            f"Ignoring invalid crawler_concurrent_requests={concurrent_requests!r}, "
            f"using default={DEFAULT_CRAWLER_CONCURRENT_REQUESTS}"
        )
        settings.set("CONCURRENT_REQUESTS", DEFAULT_CRAWLER_CONCURRENT_REQUESTS)
    download_delay = config.get("crawler_download_delay", DEFAULT_CRAWLER_DOWNLOAD_DELAY)
    try:
        settings.set("DOWNLOAD_DELAY", max(0.0, float(download_delay)))
    except (TypeError, ValueError):
        print(
            f"Ignoring invalid crawler_download_delay={download_delay!r}, "
            f"using default={DEFAULT_CRAWLER_DOWNLOAD_DELAY}"
        )
        settings.set("DOWNLOAD_DELAY", DEFAULT_CRAWLER_DOWNLOAD_DELAY)
    if page_limit is not None:
        settings.set("CLOSESPIDER_PAGECOUNT", page_limit)
        settings.set("CLOSESPIDER_ITEMCOUNT", page_limit)

    process = CrawlerProcess(settings)

    spider_cls = GenericSpider
    if source_type == "sitemap":
        spider_cls = CustomSitemapSpider
    elif source_type == "list":
        spider_cls = ListSpider

    process.crawl(spider_cls, url=url, source_id=source_id, config=config)
    process.start()  # This blocks until crawling is finished

    print(f"Crawling completed for source {source_id}")


if __name__ == "__main__":
    main()
