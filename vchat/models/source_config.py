from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from vchat.source_settings import (
    DEFAULT_CRAWLER_CONCURRENT_REQUESTS,
    DEFAULT_CRAWLER_DOWNLOAD_DELAY,
    DEFAULT_CRAWLER_DOWNLOAD_TIMEOUT,
)

RuleType = Literal["xpath", "css", "param", "regex"]


@dataclass
class CrawlerRule:
    type: RuleType
    value: str

    @classmethod
    def from_dict(cls, d: dict) -> CrawlerRule:
        return cls(type=d["type"], value=d["value"])

    def to_dict(self) -> dict:
        return {"type": self.type, "value": self.value}


@dataclass
class SourceConfig:
    crawler_concurrent_requests: int = DEFAULT_CRAWLER_CONCURRENT_REQUESTS
    crawler_download_delay: int = DEFAULT_CRAWLER_DOWNLOAD_DELAY
    crawler_download_timeout: int = DEFAULT_CRAWLER_DOWNLOAD_TIMEOUT
    ignore_robots_txt: bool = False
    rules: list[CrawlerRule] = field(default_factory=list)
    trigger_rules: list[CrawlerRule] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict | None) -> SourceConfig:
        if not d:
            return cls()
        concurrent_requests_raw = d.get("crawler_concurrent_requests")
        download_delay_raw = d.get("crawler_download_delay")
        download_timeout_raw = d.get("crawler_download_timeout")
        rules = [
            CrawlerRule.from_dict(r)
            for r in (d.get("rules") or [])
            if r.get("type") and r.get("value")
        ]
        trigger_rules = [
            CrawlerRule.from_dict(r)
            for r in (d.get("trigger_rules") or [])
            if r.get("type") and r.get("value")
        ]
        return cls(
            crawler_concurrent_requests=int(
                DEFAULT_CRAWLER_CONCURRENT_REQUESTS
                if concurrent_requests_raw is None
                else concurrent_requests_raw
            ),
            crawler_download_delay=int(
                DEFAULT_CRAWLER_DOWNLOAD_DELAY
                if download_delay_raw is None
                else download_delay_raw
            ),
            crawler_download_timeout=int(
                DEFAULT_CRAWLER_DOWNLOAD_TIMEOUT
                if download_timeout_raw is None
                else download_timeout_raw
            ),
            ignore_robots_txt=bool(d.get("ignore_robots_txt", False)),
            rules=rules,
            trigger_rules=trigger_rules,
        )

    def to_dict(self) -> dict:
        d: dict = {
            "crawler_concurrent_requests": self.crawler_concurrent_requests,
            "crawler_download_delay": self.crawler_download_delay,
            "crawler_download_timeout": self.crawler_download_timeout,
            "ignore_robots_txt": self.ignore_robots_txt,
        }
        if self.rules:
            d["rules"] = [r.to_dict() for r in self.rules]
        if self.trigger_rules:
            d["trigger_rules"] = [r.to_dict() for r in self.trigger_rules]
        return d
