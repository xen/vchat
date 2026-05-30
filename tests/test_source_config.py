from __future__ import annotations

import json
import pytest

from vchat.models.source_config import CrawlerRule, SourceConfig
from vchat.source_settings import (
    DEFAULT_CRAWLER_CONCURRENT_REQUESTS,
    DEFAULT_CRAWLER_DOWNLOAD_DELAY,
    DEFAULT_CRAWLER_DOWNLOAD_TIMEOUT,
    DEFAULT_CRAWLER_USER_AGENT,
)


# ---------------------------------------------------------------------------
# CrawlerRule
# ---------------------------------------------------------------------------


class TestCrawlerRule:
    def test_round_trip(self):
        rule = CrawlerRule(type="xpath", value="//a[@class='nav']")
        assert CrawlerRule.from_dict(rule.to_dict()) == rule

    @pytest.mark.parametrize("rule_type", ["xpath", "css", "param", "regex"])
    def test_all_valid_types(self, rule_type):
        rule = CrawlerRule(type=rule_type, value="something")
        assert rule.to_dict()["type"] == rule_type

    def test_to_dict_keys(self):
        d = CrawlerRule(type="css", value="a.link").to_dict()
        assert set(d.keys()) == {"type", "value"}


# ---------------------------------------------------------------------------
# SourceConfig.from_dict
# ---------------------------------------------------------------------------


class TestSourceConfigFromDict:
    def test_empty_none(self):
        cfg = SourceConfig.from_dict(None)
        assert cfg.crawler_user_agent == DEFAULT_CRAWLER_USER_AGENT
        assert cfg.crawler_concurrent_requests == DEFAULT_CRAWLER_CONCURRENT_REQUESTS
        assert cfg.crawler_download_delay == DEFAULT_CRAWLER_DOWNLOAD_DELAY
        assert cfg.crawler_download_timeout == DEFAULT_CRAWLER_DOWNLOAD_TIMEOUT
        assert cfg.rules == []

    def test_empty_dict(self):
        assert SourceConfig.from_dict({}) == SourceConfig()

    def test_reads_known_fields(self):
        cfg = SourceConfig.from_dict(
            {
                "crawler_user_agent": "Bot/1.0",
                "crawler_concurrent_requests": 4,
                "crawler_download_delay": 1,
                "crawler_download_timeout": 60,
            }
        )
        assert cfg.crawler_user_agent == "Bot/1.0"
        assert cfg.crawler_concurrent_requests == 4
        assert cfg.crawler_download_delay == 1
        assert cfg.crawler_download_timeout == 60

    def test_parses_rules(self):
        cfg = SourceConfig.from_dict(
            {
                "rules": [
                    {"type": "xpath", "value": "//a"},
                    {"type": "css", "value": "a.nav"},
                ]
            }
        )
        assert len(cfg.rules) == 2
        assert cfg.rules[0] == CrawlerRule(type="xpath", value="//a")
        assert cfg.rules[1] == CrawlerRule(type="css", value="a.nav")

    def test_skips_rules_with_empty_value(self):
        cfg = SourceConfig.from_dict(
            {
                "rules": [
                    {"type": "xpath", "value": ""},
                    {"type": "css", "value": "a.nav"},
                ]
            }
        )
        assert len(cfg.rules) == 1
        assert cfg.rules[0] == CrawlerRule(type="css", value="a.nav")

    def test_skips_rules_with_missing_type(self):
        cfg = SourceConfig.from_dict(
            {"rules": [{"value": "//a"}, {"type": "css", "value": "a"}]}
        )
        assert len(cfg.rules) == 1
        assert cfg.rules[0] == CrawlerRule(type="css", value="a")

    def test_ignores_unknown_keys(self):
        cfg = SourceConfig.from_dict(
            {
                "crawler_user_agent": "Bot",
                "aws_access_key_id": "should-be-gone",
                "folder_id": "should-be-gone",
                "start_pages": ["https://example.com"],
                "sitemaps": ["https://example.com/sitemap.xml"],
            }
        )
        assert cfg.crawler_user_agent == "Bot"
        assert not hasattr(cfg, "aws_access_key_id")
        assert not hasattr(cfg, "start_pages")
        assert not hasattr(cfg, "sitemaps")

    def test_falls_back_to_defaults_on_null_values(self):
        cfg = SourceConfig.from_dict(
            {
                "crawler_user_agent": None,
                "crawler_concurrent_requests": None,
            }
        )
        assert cfg.crawler_user_agent == DEFAULT_CRAWLER_USER_AGENT
        assert cfg.crawler_concurrent_requests == DEFAULT_CRAWLER_CONCURRENT_REQUESTS

    def test_preserves_zero_delay(self):
        cfg = SourceConfig.from_dict({"crawler_download_delay": 0})
        assert cfg.crawler_download_delay == 0


# ---------------------------------------------------------------------------
# SourceConfig.to_dict
# ---------------------------------------------------------------------------


class TestSourceConfigToDict:
    def test_no_rules_key_when_empty(self):
        d = SourceConfig().to_dict()
        assert "rules" not in d

    def test_rules_key_present_when_nonempty(self):
        cfg = SourceConfig(rules=[CrawlerRule(type="css", value="a")])
        d = cfg.to_dict()
        assert d["rules"] == [{"type": "css", "value": "a"}]

    def test_no_start_pages_or_sitemaps(self):
        d = SourceConfig().to_dict()
        assert "start_pages" not in d
        assert "sitemaps" not in d

    def test_known_keys_only(self):
        d = SourceConfig().to_dict()
        assert set(d.keys()) == {
            "crawler_user_agent",
            "crawler_concurrent_requests",
            "crawler_download_delay",
            "crawler_download_timeout",
        }

    def test_round_trip(self):
        original = SourceConfig(
            crawler_user_agent="Agent/2",
            crawler_concurrent_requests=8,
            crawler_download_delay=1,
            crawler_download_timeout=15,
            rules=[CrawlerRule(type="regex", value="^https://")],
        )
        assert SourceConfig.from_dict(original.to_dict()) == original

    def test_json_serialisable(self):
        d = SourceConfig(
            rules=[CrawlerRule(type="param", value="utm_source")]
        ).to_dict()
        json.dumps(d)  # must not raise


# ---------------------------------------------------------------------------
# Source model property
# ---------------------------------------------------------------------------


class _SourceStub:
    """Minimal stub that replicates Source.config property without SQLAlchemy."""

    def __init__(self, raw_config: dict | None = None):
        self._config: dict = raw_config if raw_config is not None else {}

    @property
    def config(self) -> SourceConfig:
        return SourceConfig.from_dict(self._config)

    @config.setter
    def config(self, value: SourceConfig) -> None:
        self._config = value.to_dict()


class TestSourceModelConfig:
    def test_getter_returns_source_config(self):
        s = _SourceStub()
        assert isinstance(s.config, SourceConfig)

    def test_setter_stores_dict(self):
        s = _SourceStub()
        s.config = SourceConfig(crawler_user_agent="TestBot")
        assert isinstance(s._config, dict)
        assert s._config["crawler_user_agent"] == "TestBot"

    def test_getter_reads_stored_dict(self):
        s = _SourceStub(
            {"crawler_user_agent": "StoredBot", "crawler_concurrent_requests": 2}
        )
        assert s.config.crawler_user_agent == "StoredBot"
        assert s.config.crawler_concurrent_requests == 2

    def test_old_s3_keys_ignored(self):
        s = _SourceStub(
            {
                "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
                "bucket_name": "my-bucket",
                "crawler_user_agent": "Bot",
            }
        )
        cfg = s.config
        assert cfg.crawler_user_agent == "Bot"
        assert not hasattr(cfg, "aws_access_key_id")

    def test_old_gdrive_keys_ignored(self):
        s = _SourceStub({"folder_id": "abc123", "folder_name": "Docs"})
        assert s.config == SourceConfig()


# ---------------------------------------------------------------------------
# Crawler tasks payload
# ---------------------------------------------------------------------------


class TestCrawlerTasksPayload:
    def test_payload_contains_start_pages_and_sitemaps(self):
        cfg = SourceConfig(crawler_user_agent="Bot")
        payload = cfg.to_dict()
        payload["start_pages"] = ["https://a.com/1"]
        payload["sitemaps"] = ["https://a.com/sitemap.xml"]

        assert payload["start_pages"] == ["https://a.com/1"]
        assert payload["sitemaps"] == ["https://a.com/sitemap.xml"]
        assert payload["crawler_user_agent"] == "Bot"

    def test_config_to_dict_unchanged_after_payload_mutation(self):
        cfg = SourceConfig()
        d = cfg.to_dict()
        d["start_pages"] = ["https://x.com"]
        assert "start_pages" not in cfg.to_dict()
