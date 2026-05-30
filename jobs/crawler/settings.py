SPIDER_MODULES = ["jobs.crawler.spiders"]
NEWSPIDER_MODULE = "jobs.crawler.spiders"

ROBOTSTXT_OBEY = True

AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_TARGET_CONCURRENCY = 1.0
AUTOTHROTTLE_MAX_DELAY = 60

ITEM_PIPELINES = {
    "jobs.crawler.pipelines.DatabasePipeline": 300,
}

REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
FEED_EXPORT_ENCODING = "utf-8"

EXTENSIONS = {
    "jobs.crawler.logging.CrawlerRequestLogExtension": 500,
}

try:
    from jobs.crawler import local_settings  # noqa: F401
except ImportError:
    pass
