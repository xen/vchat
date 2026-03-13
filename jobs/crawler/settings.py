# Scrapy settings for crawler project

BOT_NAME = "jobs.crawler"

SPIDER_MODULES = ["jobs.crawler.spiders"]
NEWSPIDER_MODULE = "jobs.crawler.spiders"

# Obey robots.txt rules
ROBOTSTXT_OBEY = False

# Configure item pipelines
ITEM_PIPELINES = {
    "jobs.crawler.pipelines.DatabasePipeline": 300,
}

# Set settings whose default value is deprecated to a future-proof value
REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
FEED_EXPORT_ENCODING = "utf-8"

# Enable or disable extensions
# EXTENSIONS = {
#    "scrapy.extensions.telnet.TelnetConsole": None,
# }

# Configure maximum concurrent requests performed by Scrapy (default: 16)
# CONCURRENT_REQUESTS = 32

# Configure a delay for requests for the same website (default: 0)
# DOWNLOAD_DELAY = 3
