from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from vchat.models.source_config import CrawlerRule, SourceConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_source(
    source_id: int = 1,
    uri: str = "https://example.com",
    title: str = "Example",
    start_pages: list[str] | None = None,
    rules: list[CrawlerRule] | None = None,
):
    """Build a minimal Source-like object that tasks.py reads from the DB."""
    source = MagicMock()
    source.id = source_id
    source.uri = uri
    source.title = title
    source.start_pages = start_pages or []
    source.config = SourceConfig(rules=rules or [])
    return source


# ---------------------------------------------------------------------------
# Source model: start_pages is a real attribute; sitemaps is now in Sitemap table
# ---------------------------------------------------------------------------

class TestSourceAttributes:
    def test_source_has_start_pages_attribute(self):
        from vchat.models.data import Source
        assert hasattr(Source, "start_pages"), (
            "Source model is missing 'start_pages' column — "
            "add it to data.py"
        )

    def test_source_has_sitemap_model(self):
        from vchat.models.data import Sitemap
        assert hasattr(Sitemap, "url"), (
            "Sitemap model is missing 'url' column"
        )
        assert hasattr(Sitemap, "source_id"), (
            "Sitemap model is missing 'source_id' column"
        )

    def test_source_has_config_property(self):
        from vchat.models.data import Source
        assert isinstance(Source.config, property), (
            "Source.config must be a property returning SourceConfig"
        )


# ---------------------------------------------------------------------------
# crawl_source_task: payload building
# ---------------------------------------------------------------------------

class TestCrawlSourceTaskPayload:
    """Test that crawl_source_task builds the subprocess command correctly."""

    def run_task_capture_cmd(self, source):
        """Run crawl_source_task with a fake source and capture subprocess cmd."""
        captured = {}

        engine_mock = MagicMock()
        session_mock = MagicMock()
        session_mock.get.return_value = source
        session_mock.execute.return_value = MagicMock()
        session_mock.__enter__ = lambda s: session_mock
        session_mock.__exit__ = MagicMock(return_value=False)
        engine_mock.__enter__ = lambda s: engine_mock

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch("jobs.crawler.tasks.create_sync_engine", return_value=engine_mock),
            patch("jobs.crawler.tasks.Session", return_value=session_mock),
            patch("jobs.crawler.tasks.subprocess.run", side_effect=fake_run),
            patch("jobs.embedder.tasks.refresh_project_index"),
        ):
            from jobs.crawler import tasks as crawler_tasks
            crawler_tasks.crawl_source_task(source.id)

        return captured.get("cmd", [])

    def test_cmd_contains_url_and_source_id(self):
        source = make_source(source_id=42, uri="https://test.com")
        cmd = self.run_task_capture_cmd(source)
        assert "https://test.com" in cmd
        assert "42" in cmd

    def test_cmd_has_no_page_limit_arg(self):
        """Ensure no extra positional args beyond url/source_id/config_json."""
        source = make_source()
        cmd = self.run_task_capture_cmd(source)
        non_flag_args = [a for a in cmd if not a.startswith("-")]
        module_idx = non_flag_args.index("jobs.crawler.crawler_runner")
        positional = non_flag_args[module_idx + 1:]
        assert len(positional) == 3, (
            f"Expected [url, source_id, config_json], got {positional}"
        )

    def test_config_json_contains_start_pages(self):
        source = make_source(start_pages=["https://example.com/a", "https://example.com/b"])
        cmd = self.run_task_capture_cmd(source)
        config_json = cmd[-1]
        payload = json.loads(config_json)
        assert payload["start_pages"] == ["https://example.com/a", "https://example.com/b"]

    def test_config_json_contains_crawler_settings(self):
        source = make_source()
        source.config = SourceConfig(crawler_concurrent_requests=4)
        cmd = self.run_task_capture_cmd(source)
        payload = json.loads(cmd[-1])
        assert payload["crawler_concurrent_requests"] == 4

    def test_config_json_contains_rules(self):
        source = make_source(rules=[CrawlerRule(type="xpath", value="//a")])
        cmd = self.run_task_capture_cmd(source)
        payload = json.loads(cmd[-1])
        assert payload["rules"] == [{"type": "xpath", "value": "//a"}]

    def test_start_pages_not_in_source_config_dict(self):
        """start_pages come from Source columns, NOT from SourceConfig.to_dict()."""
        cfg = SourceConfig()
        d = cfg.to_dict()
        assert "start_pages" not in d

    def test_empty_start_pages(self):
        source = make_source(start_pages=[])
        cmd = self.run_task_capture_cmd(source)
        payload = json.loads(cmd[-1])
        assert payload["start_pages"] == []
