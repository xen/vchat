import scrapy
from scrapy.spiders import SitemapSpider

from ..items import CrawledItem
from ..seed_urls import iter_source_seed_urls


class CustomSitemapSpider(SitemapSpider):
    name = "sitemap"
    sitemap_follow = [r".*"]

    def __init__(self, url=None, source_id=None, config=None, *args, **kwargs):
        self.sitemap_urls = [url] if url else []
        self.source_id = int(source_id) if source_id else None
        self.config = config or {}

        # Apply allow-list regex rules if provided (reuse the same rule format as site sources)
        regex_rules = [
            rule["value"]
            for rule in self.config.get("rules", [])
            if rule.get("type") == "regex" and rule.get("value")
        ]
        if regex_rules:
            self.sitemap_rules = [(pattern, "parse_page") for pattern in regex_rules]
        else:
            self.sitemap_rules = [(r"", "parse_page")]

        super().__init__(*args, **kwargs)

    def start_requests(self):
        yield from super().start_requests()

        for seed_url in iter_source_seed_urls(
            self.source_id,
            exclude=self.sitemap_urls,
        ):
            yield scrapy.Request(seed_url, callback=self.parse_page, dont_filter=False)

    def parse_page(self, response):
        item = CrawledItem()
        item["url"] = response.url
        item["source_id"] = self.source_id
        item["content_type"] = response.headers.get("Content-Type", b"").decode("utf-8")
        item["content"] = response.text
        item["title"] = response.xpath("//title/text()").get()
        yield item

    # Keep compatibility with default callback name when no sitemap_rules apply
    def parse(self, response):
        yield from self.parse_page(response)
