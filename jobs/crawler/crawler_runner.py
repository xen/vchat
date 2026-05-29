#!/usr/bin/env python
"""
Standalone script to run Scrapy crawler.
Usage: python -m jobs.crawler.crawler_runner <url> <source_id> [config_json]
"""

import json
import logging
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
from vchat.source_settings import (
    DEFAULT_CRAWLER_CONCURRENT_REQUESTS,
    DEFAULT_CRAWLER_DOWNLOAD_DELAY,
    DEFAULT_CRAWLER_DOWNLOAD_TIMEOUT,
    DEFAULT_CRAWLER_USER_AGENT,
)
from vchat.logging_utils import configure_json_logging

configure_json_logging(logging.INFO)

if len(sys.argv) < 3:
    print(
        "Usage: python -m jobs.crawler.crawler_runner <url> <source_id> [config_json]"
    )
    sys.exit(1)

url = sys.argv[1]
source_id = int(sys.argv[2])

config = {}
if len(sys.argv) > 3:
    config = json.loads(sys.argv[3])

print(f"Starting crawler for URL: {url}, Source ID: {source_id}")

settings = Settings()
settings.setmodule(my_settings)
settings.set(
    "USER_AGENT",
    str(config.get("crawler_user_agent") or DEFAULT_CRAWLER_USER_AGENT),
)
concurrent_requests = config.get(
    "crawler_concurrent_requests", DEFAULT_CRAWLER_CONCURRENT_REQUESTS
)
settings.set("CONCURRENT_REQUESTS", max(1, int(concurrent_requests)))
download_delay = config.get("crawler_download_delay", DEFAULT_CRAWLER_DOWNLOAD_DELAY)
settings.set("DOWNLOAD_DELAY", max(0, int(float(download_delay))))
download_timeout = config.get(
    "crawler_download_timeout", DEFAULT_CRAWLER_DOWNLOAD_TIMEOUT
)
settings.set("DOWNLOAD_TIMEOUT", max(1, int(float(download_timeout))))

process = CrawlerProcess(settings)
process.crawl(GenericSpider, url=url, source_id=source_id, config=config)
process.start()  # This blocks until crawling is finished

print(f"Crawling completed for source {source_id}")
