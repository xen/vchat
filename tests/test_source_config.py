from __future__ import annotations

import json
import pytest

from vchat.models.source_config import CrawlerRule, SourceConfig
from vchat.source_settings import (
    DEFAULT_CRAWLER_CONCURRENT_REQUESTS,
    DEFAULT_CRAWLER_DOWNLOAD_DELAY,
    DEFAULT_CRAWLER_DOWNLOAD_TIMEOUT,
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
        assert cfg.crawler_concurrent_requests == DEFAULT_CRAWLER_CONCURRENT_REQUESTS
        assert cfg.crawler_download_delay == DEFAULT_CRAWLER_DOWNLOAD_DELAY
        assert cfg.crawler_download_timeout == DEFAULT_CRAWLER_DOWNLOAD_TIMEOUT
        assert cfg.rules == []
        assert cfg.trigger_rules == []

    def test_empty_dict(self):
        assert SourceConfig.from_dict({}) == SourceConfig()

    def test_reads_known_fields(self):
        cfg = SourceConfig.from_dict(
            {
                "crawler_concurrent_requests": 4,
                "crawler_download_delay": 1,
                "crawler_download_timeout": 60,
                "ignore_robots_txt": True,
                "allow_custom_triggers": True,
            }
        )
        assert cfg.crawler_concurrent_requests == 4
        assert cfg.crawler_download_delay == 1
        assert cfg.crawler_download_timeout == 60
        assert cfg.ignore_robots_txt is True
        assert cfg.allow_custom_triggers is True

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

    def test_parses_trigger_rules(self):
        cfg = SourceConfig.from_dict(
            {
                "trigger_rules": [
                    {"type": "regex", "value": "^https://example.com/docs/"}
                ]
            }
        )
        assert cfg.trigger_rules == [
            CrawlerRule(type="regex", value="^https://example.com/docs/")
        ]

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
        assert cfg.rules[0].type == "css"

    def test_skips_rules_with_missing_type(self):
        cfg = SourceConfig.from_dict(
            {"rules": [{"value": "//a"}, {"type": "css", "value": "a"}]}
        )
        assert len(cfg.rules) == 1

    def test_ignores_unknown_keys(self):
        cfg = SourceConfig.from_dict(
            {
                "aws_access_key_id": "should-be-gone",
                "folder_id": "should-be-gone",
                "sitemaps": ["https://example.com/sitemap.xml"],
            }
        )
        assert not hasattr(cfg, "aws_access_key_id")
        assert not hasattr(cfg, "sitemaps")

    def test_falls_back_to_defaults_on_null_values(self):
        cfg = SourceConfig.from_dict(
            {
                "crawler_concurrent_requests": None,
            }
        )
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
        assert "trigger_rules" not in d

    def test_rules_key_present_when_nonempty(self):
        cfg = SourceConfig(rules=[CrawlerRule(type="css", value="a")])
        d = cfg.to_dict()
        assert d["rules"] == [{"type": "css", "value": "a"}]

    def test_trigger_rules_key_present_when_nonempty(self):
        cfg = SourceConfig(trigger_rules=[CrawlerRule(type="regex", value="^https://")])
        d = cfg.to_dict()
        assert d["trigger_rules"] == [{"type": "regex", "value": "^https://"}]

    def test_no_sitemaps(self):
        d = SourceConfig().to_dict()
        assert "sitemaps" not in d

    def test_no_user_agent_in_dict(self):
        d = SourceConfig().to_dict()
        assert "crawler_user_agent" not in d

    def test_known_keys_only(self):
        d = SourceConfig().to_dict()
        assert set(d.keys()) == {
            "crawler_concurrent_requests",
            "crawler_download_delay",
            "crawler_download_timeout",
            "ignore_robots_txt",
            "allow_custom_triggers",
        }

    def test_round_trip(self):
        original = SourceConfig(
            crawler_concurrent_requests=8,
            crawler_download_delay=1,
            crawler_download_timeout=15,
            ignore_robots_txt=True,
            allow_custom_triggers=True,
            rules=[CrawlerRule(type="regex", value="^https://")],
            trigger_rules=[CrawlerRule(type="regex", value="/product/")],
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


class SourceStub:
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
        s = SourceStub()
        assert isinstance(s.config, SourceConfig)

    def test_setter_stores_dict(self):
        s = SourceStub()
        s.config = SourceConfig(crawler_concurrent_requests=4)
        assert isinstance(s._config, dict)
        assert s._config["crawler_concurrent_requests"] == 4

    def test_getter_reads_stored_dict(self):
        s = SourceStub({"crawler_concurrent_requests": 2})
        assert s.config.crawler_concurrent_requests == 2

    def test_old_s3_keys_ignored(self):
        s = SourceStub(
            {
                "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
                "bucket_name": "my-bucket",
            }
        )
        cfg = s.config
        assert not hasattr(cfg, "aws_access_key_id")

    def test_old_gdrive_keys_ignored(self):
        s = SourceStub({"folder_id": "abc123", "folder_name": "Docs"})
        assert s.config == SourceConfig()
