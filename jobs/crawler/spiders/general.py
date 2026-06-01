import json
from urllib.parse import urlparse

import pycld2 as cld2
from dateutil import parser as date_parser
from scrapy.linkextractors import LinkExtractor
from scrapy.http import Request
from scrapy.spiders import CrawlSpider, Rule

from ..items import CrawledItem
from ..seed_urls import iter_source_seed_urls
from ..url_rules import ignored_query_params, normalize_url_for_queue, url_allowed_by_rules


class GeneralSpider(CrawlSpider):
    name = "general"

    def __init__(self, url=None, source_id=None, config=None, *args, **kwargs):
        self.source_id = int(source_id) if source_id else None
        self.allowed_domains = [url.split("//")[-1].split("/")[0]] if url else []
        self.start_urls = [url] if url else []
        self.config = config or {}
        self.source_rules = list(self.config.get("rules", []) or [])
        self.tracked_sources = list(self.config.get("tracked_sources", []) or [])
        self.single_page_only = bool(self.config.get("single_page_only"))
        self._link_extractor = None

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

        source_hostname = urlparse(url).hostname if url else None
        tracked_sources_by_host: dict[str, dict] = {}
        for tracked_source in self.tracked_sources:
            tracked_uri = (tracked_source.get("uri") or "").strip()
            tracked_host = (urlparse(tracked_uri).hostname or "").lower()
            if not tracked_host:
                continue
            tracked_sources_by_host[tracked_host] = {
                "id": tracked_source.get("id"),
                "rules": list(tracked_source.get("rules", []) or []),
            }
        if source_hostname and source_hostname.lower() not in tracked_sources_by_host:
            tracked_sources_by_host[source_hostname.lower()] = {
                "id": self.source_id,
                "rules": self.source_rules,
            }

        def process_links(links):
            filtered_links = []
            seen_urls: set[str] = set()
            for link in links:
                raw_url = (link.url or "").strip()
                link_hostname = (urlparse(raw_url).hostname or "").lower()
                if not link_hostname:
                    continue
                target_source = tracked_sources_by_host.get(link_hostname)
                if target_source is None:
                    continue
                target_rules = list(target_source.get("rules", []) or [])
                normalized_url = normalize_url_for_queue(raw_url, target_rules)
                if not normalized_url:
                    continue

                if not url_allowed_by_rules(normalized_url, target_rules):
                    continue

                should_ignore = False
                target_ignored_params = ignored_query_params(target_rules)
                if target_ignored_params:
                    for param in target_ignored_params:
                        # Check if param exists in query string
                        if f"?{param}=" in raw_url or f"&{param}=" in raw_url:
                            should_ignore = True
                            break

                if not should_ignore:
                    if normalized_url in seen_urls:
                        continue
                    seen_urls.add(normalized_url)
                    link.url = normalized_url
                    filtered_links.append(link)
            return filtered_links

        self._process_links = process_links
        self._link_extractor = LinkExtractor(**le_kwargs)

        self.rules = (
            ()
            if self.single_page_only
            else (
                Rule(
                    self._link_extractor,
                    callback="parse_item",
                    follow=True,
                    process_links=process_links,
                ),
            )
        )
        super().__init__(*args, **kwargs)

    def parse_start_url(self, response, **kwargs):
        """Index start URLs as content (not just extract links from them)."""
        return self.parse_item(response)

    def start_requests(self):
        yield from super().start_requests()

        if self.single_page_only:
            return

        for seed_url in iter_source_seed_urls(
            self.source_id,
            exclude=self.start_urls,
        ):
            yield Request(seed_url, dont_filter=False)

    def parse_item(self, response):
        print(f"Crawling {response.url}")
        item = CrawledItem()
        item["url"] = normalize_url_for_queue(response.request.url, self.source_rules)
        item["final_url"] = normalize_url_for_queue(response.url, self.source_rules)
        item["http_status"] = response.status
        item["etag"] = response.headers.get("ETag", b"").decode("utf-8") or None
        item["source_id"] = self.source_id
        item["content_type"] = response.headers.get("Content-Type", b"").decode("utf-8")
        item["content"] = response.text
        extracted_links = []
        if self._link_extractor is not None:
            extracted_links = [
                link.url
                for link in self._process_links(self._link_extractor.extract_links(response))
            ]
        item["out_links"] = extracted_links
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
