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
from jobs.crawler.spiders.general import GeneralSpider
from jobs.crawler.source_settings import (
    DEFAULT_CRAWLER_CONCURRENT_REQUESTS,
    DEFAULT_CRAWLER_DOWNLOAD_DELAY,
    DEFAULT_CRAWLER_DOWNLOAD_TIMEOUT,
)
from vchat.settings import config as project_config
from vchat.logging import configure_logging

configure_logging(
    logging.INFO,
    log_format=project_config.get("log_format", "text"),
    config_path=project_config.get("log_config"),
)

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

user_agent = project_config.get("crawler_user_agent") or "Dzen-AI/1.0"

settings = Settings()
settings.setmodule(my_settings)
settings.set("USER_AGENT", user_agent)
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
settings.set(
    "DOWNLOAD_MAXSIZE",
    max(1, int(project_config.get("raw_content_max_bytes", 10 * 1024 * 1024) or 1)),
)
settings.set("ROBOTSTXT_OBEY", not bool(config.get("ignore_robots_txt", False)))

process = CrawlerProcess(settings)
process.crawl(GeneralSpider, url=url, source_id=source_id, config=config)
process.start()  # This blocks until crawling is finished

print(f"Crawling completed for source {source_id}")
