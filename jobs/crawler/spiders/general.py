import json
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import pycld2 as cld2
from dateutil import parser as date_parser
from scrapy.link import Link
from scrapy.linkextractors import IGNORED_EXTENSIONS, LinkExtractor
from scrapy.http import HtmlResponse, Request, TextResponse
from scrapy.spiders import CrawlSpider, Rule
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..items import CrawledItem
from ..seed_urls import iter_priority_crawl_queue
from ..url_rules import (
    ignored_query_params,
    normalize_url_for_queue,
    url_allowed_by_rules,
    extract_hostname,
)
from jobs.db import create_sync_engine
from vchat.models.data import Page
from vchat.views.projects.page_status import PageStatusError

DOWNLOADABLE_DOCUMENT_EXTENSIONS = {
    "doc",
    "docx",
    "pdf",
    "pptx",
}


class GeneralSpider(CrawlSpider):
    name = "general"
    _HTTP_STATUS_META = {"handle_httpstatus_all": True}

    def __init__(self, url=None, source_id=None, config=None, *args, **kwargs):
        self.source_id = int(source_id) if source_id else None
        self.allowed_domains = [url.split("//")[-1].split("/")[0]] if url else []
        self.start_urls = [url] if url else []
        self.config = config or {}
        self.source_rules = list(self.config.get("rules", []) or [])
        self.tracked_sources = list(self.config.get("tracked_sources", []) or [])
        self.single_page_only = bool(self.config.get("single_page_only"))
        self.crawl_run_id = self.config.get("crawl_run_id")
        self._link_extractor = None
        self._discovery_eligibility_cache: dict[tuple[int, str], bool] = {}
        self._engine = create_sync_engine()
        self._tracked_sources_by_host: dict[str, dict] = {}

        # Parse rules from config
        le_kwargs: dict[str, Any] = {}

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
        le_kwargs["deny_extensions"] = [
            extension
            for extension in IGNORED_EXTENSIONS
            if extension not in DOWNLOADABLE_DOCUMENT_EXTENSIONS
        ]

        source_hostname = urlparse(url).hostname if url else None
        for tracked_source in self.tracked_sources:
            tracked_uri = (tracked_source.get("uri") or "").strip()
            tracked_host = extract_hostname(tracked_uri)
            if not tracked_host:
                continue
            self._tracked_sources_by_host[tracked_host] = {
                "id": tracked_source.get("id"),
                "rules": list(tracked_source.get("rules", []) or []),
            }
        if (
            source_hostname
            and source_hostname.lower() not in self._tracked_sources_by_host
        ):
            self._tracked_sources_by_host[source_hostname.lower()] = {
                "id": self.source_id,
                "rules": self.source_rules,
            }

        def process_links(links: list[Link]) -> list[Link]:
            filtered_links: list[Link] = []
            seen_urls: set[str] = set()
            candidate_targets: list[tuple[Link, int, str]] = []
            for link in links:
                raw_url = (link.url or "").strip()
                target_source = self._resolve_tracked_source(raw_url)
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
                    target_source_id = int(
                        target_source.get("id") or self.source_id or 0
                    )
                    candidate_targets.append((link, target_source_id, normalized_url))

            allowed_targets = self._filter_links_by_crawl_eligibility(candidate_targets)
            for link, _, normalized_url in allowed_targets:
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
                    process_request="process_request",
                ),
            )
        )
        super().__init__(*args, **kwargs)

    def _resolve_tracked_source(self, url: str | None) -> dict | None:
        host = extract_hostname(url)
        if not host:
            return None
        return self._tracked_sources_by_host.get(host)

    def _request_source_context(self, url: str | None) -> tuple[int | None, list[dict]]:
        target_source = self._resolve_tracked_source(url)
        if target_source is None:
            return self.source_id, list(self.source_rules)
        return int(target_source.get("id") or self.source_id or 0), list(
            target_source.get("rules", []) or []
        )

    def parse_start_url(self, response, **kwargs):
        """Index start URLs as content (not just extract links from them)."""
        return self.parse_item(response)

    def start_requests(self):
        for url in self.start_urls:
            yield Request(
                url,
                dont_filter=True,
                meta={
                    **self._HTTP_STATUS_META,
                    "target_source_id": self.source_id,
                    "target_source_rules": list(self.source_rules),
                },
            )

        if self.single_page_only:
            return

        for seed_url in iter_priority_crawl_queue(
            self.source_id,
            exclude=self.start_urls,
        ):
            yield Request(
                seed_url,
                dont_filter=False,
                meta={
                    **self._HTTP_STATUS_META,
                    "target_source_id": self.source_id,
                    "target_source_rules": list(self.source_rules),
                },
            )

    def process_request(self, request, response):
        request.meta.update(self._HTTP_STATUS_META)
        source_id, source_rules = self._request_source_context(request.url)
        request.meta["target_source_id"] = source_id
        request.meta["target_source_rules"] = list(source_rules)
        return request

    def parse_item(self, response):
        print(f"Crawling {response.url}")
        item = CrawledItem()
        final_source = self._resolve_tracked_source(response.url)
        if final_source is not None:
            page_source_id = int(final_source.get("id") or self.source_id or 0)
            page_rules = list(final_source.get("rules", []) or [])
        else:
            page_source_id = int(
                response.request.meta.get("target_source_id") or self.source_id or 0
            )
            page_rules = list(
                response.request.meta.get("target_source_rules") or self.source_rules
            )

        item["url"] = normalize_url_for_queue(response.request.url, page_rules)
        item["final_url"] = normalize_url_for_queue(response.url, page_rules)
        item["referer_url"] = (
            response.request.headers.get("Referer", b"").decode("utf-8") or None
        )
        item["http_status"] = response.status
        item["etag"] = response.headers.get("ETag", b"").decode("utf-8") or None
        item["source_id"] = page_source_id
        item["content_type"] = response.headers.get("Content-Type", b"").decode("utf-8")
        response_text = response.text if isinstance(response, TextResponse) else None
        item["content"] = response_text
        item["raw_content"] = response.body
        extracted_links = []
        if self._link_extractor is not None and isinstance(response, HtmlResponse):
            extracted_links = [
                link.url
                for link in self._process_links(
                    self._link_extractor.extract_links(response)
                )
            ]
        item["out_links"] = extracted_links
        # Extract title only from HTML responses.
        item["title"] = (
            response.xpath("//title/text()").get()
            if isinstance(response, HtmlResponse)
            else None
        )

        # Detect language
        lang = None
        if response_text:
            try:
                is_reliable, _, details = cld2.detect(response_text)
                if is_reliable and details:
                    lang = details[0][1]
            except ValueError:
                lang = None

        # Detect date
        date = None

        # 1. Try Last-Modified header
        if "Last-Modified" in response.headers:
            try:
                date = date_parser.parse(
                    response.headers["Last-Modified"].decode("utf-8")
                ).isoformat()
            except (TypeError, ValueError, OverflowError):
                date = None

        # 2. Try meta tags if no date yet
        if not date and isinstance(response, HtmlResponse):
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
                except (TypeError, ValueError, OverflowError):
                    date = None

        # 3. Try Schema.org if no date yet
        if not date and isinstance(response, HtmlResponse):
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
                            if not isinstance(item, dict):
                                continue
                            date_val = item.get("datePublished") or item.get(
                                "dateModified"
                            )
                            if date_val:
                                date = date_parser.parse(date_val).isoformat()
                                break
                        if date:
                            break
                except (TypeError, ValueError, OverflowError, json.JSONDecodeError):
                    continue

        item["meta"] = {"lang": lang, "date": date}

        # We don't save content here; the pipeline handles document extraction.
        yield item

    def closed(self, reason):
        self._engine.dispose()

    def _filter_links_by_crawl_eligibility(
        self,
        candidates: list[tuple[Link, int, str]],
    ) -> list[tuple[Link, int, str]]:
        if not candidates:
            return []

        uncached_urls_by_source: dict[int, set[str]] = {}
        for _, target_source_id, normalized_url in candidates:
            cache_key = (target_source_id, normalized_url)
            if cache_key not in self._discovery_eligibility_cache:
                uncached_urls_by_source.setdefault(target_source_id, set()).add(
                    normalized_url
                )

        if uncached_urls_by_source:
            now = datetime.now(timezone.utc)
            with Session(bind=self._engine) as session:
                for target_source_id, urls in uncached_urls_by_source.items():
                    rows = session.execute(
                        select(
                            Page.uri,
                            Page.is_hub_page,
                            Page.last_crawled_at,
                            Page.check_interval_days,
                            Page.status_error,
                        ).where(
                            Page.source_id == target_source_id,
                            Page.uri.in_(list(urls)),
                        )
                    ).all()

                    found_urls: set[str] = set()
                    for (
                        uri,
                        is_hub_page,
                        last_crawled_at,
                        check_interval_days,
                        status_error,
                    ) in rows:
                        found_urls.add(uri)
                        is_due = last_crawled_at is None
                        if (
                            not is_due
                            and check_interval_days is not None
                            and check_interval_days > 0
                        ):
                            is_due = (
                                last_crawled_at
                                + timedelta(days=int(check_interval_days))
                                <= now
                            )
                        self._discovery_eligibility_cache[(target_source_id, uri)] = (
                            bool(is_hub_page)
                            or status_error == PageStatusError.http_5xx
                            or is_due
                        )

                    for missing_url in urls - found_urls:
                        self._discovery_eligibility_cache[
                            (target_source_id, missing_url)
                        ] = True

        return [
            candidate
            for candidate in candidates
            if self._discovery_eligibility_cache.get((candidate[1], candidate[2]), True)
        ]
