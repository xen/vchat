from scrapy.linkextractors import LinkExtractor
from scrapy.spiders import CrawlSpider, Rule

from ..items import CrawledItem
import pycld2 as cld2
from dateutil import parser as date_parser
import json


class GenericSpider(CrawlSpider):
    name = "generic"

    def __init__(self, url=None, source_id=None, config=None, *args, **kwargs):
        self.source_id = int(source_id) if source_id else None
        self.allowed_domains = [url.split("//")[-1].split("/")[0]] if url else []
        self.start_urls = [url] if url else []
        self.config = config or {}

        # Parse rules from config
        le_kwargs = {}

        # XPath rules
        xpaths = [
            r["value"] for r in self.config.get("rules", []) if r["type"] == "xpath"
        ]
        if xpaths:
            le_kwargs["restrict_xpaths"] = xpaths

        # CSS rules
        css = [r["value"] for r in self.config.get("rules", []) if r["type"] == "css"]
        if css:
            le_kwargs["restrict_css"] = css

        # URL Regex rules (allow)
        regexes = [
            r["value"] for r in self.config.get("rules", []) if r["type"] == "regex"
        ]
        if regexes:
            le_kwargs["allow"] = regexes

        # Ignored params
        ignored_params = [
            r["value"] for r in self.config.get("rules", []) if r["type"] == "param"
        ]

        # Strict domain matching
        from urllib.parse import urlparse

        source_hostname = urlparse(url).hostname if url else None

        def process_links(links):
            filtered_links = []
            for link in links:
                # Strict hostname check
                if source_hostname:
                    link_hostname = urlparse(link.url).hostname
                    if link_hostname != source_hostname:
                        continue

                should_ignore = False
                if ignored_params:
                    for param in ignored_params:
                        # Check if param exists in query string
                        if f"?{param}=" in link.url or f"&{param}=" in link.url:
                            should_ignore = True
                            break

                if not should_ignore:
                    filtered_links.append(link)
            return filtered_links

        self.rules = (
            Rule(
                LinkExtractor(**le_kwargs),
                callback="parse_item",
                follow=True,
                process_links=process_links,
            ),
        )
        super().__init__(*args, **kwargs)

    def parse_item(self, response):
        print(f"Crawling {response.url}")
        item = CrawledItem()
        item["url"] = response.url
        item["source_id"] = self.source_id
        item["content_type"] = response.headers.get("Content-Type", b"").decode("utf-8")
        item["content"] = response.text
        # Extract title
        item["title"] = response.xpath("//title/text()").get()

        # Detect language
        try:
            is_reliable, _, details = cld2.detect(response.text)
            if is_reliable and details:
                lang = details[0][1]
            else:
                lang = None
        except Exception:
            lang = None

        # Detect date
        date = None

        # 1. Try Last-Modified header
        if "Last-Modified" in response.headers:
            try:
                date = date_parser.parse(
                    response.headers["Last-Modified"].decode("utf-8")
                ).isoformat()
            except Exception:
                pass

        # 2. Try meta tags if no date yet
        if not date:
            date_meta = (
                response.xpath(
                    '//meta[@property="article:published_time"]/@content'
                ).get()
                or response.xpath(
                    '//meta[@property="article:modified_time"]/@content'
                ).get()
                or response.xpath('//meta[@name="date"]/@content').get()
                or response.xpath('//meta[@name="pubdate"]/@content').get()
            )
            if date_meta:
                try:
                    date = date_parser.parse(date_meta).isoformat()
                except Exception:
                    pass

        # 3. Try Schema.org if no date yet
        if not date:
            schema_json = response.xpath(
                '//script[@type="application/ld+json"]/text()'
            ).getall()
            for schema in schema_json:
                try:
                    data = json.loads(schema)
                    if isinstance(data, dict):
                        date_val = data.get("datePublished") or data.get("dateModified")
                        if date_val:
                            date = date_parser.parse(date_val).isoformat()
                            break
                    elif isinstance(data, list):
                        for item in data:
                            date_val = item.get("datePublished") or item.get(
                                "dateModified"
                            )
                            if date_val:
                                date = date_parser.parse(date_val).isoformat()
                                break
                        if date:
                            break
                except Exception:
                    continue

        item["meta"] = {"lang": lang, "date": date}

        # We don't save content here, pipeline handles it via docling
        yield item
