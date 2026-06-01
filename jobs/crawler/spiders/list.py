import scrapy

from ..items import CrawledItem
from ..seed_urls import iter_source_seed_urls
from ..url_rules import normalize_url_for_queue


class ListSpider(scrapy.Spider):
    name = "list"

    def __init__(self, url=None, source_id=None, config=None, *args, **kwargs):
        self.list_url = url
        self.source_id = int(source_id) if source_id else None
        self.config = config or {}
        self.source_rules = list(self.config.get("rules", []) or [])
        super().__init__(*args, **kwargs)

    def start_requests(self):
        yield scrapy.Request(self.list_url, callback=self.parse_list)

        for seed_url in iter_source_seed_urls(
            self.source_id,
            exclude=[self.list_url],
        ):
            yield scrapy.Request(seed_url, callback=self.parse_item, dont_filter=False)

    def parse_list(self, response):
        urls = response.text.splitlines()
        for url in urls:
            url = url.strip()
            if url:
                yield scrapy.Request(url, callback=self.parse_item)

    def parse_item(self, response):
        item = CrawledItem()
        item["url"] = normalize_url_for_queue(response.request.url, self.source_rules)
        item["final_url"] = normalize_url_for_queue(response.url, self.source_rules)
        item["http_status"] = response.status
        item["etag"] = response.headers.get("ETag", b"").decode("utf-8") or None
        item["source_id"] = self.source_id
        item["content_type"] = response.headers.get("Content-Type", b"").decode("utf-8")
        item["content"] = response.text
        item["title"] = response.xpath("//title/text()").get()
        item["out_links"] = []
        yield item
