from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from vchat.source_settings import (
    DEFAULT_CRAWLER_CONCURRENT_REQUESTS,
    DEFAULT_CRAWLER_DOWNLOAD_DELAY,
    DEFAULT_CRAWLER_DOWNLOAD_TIMEOUT,
    DEFAULT_CRAWLER_USER_AGENT,
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
    crawler_user_agent: str = DEFAULT_CRAWLER_USER_AGENT
    crawler_concurrent_requests: int = DEFAULT_CRAWLER_CONCURRENT_REQUESTS
    crawler_download_delay: float = DEFAULT_CRAWLER_DOWNLOAD_DELAY
    crawler_download_timeout: float = DEFAULT_CRAWLER_DOWNLOAD_TIMEOUT
    rules: list[CrawlerRule] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict | None) -> SourceConfig:
        if not d:
            return cls()
        rules = [
            CrawlerRule.from_dict(r)
            for r in (d.get("rules") or [])
            if r.get("type") and r.get("value")
        ]
        return cls(
            crawler_user_agent=str(d.get("crawler_user_agent") or DEFAULT_CRAWLER_USER_AGENT),
            crawler_concurrent_requests=int(d.get("crawler_concurrent_requests") or DEFAULT_CRAWLER_CONCURRENT_REQUESTS),
            crawler_download_delay=float(d.get("crawler_download_delay") or DEFAULT_CRAWLER_DOWNLOAD_DELAY),
            crawler_download_timeout=float(d.get("crawler_download_timeout") or DEFAULT_CRAWLER_DOWNLOAD_TIMEOUT),
            rules=rules,
        )

    def to_dict(self) -> dict:
        d: dict = {
            "crawler_user_agent": self.crawler_user_agent,
            "crawler_concurrent_requests": self.crawler_concurrent_requests,
            "crawler_download_delay": self.crawler_download_delay,
            "crawler_download_timeout": self.crawler_download_timeout,
        }
        if self.rules:
            d["rules"] = [r.to_dict() for r in self.rules]
        return d
