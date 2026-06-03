from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from vchat.models.source_config import CrawlerRule, SourceConfig
from vchat.page_status import PageStatusError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_source(
    source_id: int = 1,
    uri: str = "https://example.com",
    title: str = "Example",
    rules: list[CrawlerRule] | None = None,
):
    """Build a minimal Source-like object that tasks.py reads from the DB."""
    source = MagicMock()
    source.id = source_id
    source.uri = uri
    source.title = title
    source.config = SourceConfig(rules=rules or [])
    source.is_paused = False
    source.blocked_reason = None
    source.blocked_message = None
    return source


# ---------------------------------------------------------------------------
# Source model: sitemaps are now in Sitemap table
# ---------------------------------------------------------------------------


class TestSourceAttributes:
    def test_source_has_sitemap_model(self):
        from vchat.models.data import Sitemap

        assert hasattr(Sitemap, "url"), "Sitemap model is missing 'url' column"
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
        session_mock.__enter__ = lambda s: session_mock
        session_mock.__exit__ = MagicMock(return_value=False)
        engine_mock.__enter__ = lambda s: engine_mock

        def fake_execute(stmt):
            stmt_text = str(stmt)
            if "FROM source" in stmt_text and "source.id" in stmt_text:
                return MagicMock(scalar_one_or_none=lambda: source)
            if "crawl_run" in stmt_text:
                return MagicMock(scalars=lambda: MagicMock(all=lambda: []))
            if "FROM source" in stmt_text:
                return MagicMock(scalars=lambda: MagicMock(all=lambda: []))
            return MagicMock()

        session_mock.execute.side_effect = fake_execute

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch("jobs.crawler.tasks.create_sync_engine", return_value=engine_mock),
            patch("jobs.crawler.tasks.Session", return_value=session_mock),
            patch(
                "jobs.crawler.tasks.check_source_blocking",
                return_value=SimpleNamespace(
                    is_blocked=False,
                    reason=None,
                    message=None,
                    checked_at=None,
                ),
            ),
            patch("jobs.crawler.tasks.subprocess.run", side_effect=fake_run),
            patch("jobs.crawler.tasks._reserve_source_crawl_run", return_value=321),
            patch("jobs.crawler.tasks._refresh_source_discovery"),
            patch("jobs.crawler.tasks._sync_sitemaps_for_source"),
            patch("jobs.crawler.tasks.refresh_project_index"),
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
        positional = non_flag_args[module_idx + 1 :]
        assert len(positional) == 3, (
            f"Expected [url, source_id, config_json], got {positional}"
        )

    def test_config_json_contains_crawler_settings(self):
        source = make_source()
        source.config = SourceConfig(
            crawler_concurrent_requests=4,
            ignore_robots_txt=True,
        )
        cmd = self.run_task_capture_cmd(source)
        payload = json.loads(cmd[-1])
        assert payload["crawler_concurrent_requests"] == 4
        assert payload["ignore_robots_txt"] is True

    def test_config_json_contains_rules(self):
        source = make_source(rules=[CrawlerRule(type="xpath", value="//a")])
        cmd = self.run_task_capture_cmd(source)
        payload = json.loads(cmd[-1])
        assert payload["rules"] == [{"type": "xpath", "value": "//a"}]

    def test_config_json_contains_tracked_sources(self):
        source = make_source(source_id=1, uri="https://example.com")
        other_source = make_source(source_id=2, uri="https://grant.vbudushee.ru")
        captured = {}

        engine_mock = MagicMock()
        session_mock = MagicMock()
        session_mock.__enter__ = lambda s: session_mock
        session_mock.__exit__ = MagicMock(return_value=False)

        def fake_execute(stmt):
            stmt_text = str(stmt)
            if "FROM source" in stmt_text and "WHERE source.id" in stmt_text:
                return MagicMock(scalar_one_or_none=lambda: source)
            if "crawl_run" in stmt_text:
                return MagicMock(scalars=lambda: MagicMock(all=lambda: []))
            return MagicMock(scalars=lambda: MagicMock(all=lambda: [source, other_source]))

        session_mock.execute.side_effect = fake_execute

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch("jobs.crawler.tasks.create_sync_engine", return_value=engine_mock),
            patch("jobs.crawler.tasks.Session", return_value=session_mock),
            patch(
                "jobs.crawler.tasks.check_source_blocking",
                return_value=SimpleNamespace(
                    is_blocked=False,
                    reason=None,
                    message=None,
                    checked_at=None,
                ),
            ),
            patch("jobs.crawler.tasks.subprocess.run", side_effect=fake_run),
            patch("jobs.crawler.tasks._reserve_source_crawl_run", return_value=654),
            patch("jobs.crawler.tasks._sync_sitemaps_for_source"),
            patch("jobs.crawler.tasks.refresh_project_index"),
        ):
            from jobs.crawler import tasks as crawler_tasks

            crawler_tasks.crawl_source_task(source.id)

        payload = json.loads(captured["cmd"][-1])
        assert payload["crawl_run_id"] == 654
        assert {item["uri"] for item in payload["tracked_sources"]} == {
            "https://example.com",
            "https://grant.vbudushee.ru",
        }

    def test_commits_discovery_updates_before_run_finishes(self):
        source = make_source(source_id=42, uri="https://test.com")
        engine_mock = MagicMock()
        session_mock = MagicMock()
        session_mock.__enter__ = lambda s: session_mock
        session_mock.__exit__ = MagicMock(return_value=False)

        def fake_execute(stmt):
            stmt_text = str(stmt)
            if "FROM source" in stmt_text and "source.id" in stmt_text:
                return MagicMock(scalar_one_or_none=lambda: source)
            if "crawl_run" in stmt_text:
                return MagicMock(scalars=lambda: MagicMock(all=lambda: []))
            if "FROM source" in stmt_text:
                return MagicMock(scalars=lambda: MagicMock(all=lambda: []))
            return MagicMock()

        session_mock.execute.side_effect = fake_execute

        with (
            patch("jobs.crawler.tasks.create_sync_engine", return_value=engine_mock),
            patch("jobs.crawler.tasks.Session", return_value=session_mock),
            patch(
                "jobs.crawler.tasks.check_source_blocking",
                return_value=SimpleNamespace(
                    is_blocked=False,
                    reason=None,
                    message=None,
                    checked_at=None,
                ),
            ),
            patch("jobs.crawler.tasks.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")),
            patch("jobs.crawler.tasks._reserve_source_crawl_run", return_value=321),
            patch("jobs.crawler.tasks._refresh_source_discovery"),
            patch("jobs.crawler.tasks._sync_sitemaps_for_source"),
            patch("jobs.crawler.tasks.refresh_project_index"),
        ):
            from jobs.crawler import tasks as crawler_tasks

            crawler_tasks.crawl_source_task(source.id)

        assert session_mock.commit.call_count >= 2


class TestCrawlPageTaskPayload:
    def test_page_task_uses_single_page_mode(self):
        page = SimpleNamespace(id=7, source_id=42, uri="https://test.com/page")
        source = make_source(
            source_id=42,
            uri="https://test.com",
        )
        captured = {}

        engine_mock = MagicMock()
        session_mock = MagicMock()
        session_mock.__enter__ = lambda s: session_mock
        session_mock.__exit__ = MagicMock(return_value=False)
        source_fetches = {"count": 0}

        def fake_execute(stmt):
            stmt_text = str(stmt)
            if "JOIN source ON source.id = page.source_id" in stmt_text:
                return MagicMock(one_or_none=lambda: (page, source))
            if "crawl_run" in stmt_text:
                return MagicMock(scalars=lambda: MagicMock(all=lambda: []))
            if "FROM source" in stmt_text:
                source_fetches["count"] += 1
                return MagicMock(scalars=lambda: MagicMock(all=lambda: []))
            return MagicMock(one_or_none=lambda: (page, source))

        session_mock.execute.side_effect = fake_execute

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch("jobs.crawler.tasks.create_sync_engine", return_value=engine_mock),
            patch("jobs.crawler.tasks.Session", return_value=session_mock),
            patch("jobs.crawler.tasks.subprocess.run", side_effect=fake_run),
            patch("jobs.crawler.tasks.schedule_refresh_project_index"),
            patch("jobs.crawler.tasks.rebuild_boilerplate_index.delay"),
        ):
            from jobs.crawler import tasks as crawler_tasks

            crawler_tasks.crawl_page_task(page.id)

        payload = json.loads(captured["cmd"][-1])
        assert captured["cmd"][-3:-1] == ["https://test.com/page", "42"]
        assert payload["single_page_only"] is True
        assert "crawler_max_pages" not in payload

    def test_page_task_rejects_page_with_unavailable_source(self):
        engine_mock = MagicMock()
        session_mock = MagicMock()
        session_mock.__enter__ = lambda s: session_mock
        session_mock.__exit__ = MagicMock(return_value=False)
        session_mock.execute.return_value = MagicMock(one_or_none=lambda: None)

        with (
            patch("jobs.crawler.tasks.create_sync_engine", return_value=engine_mock),
            patch("jobs.crawler.tasks.Session", return_value=session_mock),
        ):
            from jobs.crawler import tasks as crawler_tasks

            with pytest.raises(RuntimeError, match="not refreshable"):
                crawler_tasks.crawl_page_task(7)


class TestCrawlSourceTaskFiltering:
    def test_skips_non_crawlable_source_before_reserve_or_crawl(self):
        engine_mock = MagicMock()
        session_mock = MagicMock()
        session_mock.__enter__ = lambda s: session_mock
        session_mock.__exit__ = MagicMock(return_value=False)
        session_mock.execute.return_value = MagicMock(scalar_one_or_none=lambda: None)

        with (
            patch("jobs.crawler.tasks.create_sync_engine", return_value=engine_mock),
            patch("jobs.crawler.tasks.Session", return_value=session_mock),
            patch("jobs.crawler.tasks._reserve_source_crawl_run") as reserve_mock,
            patch("jobs.crawler.tasks.subprocess.run") as run_mock,
        ):
            from jobs.crawler import tasks as crawler_tasks

            crawler_tasks.crawl_source_task(42)

        reserve_mock.assert_not_called()
        run_mock.assert_not_called()

    def test_blocked_source_marks_clean_crawler_pages_ready(self):
        source = make_source(source_id=42, uri="https://example.com")
        engine_mock = MagicMock()
        session_mock = MagicMock()
        session_mock.__enter__ = lambda s: session_mock
        session_mock.__exit__ = MagicMock(return_value=False)

        execute_calls = []

        def fake_execute(stmt):
            execute_calls.append(stmt)
            stmt_text = str(stmt)
            if "FROM source" in stmt_text and "WHERE source.id" in stmt_text:
                return MagicMock(scalar_one_or_none=lambda: source)
            return MagicMock()

        session_mock.execute.side_effect = fake_execute

        with (
            patch("jobs.crawler.tasks.create_sync_engine", return_value=engine_mock),
            patch("jobs.crawler.tasks.Session", return_value=session_mock),
            patch(
                "jobs.crawler.tasks.check_source_blocking",
                return_value=SimpleNamespace(
                    is_blocked=True,
                    reason=SimpleNamespace(value="dns_unresolved"),
                    message="dns failed",
                    checked_at=datetime.now(timezone.utc),
                ),
            ),
            patch("jobs.crawler.tasks.subprocess.run") as run_mock,
        ):
            from jobs.crawler import tasks as crawler_tasks

            crawler_tasks.crawl_source_task(42)

        run_mock.assert_not_called()
        update_stmt = execute_calls[1]
        params = update_stmt.compile().params
        assert params["status"] == "ready"
        assert params["status_error"] == "dns_unresolved"


class TestRefreshSourceBlockingState:
    def test_marks_clean_crawler_pages_for_any_block_reason(self):
        source = make_source(source_id=42, uri="https://example.com")
        engine_mock = MagicMock()
        session_mock = MagicMock()
        session_mock.__enter__ = lambda s: session_mock
        session_mock.__exit__ = MagicMock(return_value=False)
        session_mock.get.return_value = source

        execute_calls = []

        def fake_execute(stmt):
            execute_calls.append(stmt)
            return MagicMock()

        session_mock.execute.side_effect = fake_execute

        with (
            patch("jobs.crawler.tasks.create_sync_engine", return_value=engine_mock),
            patch("jobs.crawler.tasks.Session", return_value=session_mock),
            patch(
                "jobs.crawler.tasks.check_source_blocking",
                return_value=SimpleNamespace(
                    is_blocked=True,
                    reason=SimpleNamespace(value="redirect_other_domain"),
                    message="redirected away",
                    checked_at=datetime.now(timezone.utc),
                ),
            ),
        ):
            from jobs.crawler import tasks as crawler_tasks

            blocked = crawler_tasks.refresh_source_blocking_state(42)

        assert blocked is True
        update_stmt = execute_calls[0]
        params = update_stmt.compile().params
        assert params["status"] == "ready"
        assert params["status_error"] == "redirect_other_domain"


class TestRefreshSourceDiscovery:
    def test_marks_path_level_robots_blocked_pages_ready(self):
        source = make_source(source_id=42, uri="https://example.com")
        source.robots_cache = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "sitemaps": [],
            "crawl_delay": None,
            "body": "User-agent: *\nDisallow: /private/\n",
        }
        session_mock = MagicMock()

        execute_calls = []

        def fake_execute(stmt):
            execute_calls.append(stmt)
            stmt_text = str(stmt)
            if "count(sitemap.id)" in stmt_text:
                return MagicMock(scalar_one=lambda: 1)
            if "SELECT page.id, page.uri" in stmt_text:
                return MagicMock(
                    all=lambda: [
                        (1, "https://example.com/private/doc"),
                        (2, "https://example.com/public/doc"),
                    ]
                )
            return MagicMock()

        session_mock.execute.side_effect = fake_execute
        parser_mock = MagicMock()
        parser_mock.can_fetch.side_effect = lambda _ua, url: "/private/" not in url

        with (
            patch("jobs.crawler.tasks._probe_common_sitemaps", return_value=[]),
            patch("jobs.crawler.tasks.RobotFileParser", return_value=parser_mock),
        ):
            from jobs.crawler import tasks as crawler_tasks

            crawler_tasks._refresh_source_discovery(session_mock, source, {})

        update_stmt = execute_calls[-1]
        params = update_stmt.compile().params
        assert params["status"] == "ready"
        assert params["status_error"] == PageStatusError.excluded_robots


class TestReapplySourceRulesTask:
    def test_marks_pages_with_newly_ignored_params_as_excluded(self):
        source = make_source(
            source_id=42,
            rules=[CrawlerRule(type="param", value="tag")],
        )
        page_tag = SimpleNamespace(
            source_id=42,
            uri="https://example.com/library/?tag=science",
            status="crawler",
            status_error=None,
        )
        page_plain = SimpleNamespace(
            source_id=42,
            uri="https://example.com/library/",
            status="crawler",
            status_error=None,
        )
        committed = {"count": 0}

        engine_mock = MagicMock()
        session_mock = MagicMock()
        session_mock.get.return_value = source
        session_mock.execute.return_value.scalars.return_value.all.return_value = [
            page_tag,
            page_plain,
        ]
        session_mock.commit.side_effect = lambda: committed.__setitem__(
            "count", committed["count"] + 1
        )
        session_mock.__enter__ = lambda s: session_mock
        session_mock.__exit__ = MagicMock(return_value=False)

        with (
            patch("jobs.crawler.tasks.create_sync_engine", return_value=engine_mock),
            patch("jobs.crawler.tasks.Session", return_value=session_mock),
        ):
            from jobs.crawler import tasks as crawler_tasks

            updated = crawler_tasks.reapply_source_rules_task(source.id)

        assert updated == 1
        assert page_tag.status_error == PageStatusError.excluded_rules
        assert page_plain.status_error is None
        assert committed["count"] == 1

    def test_restores_pages_when_rule_is_removed(self):
        source = make_source(source_id=42, rules=[])
        page = SimpleNamespace(
            source_id=42,
            uri="https://example.com/library/?tag=science",
            status="crawler",
            status_error=PageStatusError.excluded_rules,
        )
        committed = {"count": 0}

        engine_mock = MagicMock()
        session_mock = MagicMock()
        session_mock.get.return_value = source
        session_mock.execute.return_value.scalars.return_value.all.return_value = [page]
        session_mock.commit.side_effect = lambda: committed.__setitem__(
            "count", committed["count"] + 1
        )
        session_mock.__enter__ = lambda s: session_mock
        session_mock.__exit__ = MagicMock(return_value=False)

        with (
            patch("jobs.crawler.tasks.create_sync_engine", return_value=engine_mock),
            patch("jobs.crawler.tasks.Session", return_value=session_mock),
        ):
            from jobs.crawler import tasks as crawler_tasks

            updated = crawler_tasks.reapply_source_rules_task(source.id)

        assert updated == 1
        assert page.status_error is None
        assert committed["count"] == 1

    def test_does_not_touch_manually_ignored_pages(self):
        source = make_source(
            source_id=42,
            rules=[CrawlerRule(type="param", value="tag")],
        )
        page = SimpleNamespace(
            source_id=42,
            uri="https://example.com/library/",
            status="crawler",
            status_error=PageStatusError.excluded_ignored,
        )

        engine_mock = MagicMock()
        session_mock = MagicMock()
        session_mock.get.return_value = source
        session_mock.execute.return_value.scalars.return_value.all.return_value = [page]
        session_mock.__enter__ = lambda s: session_mock
        session_mock.__exit__ = MagicMock(return_value=False)

        with (
            patch("jobs.crawler.tasks.create_sync_engine", return_value=engine_mock),
            patch("jobs.crawler.tasks.Session", return_value=session_mock),
        ):
            from jobs.crawler import tasks as crawler_tasks

            updated = crawler_tasks.reapply_source_rules_task(source.id)

        assert updated == 0
        assert page.status_error == PageStatusError.excluded_ignored
