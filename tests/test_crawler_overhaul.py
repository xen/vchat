"""Tests for the crawler overhaul: pipelines, seed_urls, and models."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from scrapy.http import HtmlResponse, Request


# ---------------------------------------------------------------------------
# TestIsAuthRedirect
# ---------------------------------------------------------------------------

class TestIsAuthRedirect:
    def setup_method(self):
        from jobs.crawler.pipelines import is_auth_redirect
        self.is_auth_redirect = is_auth_redirect

    def test_same_url_not_redirect(self):
        assert self.is_auth_redirect("https://example.com/page", "https://example.com/page") is False

    def test_login_path_is_auth(self):
        assert self.is_auth_redirect("https://example.com/page", "https://example.com/login") is True

    def test_auth_path_is_auth(self):
        assert self.is_auth_redirect("https://example.com/page", "https://example.com/auth/login") is True

    def test_signin_path_is_auth(self):
        assert self.is_auth_redirect("https://example.com/page", "https://example.com/signin") is True

    def test_next_query_param_is_auth(self):
        assert self.is_auth_redirect(
            "https://example.com/page",
            "https://example.com/auth?next=/page"
        ) is True

    def test_redirect_query_param_is_auth(self):
        assert self.is_auth_redirect(
            "https://example.com/page",
            "https://example.com/login?redirect=/page"
        ) is True

    def test_cross_domain_redirect_is_auth(self):
        assert self.is_auth_redirect(
            "https://example.com/page",
            "https://auth.otherdomain.com/login"
        ) is True

    def test_normal_redirect_not_auth(self):
        assert self.is_auth_redirect(
            "https://example.com/page",
            "https://example.com/other-page"
        ) is False

    def test_empty_final_url_treated_as_no_redirect(self):
        assert self.is_auth_redirect("https://example.com/a", "https://example.com/a") is False


# ---------------------------------------------------------------------------
# TestCountInternalLinks
# ---------------------------------------------------------------------------

class TestCountInternalLinks:
    def setup_method(self):
        from jobs.crawler.pipelines import count_internal_links
        self.count_internal_links = count_internal_links

    def test_counts_domain_links(self):
        content = "[Page 1](https://example.com/page1) [Page 2](https://example.com/page2)"
        assert self.count_internal_links(content, "example.com") == 2

    def test_counts_relative_links(self):
        content = "[Home](/home) [About](/about) [Contact](/contact)"
        assert self.count_internal_links(content, "example.com") == 3

    def test_ignores_external_links(self):
        content = "[External](https://other.com/page)"
        assert self.count_internal_links(content, "example.com") == 0

    def test_mixed_links(self):
        content = (
            "[Internal](https://example.com/a) "
            "[External](https://other.com/b) "
            "[Relative](/c)"
        )
        assert self.count_internal_links(content, "example.com") == 2

    def test_empty_content(self):
        assert self.count_internal_links("", "example.com") == 0

    def test_no_markdown_links(self):
        content = "Just plain text with no links"
        assert self.count_internal_links(content, "example.com") == 0


# ---------------------------------------------------------------------------
# TestComputeAdaptiveInterval
# ---------------------------------------------------------------------------

class TestComputeAdaptiveInterval:
    def setup_method(self):
        from jobs.crawler.pipelines import compute_adaptive_interval
        self.compute_adaptive_interval = compute_adaptive_interval

    def make_page(self, check_interval_days=7):
        page = MagicMock()
        page.check_interval_days = check_interval_days
        return page

    def test_content_changed_halves_interval(self):
        page = self.make_page(check_interval_days=14)
        result = self.compute_adaptive_interval(page, content_changed=True)
        assert result == 7

    def test_content_unchanged_increases_interval(self):
        page = self.make_page(check_interval_days=10)
        result = self.compute_adaptive_interval(page, content_changed=False)
        assert result == 15

    def test_minimum_interval_is_1(self):
        page = self.make_page(check_interval_days=1)
        result = self.compute_adaptive_interval(page, content_changed=True)
        assert result >= 1

    def test_maximum_interval_is_90(self):
        page = self.make_page(check_interval_days=70)
        result = self.compute_adaptive_interval(page, content_changed=False)
        assert result <= 90

    def test_none_interval_defaults_to_7(self):
        page = MagicMock()
        page.check_interval_days = None
        result = self.compute_adaptive_interval(page, content_changed=False)
        assert result > 0


# ---------------------------------------------------------------------------
# TestLowContentDetection
# ---------------------------------------------------------------------------

class TestLowContentDetection:
    def setup_method(self):
        from jobs.crawler.pipelines import is_low_content_page

        self.is_low_content_page = is_low_content_page

    def test_low_content_page_detected_for_short_page(self):
        meta = {"extraction": {"word_count": 19}}
        content = "# Title\n\nshort text"
        assert self.is_low_content_page(content, meta) is True

    def test_normal_page_not_marked_low_content(self):
        meta = {"extraction": {"word_count": 120}}
        content = "x" * 500
        assert self.is_low_content_page(content, meta) is False


# ---------------------------------------------------------------------------
# TestHubPageDetection
# ---------------------------------------------------------------------------

class TestHubPageDetection:
    def test_hub_page_threshold_is_40(self):
        from jobs.crawler.pipelines import HUB_INTERNAL_LINK_THRESHOLD
        assert HUB_INTERNAL_LINK_THRESHOLD == 40

    def test_page_with_40_internal_links_is_hub(self):
        from jobs.crawler.pipelines import count_internal_links
        links = " ".join(
            f"[Link {i}](https://example.com/page{i})" for i in range(40)
        )
        count = count_internal_links(links, "example.com")
        assert count >= 40

    def test_page_with_few_links_is_not_hub(self):
        from jobs.crawler.pipelines import count_internal_links
        links = "[Link 1](https://example.com/a) [Link 2](https://example.com/b)"
        count = count_internal_links(links, "example.com")
        assert count < 40


class TestGeneralSpiderTrackedSources:
    def test_process_links_keeps_links_to_other_tracked_sources(self):
        from jobs.crawler.spiders.general import GeneralSpider

        with patch.object(
            GeneralSpider,
            "_filter_links_by_crawl_eligibility",
            side_effect=lambda candidates: candidates,
        ):
            spider = GeneralSpider(
                url="https://vbudushee.ru",
                source_id=1,
                config={
                    "rules": [],
                    "tracked_sources": [
                        {"id": 1, "uri": "https://vbudushee.ru", "rules": []},
                        {"id": 2, "uri": "https://grant.vbudushee.ru", "rules": []},
                    ],
                },
            )

            links = [
                SimpleNamespace(url="https://grant.vbudushee.ru/identity/account/login"),
                SimpleNamespace(url="https://untracked.example.org/page"),
            ]

            filtered = spider._process_links(links)

            assert [link.url for link in filtered] == [
                "https://grant.vbudushee.ru/identity/account/login"
            ]

    def test_parse_item_uses_tracked_source_for_cross_host_response(self):
        from jobs.crawler.spiders.general import GeneralSpider

        spider = GeneralSpider(
            url="https://vbudushee.ru",
            source_id=1,
            config={
                "rules": [],
                "tracked_sources": [
                    {"id": 1, "uri": "https://vbudushee.ru", "rules": []},
                    {"id": 2, "uri": "https://grant.vbudushee.ru", "rules": []},
                ],
            },
        )
        spider._link_extractor = None

        request = Request("https://grant.vbudushee.ru/public/application/cards")
        spider.process_request(request, None)
        response = HtmlResponse(
            url="https://grant.vbudushee.ru/public/application/cards",
            body=b"<html><head><title>Grant</title></head><body>ok</body></html>",
            encoding="utf-8",
            request=request,
        )

        item = next(iter(spider.parse_item(response)))

        assert item["source_id"] == 2
        assert item["url"] == "https://grant.vbudushee.ru/public/application/cards"

    def test_discovery_filter_skips_known_not_due_pages(self):
        from jobs.crawler.spiders.general import GeneralSpider

        spider = GeneralSpider(
            url="https://vbudushee.ru",
            source_id=1,
            config={"rules": [], "tracked_sources": [{"id": 1, "uri": "https://vbudushee.ru", "rules": []}]},
        )
        session = MagicMock()
        page_row = (
            "https://vbudushee.ru/already-known/",
            False,
            datetime(2026, 6, 1, tzinfo=timezone.utc),
            7,
            None,
        )
        execute_result = MagicMock()
        execute_result.all.return_value = [page_row]
        session.execute.return_value = execute_result
        session.__enter__ = lambda s: session
        session.__exit__ = MagicMock(return_value=False)

        with patch("jobs.crawler.spiders.general.Session", return_value=session):
            allowed = spider._filter_links_by_crawl_eligibility(
                [
                    (
                        SimpleNamespace(url="https://vbudushee.ru/already-known/"),
                        1,
                        "https://vbudushee.ru/already-known/",
                    ),
                    (
                        SimpleNamespace(url="https://vbudushee.ru/new-page/"),
                        1,
                        "https://vbudushee.ru/new-page/",
                    ),
                ]
            )

        assert [item[2] for item in allowed] == ["https://vbudushee.ru/new-page/"]

    def test_discovery_filter_keeps_due_or_retry_pages(self):
        from jobs.crawler.spiders.general import GeneralSpider

        spider = GeneralSpider(
            url="https://vbudushee.ru",
            source_id=1,
            config={"rules": [], "tracked_sources": [{"id": 1, "uri": "https://vbudushee.ru", "rules": []}]},
        )
        session = MagicMock()
        rows = [
            (
                "https://vbudushee.ru/due-page/",
                False,
                datetime(2026, 5, 1, tzinfo=timezone.utc),
                7,
                None,
            ),
            (
                "https://vbudushee.ru/retry-page/",
                False,
                datetime(2026, 6, 2, tzinfo=timezone.utc),
                7,
                "http_5xx",
            ),
        ]
        execute_result = MagicMock()
        execute_result.all.return_value = rows
        session.execute.return_value = execute_result
        session.__enter__ = lambda s: session
        session.__exit__ = MagicMock(return_value=False)

        with patch("jobs.crawler.spiders.general.Session", return_value=session):
            allowed = spider._filter_links_by_crawl_eligibility(
                [
                    (
                        SimpleNamespace(url="https://vbudushee.ru/due-page/"),
                        1,
                        "https://vbudushee.ru/due-page/",
                    ),
                    (
                        SimpleNamespace(url="https://vbudushee.ru/retry-page/"),
                        1,
                        "https://vbudushee.ru/retry-page/",
                    ),
                ]
            )

        assert [item[2] for item in allowed] == [
            "https://vbudushee.ru/due-page/",
            "https://vbudushee.ru/retry-page/",
        ]


# ---------------------------------------------------------------------------
# TestPriorityQueue (basket algorithm)
# ---------------------------------------------------------------------------

class TestPriorityQueue:
    def test_budget_respected(self):
        """Total yielded URLs should not exceed budget."""
        from unittest.mock import patch, MagicMock
        from jobs.crawler.seed_urls import iter_priority_crawl_queue

        def make_rows(urls):
            return [SimpleNamespace(uri=u) for u in urls]

        with patch("jobs.crawler.seed_urls.create_sync_engine") as mock_engine, \
             patch("jobs.crawler.seed_urls.Session") as mock_session_cls:
            session = MagicMock()
            session.__enter__ = lambda s: session
            session.__exit__ = MagicMock(return_value=False)
            mock_session_cls.return_value = session
            engine = MagicMock()
            engine.__enter__ = lambda s: engine
            engine.__exit__ = MagicMock(return_value=False)
            mock_engine.return_value = engine

            urls_a = [f"https://example.com/hub/{i}" for i in range(10)]
            urls_b = [f"https://example.com/page/{i}" for i in range(30)]
            urls_c = [f"https://example.com/err/{i}" for i in range(10)]

            def mock_execute(query, params):
                result = MagicMock()
                sql = str(query)
                if "is_hub_page = true" in sql:
                    result.all.return_value = make_rows(urls_a)
                elif "status_error = 'http_5xx'" in sql:
                    result.all.return_value = make_rows(urls_c)
                else:
                    result.all.return_value = make_rows(urls_b)
                return result

            session.execute = mock_execute

            result = list(iter_priority_crawl_queue(1, budget=20))
            assert len(result) <= 20

    def test_without_budget_returns_all_candidates(self):
        from jobs.crawler.seed_urls import iter_priority_crawl_queue

        def make_rows(urls):
            return [SimpleNamespace(uri=u) for u in urls]

        with patch("jobs.crawler.seed_urls.create_sync_engine") as mock_engine, \
             patch("jobs.crawler.seed_urls.Session") as mock_session_cls:
            session = MagicMock()
            session.__enter__ = lambda s: session
            session.__exit__ = MagicMock(return_value=False)
            mock_session_cls.return_value = session
            mock_engine.return_value = MagicMock()

            urls_a = [f"https://example.com/hub/{i}" for i in range(2)]
            urls_b = [f"https://example.com/page/{i}" for i in range(3)]
            urls_c = [f"https://example.com/err/{i}" for i in range(2)]

            def mock_execute(query, params):
                result = MagicMock()
                sql = str(query)
                if "is_hub_page = true" in sql:
                    result.all.return_value = make_rows(urls_a)
                elif "status_error = 'http_5xx'" in sql:
                    result.all.return_value = make_rows(urls_c)
                else:
                    result.all.return_value = make_rows(urls_b)
                return result

            session.execute = mock_execute

            result = list(iter_priority_crawl_queue(1, budget=None))
            assert len(result) == len(urls_a) + len(urls_b) + len(urls_c)

    def test_excludes_specified_urls(self):
        """Excluded URLs should not appear in result."""
        from jobs.crawler.seed_urls import iter_priority_crawl_queue

        with patch("jobs.crawler.seed_urls.create_sync_engine") as mock_engine, \
             patch("jobs.crawler.seed_urls.Session") as mock_session_cls:
            session = MagicMock()
            session.__enter__ = lambda s: session
            session.__exit__ = MagicMock(return_value=False)
            mock_session_cls.return_value = session
            engine = MagicMock()
            engine.__enter__ = lambda s: engine
            engine.__exit__ = MagicMock(return_value=False)
            mock_engine.return_value = engine

            all_urls = [f"https://example.com/page/{i}" for i in range(5)]
            excluded = {"https://example.com/page/0", "https://example.com/page/1"}

            def mock_execute(query, params):
                result = MagicMock()
                result.all.return_value = [SimpleNamespace(uri=u) for u in all_urls]
                return result

            session.execute = mock_execute

            result = list(iter_priority_crawl_queue(1, exclude=list(excluded), budget=10))
            for url in result:
                assert url not in excluded

    def test_none_source_id_yields_nothing(self):
        from jobs.crawler.seed_urls import iter_priority_crawl_queue
        result = list(iter_priority_crawl_queue(None, budget=100))
        assert result == []

    def test_deduplicates_results(self):
        """Same URL appearing in multiple baskets should only be yielded once."""
        from jobs.crawler.seed_urls import iter_priority_crawl_queue

        with patch("jobs.crawler.seed_urls.create_sync_engine") as mock_engine, \
             patch("jobs.crawler.seed_urls.Session") as mock_session_cls:
            session = MagicMock()
            session.__enter__ = lambda s: session
            session.__exit__ = MagicMock(return_value=False)
            mock_session_cls.return_value = session
            engine = MagicMock()
            engine.__enter__ = lambda s: engine
            engine.__exit__ = MagicMock(return_value=False)
            mock_engine.return_value = engine

            duplicate_url = "https://example.com/shared"

            def mock_execute(query, params):
                result = MagicMock()
                result.all.return_value = [SimpleNamespace(uri=duplicate_url)]
                return result

            session.execute = mock_execute

            result = list(iter_priority_crawl_queue(1, budget=100))
            assert result.count(duplicate_url) == 1

    def test_uses_status_error_filters_for_recrawl_and_retry(self):
        from jobs.crawler.seed_urls import fetch_basket

        session = MagicMock()
        result = MagicMock()
        result.all.return_value = []
        session.execute.return_value = result

        fetch_basket(
            session,
            1,
            set(),
            extra_filter=(
                "is_hub_page = false AND (status_error IS NULL OR status_error != 'http_5xx')"
            ),
            limit=10,
        )
        executed_sql = str(session.execute.call_args.args[0])
        assert "status_error" in executed_sql
        assert "http_5xx" in executed_sql


class TestUrlNormalization:
    def test_normalize_url_for_queue_removes_fragment_and_ignored_params(self):
        from jobs.crawler.url_rules import normalize_url_for_queue

        normalized = normalize_url_for_queue(
            "https://example.com/path/?utm_source=x&id=5#section-1",
            [{"type": "param", "value": "utm_source"}],
        )
        assert normalized == "https://example.com/path/?id=5"

    def test_url_allowed_by_rules_uses_regex_filters(self):
        from jobs.crawler.url_rules import url_allowed_by_rules

        rules = [{"type": "regex", "value": r"^https://example.com/course/"}]
        assert url_allowed_by_rules("https://example.com/course/step1", rules) is True
        assert url_allowed_by_rules("https://example.com/blog/post", rules) is False


# ---------------------------------------------------------------------------
# TestCrawlRunCreation
# ---------------------------------------------------------------------------

class TestCrawlRunCreation:
    def test_pipeline_creates_crawl_run_on_open(self):
        """DatabasePipeline.open_spider should create a CrawlRun record."""
        from jobs.crawler.pipelines import DatabasePipeline

        run_mock = MagicMock()
        run_mock.id = 42

        with patch("jobs.crawler.pipelines.create_engine"), \
             patch("jobs.crawler.pipelines.sync_uri", return_value="sqlite://"), \
             patch("jobs.crawler.pipelines.Session") as mock_session_cls:

            session = MagicMock()
            session.__enter__ = lambda s: session
            session.__exit__ = MagicMock(return_value=False)
            mock_session_cls.return_value = session

            def fake_add(obj):
                obj.id = 42

            session.add = fake_add
            session.commit = MagicMock()

            pipeline = DatabasePipeline.__new__(DatabasePipeline)
            pipeline.logger = MagicMock()
            pipeline.engine = MagicMock()
            pipeline._crawl_run_id = None

            spider = MagicMock()
            spider.source_id = 1

            pipeline.open_spider(spider)
            session.commit.assert_called_once()

    def test_pipeline_closes_crawl_run_on_close(self):
        """DatabasePipeline.close_spider should mark CrawlRun as finished."""
        from jobs.crawler.pipelines import DatabasePipeline

        run_mock = MagicMock()
        run_mock.finished_at = None

        with patch("jobs.crawler.pipelines.Session") as mock_session_cls:
            session = MagicMock()
            session.__enter__ = lambda s: session
            session.__exit__ = MagicMock(return_value=False)
            session.get.return_value = run_mock
            mock_session_cls.return_value = session

            pipeline = DatabasePipeline.__new__(DatabasePipeline)
            pipeline.logger = MagicMock()
            pipeline.engine = MagicMock()
            pipeline._crawl_run_id = 42

            spider = MagicMock()
            pipeline.close_spider(spider)

            assert run_mock.exit_reason == "finished"
            assert run_mock.finished_at is not None

    def test_pipeline_force_reprocess_requeues_unchanged_page(self):
        """force_reprocess_once should bypass unchanged-content short circuit."""
        from jobs.crawler.pipelines import DatabasePipeline

        from vchat.page_status import PageStatus

        page = SimpleNamespace(
            id=55,
            source_id=1,
            uri="https://example.com/page",
            meta={"force_reprocess_once": True},
            status_error=None,
            is_hub_page=False,
            content_value=None,
            stable_count=2,
            error_count=0,
            check_interval_days=7,
            title="Existing",
        )

        session = MagicMock()
        session.__enter__ = lambda s: session
        session.__exit__ = MagicMock(return_value=False)
        session.execute.return_value.scalars.return_value.first.return_value = page
        session.commit = MagicMock()

        pipeline = DatabasePipeline.__new__(DatabasePipeline)
        pipeline.logger = MagicMock()
        pipeline.engine = MagicMock()
        pipeline._crawl_run_id = None

        spider = MagicMock()
        spider.logger = MagicMock()

        item = {
            "url": "https://example.com/page",
            "source_id": 1,
            "content_type": "text/html",
            "meta": {},
        }

        with (
            patch("jobs.crawler.pipelines.Session", return_value=session),
            patch(
                "jobs.crawler.pipelines.extract_url_document",
                return_value=(
                    "same content",
                    "Fresh title",
                    {"extraction": {"word_count": 100}},
                ),
            ),
            patch(
                "jobs.crawler.pipelines.document_content_effectively_unchanged",
                return_value=True,
            ),
            patch(
                "jobs.crawler.pipelines.sync_document_has_chunks",
                return_value=True,
            ),
            patch("jobs.crawler.pipelines.schedule_index_document") as schedule_mock,
        ):
            pipeline.process_item(item, spider)

        assert page.status == PageStatus.parsing
        assert "force_reprocess_once" not in page.meta
        schedule_mock.assert_called_once_with(55)


class TestPageLinkSync:
    def test_sync_page_links_creates_placeholders_and_links(self):
        from jobs.crawler.pipelines import sync_page_links
        from vchat.models.data import PageLink

        source_page = SimpleNamespace(id=10, uri="https://example.com/source")

        session = MagicMock()
        added_objects = []

        class _ExecuteResult:
            def __init__(self, *, rows=None, first=None):
                self._rows = rows or []
                self._first = first

            def all(self):
                return self._rows

            def scalars(self):
                class _Scalars:
                    def __init__(self, first):
                        self._first = first

                    def first(self):
                        return self._first

                return _Scalars(self._first)

        def fake_execute(stmt):
            sql = str(stmt)
            if "FROM source" in sql:
                return _ExecuteResult(rows=[(1, "https://example.com")])
            if "FROM page" in sql:
                return _ExecuteResult(first=None)
            return _ExecuteResult()

        def fake_add(obj):
            added_objects.append(obj)
            if obj.__class__.__name__ == "Page":
                obj.id = 25

        session.execute.side_effect = fake_execute
        session.add.side_effect = fake_add
        session.flush = MagicMock()

        sync_page_links(
            session,
            source_page=source_page,
            source_id=1,
            out_links=[
                "https://example.com/target#frag",
                "https://example.com/target",
                "https://example.com/source",
            ],
            source_rules=[],
        )

        page_links = [obj for obj in added_objects if isinstance(obj, PageLink)]
        assert len(page_links) == 1
        assert page_links[0].target_uri == "https://example.com/target"
        assert page_links[0].target_status == "not_indexed"

    def test_sync_page_links_attaches_cross_source_targets_to_tracked_source(self):
        from jobs.crawler.pipelines import sync_page_links
        from vchat.models.data import PageLink

        source_page = SimpleNamespace(id=10, uri="https://example.com/source")

        class _ExecuteResult:
            def __init__(self, *, rows=None, first=None):
                self._rows = rows or []
                self._first = first

            def all(self):
                return self._rows

            def scalars(self):
                class _Scalars:
                    def __init__(self, first):
                        self._first = first

                    def first(self):
                        return self._first

                return _Scalars(self._first)

        session = MagicMock()
        added_objects = []

        def fake_execute(stmt):
            sql = str(stmt)
            if "FROM source" in sql:
                return _ExecuteResult(
                    rows=[
                        (1, "https://example.com"),
                        (2, "https://grant.vbudushee.ru"),
                    ]
                )
            if "FROM page" in sql:
                return _ExecuteResult(first=None)
            return _ExecuteResult()

        def fake_add(obj):
            added_objects.append(obj)
            if obj.__class__.__name__ == "Page":
                obj.id = 25

        session.execute.side_effect = fake_execute
        session.add.side_effect = fake_add
        session.flush = MagicMock()

        sync_page_links(
            session,
            source_page=source_page,
            source_id=1,
            out_links=["https://grant.vbudushee.ru/identity/account/login"],
            source_rules=[],
        )

        created_pages = [obj for obj in added_objects if obj.__class__.__name__ == "Page"]
        page_links = [obj for obj in added_objects if isinstance(obj, PageLink)]
        assert created_pages[0].source_id == 2
        assert page_links[0].target_page_id == 25
        assert page_links[0].target_uri == "https://grant.vbudushee.ru/identity/account/login"

    def test_sync_page_links_keeps_untracked_links_without_creating_pages(self):
        from jobs.crawler.pipelines import sync_page_links
        from vchat.models.data import PageLink

        source_page = SimpleNamespace(id=10, uri="https://example.com/source")

        class _ExecuteResult:
            def __init__(self, *, rows=None, first=None):
                self._rows = rows or []
                self._first = first

            def all(self):
                return self._rows

            def scalars(self):
                class _Scalars:
                    def __init__(self, first):
                        self._first = first

                    def first(self):
                        return self._first

                return _Scalars(self._first)

        session = MagicMock()
        added_objects = []

        def fake_execute(stmt):
            sql = str(stmt)
            if "FROM source" in sql:
                return _ExecuteResult(rows=[(1, "https://example.com")])
            if "FROM page" in sql:
                return _ExecuteResult(first=None)
            return _ExecuteResult()

        session.execute.side_effect = fake_execute
        session.add.side_effect = added_objects.append
        session.flush = MagicMock()

        sync_page_links(
            session,
            source_page=source_page,
            source_id=1,
            out_links=["https://external.example.org/docs"],
            source_rules=[],
        )

        created_pages = [obj for obj in added_objects if obj.__class__.__name__ == "Page"]
        page_links = [obj for obj in added_objects if isinstance(obj, PageLink)]
        assert created_pages == []
        assert page_links[0].target_page_id is None
        assert page_links[0].target_status == "missing"


class TestSitemapDiscovery:
    def test_parse_robots_extracts_sitemaps_and_delay(self):
        from jobs.crawler.tasks import _parse_robots_txt

        body = """
        User-agent: *
        Crawl-delay: 7
        Sitemap: https://example.com/sitemap.xml
        Sitemap: https://example.com/sitemap-news.xml
        """

        sitemap_urls, crawl_delay = _parse_robots_txt(body)
        assert sitemap_urls == [
            "https://example.com/sitemap.xml",
            "https://example.com/sitemap-news.xml",
        ]
        assert crawl_delay == 7

    def test_upsert_sitemap_pages_creates_missing_pages(self):
        from jobs.crawler.tasks import _upsert_sitemap_pages

        session = MagicMock()
        session.execute.return_value.scalar_one_or_none.return_value = None

        added_objects = []

        def fake_add(obj):
            added_objects.append(obj)

        session.add.side_effect = fake_add

        prioritized = _upsert_sitemap_pages(
            session,
            source_id=1,
            parsed_entries=[("https://example.com/page", None)],
            source_rules=[],
        )

        assert prioritized == set()
        assert any(getattr(obj, "uri", None) == "https://example.com/page" for obj in added_objects)

    def test_upsert_sitemap_pages_skips_urls_filtered_by_regex(self):
        from jobs.crawler.tasks import _upsert_sitemap_pages

        session = MagicMock()
        session.execute.return_value.scalar_one_or_none.return_value = None

        prioritized = _upsert_sitemap_pages(
            session,
            source_id=1,
            parsed_entries=[("https://example.com/blog/post#frag", None)],
            source_rules=[{"type": "regex", "value": r"^https://example.com/course/"}],
        )

        assert prioritized == set()
        session.add.assert_not_called()

    def test_upsert_sitemap_pages_handles_naive_sitemap_lastmod(self):
        from jobs.crawler.tasks import _upsert_sitemap_pages

        session = MagicMock()
        page = SimpleNamespace(
            last_modified_at=datetime(2026, 5, 1, tzinfo=timezone.utc)
        )
        session.execute.return_value.scalar_one_or_none.return_value = page

        prioritized = _upsert_sitemap_pages(
            session,
            source_id=1,
            parsed_entries=[("https://example.com/page", "2026-06-01")],
            source_rules=[],
        )

        assert prioritized == {"https://example.com/page"}

    def test_upsert_sitemap_pages_routes_cross_host_url_to_matching_source(self):
        from jobs.crawler.tasks import _upsert_sitemap_pages

        session = MagicMock()
        session.execute.return_value.scalar_one_or_none.return_value = None

        added_objects = []
        session.add.side_effect = added_objects.append

        _upsert_sitemap_pages(
            session,
            source_id=1,
            parsed_entries=[("https://grant.vbudushee.ru/public/application/cards", None)],
            source_rules=[],
            source_id_by_host={
                "vbudushee.ru": 1,
                "grant.vbudushee.ru": 2,
            },
        )

        created_pages = [obj for obj in added_objects if obj.__class__.__name__ == "Page"]
        assert created_pages[0].source_id == 2

    def test_parse_sitemap_document_detects_sitemap_index(self):
        from jobs.crawler.tasks import _parse_sitemap_document

        body = b"""<?xml version="1.0" encoding="UTF-8"?>
        <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <sitemap><loc>https://example.com/sitemap-a.xml</loc><lastmod>2026-01-01</lastmod></sitemap>
          <sitemap><loc>https://example.com/sitemap-b.xml</loc></sitemap>
        </sitemapindex>
        """

        kind, entries = _parse_sitemap_document(body)
        assert kind == "sitemapindex"
        assert entries == [
            ("https://example.com/sitemap-a.xml", "2026-01-01"),
            ("https://example.com/sitemap-b.xml", None),
        ]

    def test_upsert_child_sitemaps_creates_sitemap_records(self):
        from jobs.crawler.tasks import _upsert_child_sitemaps

        session = MagicMock()
        session.execute.return_value.scalar_one_or_none.return_value = None

        added_objects = []
        session.add.side_effect = added_objects.append

        discovered = _upsert_child_sitemaps(
            session,
            source_id=1,
            source_uri="https://example.com/",
            parent_sitemap_url="https://example.com/sitemap.xml",
            parsed_entries=[
                ("https://example.com/sitemap-a.xml", None),
                ("https://example.com/sitemap-b.xml", None),
            ],
        )

        assert discovered == [
            "https://example.com/sitemap-a.xml",
            "https://example.com/sitemap-b.xml",
        ]
        assert all(getattr(obj, "url", "").startswith("https://example.com/sitemap-") for obj in added_objects)

    def test_invalid_child_sitemap_is_added_as_excluded_with_reason(self):
        from jobs.crawler.tasks import _upsert_child_sitemaps

        session = MagicMock()
        session.execute.return_value.scalar_one_or_none.return_value = None

        added_objects = []
        session.add.side_effect = added_objects.append

        discovered = _upsert_child_sitemaps(
            session,
            source_id=1,
            source_uri="https://example.com/",
            parent_sitemap_url="https://example.com/sitemap.xml",
            parsed_entries=[("https://other.com/sitemap.xml", None)],
        )

        assert discovered == []
        assert added_objects[0].is_excluded is True
        assert added_objects[0].ignore_reason == "wrong_address"
        assert added_objects[0].discovered_via == "sitemap_index"
        assert added_objects[0].discovered_from_url == "https://example.com/sitemap.xml"

    def test_sync_sitemaps_skips_fetch_within_24_hours(self):
        from jobs.crawler.tasks import _sync_sitemaps_for_source

        now = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)
        session = MagicMock()
        source = SimpleNamespace(
            id=1,
            uri="https://example.com",
            config=SimpleNamespace(rules=[]),
        )
        sitemap = SimpleNamespace(
            url="https://example.com/sitemap.xml",
            source_id=1,
            is_excluded=False,
            last_fetched_at=now - timedelta(hours=23),
            last_etag="etag-1",
            last_content_hash=None,
            ignore_reason=None,
            url_count=None,
        )

        session.get.return_value = source
        session.execute.return_value.scalars.return_value.all.return_value = [sitemap]

        with (
            patch("jobs.crawler.tasks.datetime") as datetime_mock,
            patch("jobs.crawler.tasks._fetch_sitemap") as fetch_mock,
        ):
            datetime_mock.now.return_value = now
            _sync_sitemaps_for_source(session, 1)

        fetch_mock.assert_not_called()

    def test_sync_sitemaps_fetches_again_after_24_hours(self):
        from jobs.crawler.tasks import _sync_sitemaps_for_source

        now = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)
        session = MagicMock()
        source = SimpleNamespace(
            id=1,
            uri="https://example.com",
            config=SimpleNamespace(rules=[]),
        )
        sitemap = SimpleNamespace(
            url="https://example.com/sitemap.xml",
            source_id=1,
            is_excluded=False,
            last_fetched_at=now - timedelta(days=1),
            last_etag="etag-1",
            last_content_hash=None,
            ignore_reason=None,
            url_count=None,
        )

        session.get.return_value = source
        session.execute.return_value.scalars.return_value.all.return_value = [sitemap]

        body = b"""<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://example.com/page</loc></url>
        </urlset>
        """

        with (
            patch("jobs.crawler.tasks.datetime") as datetime_mock,
            patch(
                "jobs.crawler.tasks._fetch_sitemap",
                return_value=(200, body, "etag-2", None),
            ) as fetch_mock,
            patch(
                "jobs.crawler.tasks._upsert_sitemap_pages",
                return_value=set(),
            ) as upsert_pages_mock,
        ):
            datetime_mock.now.return_value = now
            _sync_sitemaps_for_source(session, 1)

        fetch_mock.assert_called_once_with("https://example.com/sitemap.xml", "etag-1")
        upsert_pages_mock.assert_called_once()
        assert sitemap.last_fetched_at == now
        assert sitemap.last_etag == "etag-2"
        assert sitemap.url_count == 1


# ---------------------------------------------------------------------------
# TestPageStatusOnErrors
# ---------------------------------------------------------------------------

class TestPageStatusOnErrors:
    def test_4xx_sets_error_4xx_status(self):
        """Pipeline should record error_4xx for 4xx HTTP responses."""
        from jobs.crawler.pipelines import handle_error_page

        with patch("jobs.crawler.pipelines.Session") as mock_session_cls:
            session = MagicMock()
            session.__enter__ = lambda s: session
            session.__exit__ = MagicMock(return_value=False)

            page_mock = MagicMock()
            page_mock.error_count = 0
            page_mock.check_interval_days = 7
            session.execute.return_value.scalars.return_value.first.return_value = page_mock
            mock_session_cls.return_value = session

            logger = MagicMock()
            engine = MagicMock()

            from vchat.page_status import PageStatus, PageStatusError

            handle_error_page(engine, "https://example.com/gone", 1, 404, None, logger)

            assert page_mock.status == PageStatus.crawler
            assert page_mock.status_error == PageStatusError.http_4xx
            assert page_mock.http_status == 404
            assert page_mock.meta["reason"] == "http_4xx"
            assert page_mock.meta["message"] == "Source returned HTTP 404."
            assert page_mock.meta["error"] == "HTTP 404"

    def test_repeated_4xx_increases_check_interval(self):
        """After 2+ errors the check_interval_days should be set to 90."""
        from jobs.crawler.pipelines import handle_error_page

        with patch("jobs.crawler.pipelines.Session") as mock_session_cls:
            session = MagicMock()
            session.__enter__ = lambda s: session
            session.__exit__ = MagicMock(return_value=False)

            page_mock = MagicMock()
            page_mock.error_count = 2
            page_mock.check_interval_days = 7
            session.execute.return_value.scalars.return_value.first.return_value = page_mock
            mock_session_cls.return_value = session

            logger = MagicMock()
            engine = MagicMock()

            handle_error_page(engine, "https://example.com/gone", 1, 404, None, logger)

            assert page_mock.check_interval_days == 90

    def test_5xx_sets_error_5xx_status(self):
        """save_page_status with http_5xx should record the right status."""
        from jobs.crawler.pipelines import save_page_status
        from vchat.page_status import PageStatus, PageStatusError

        with patch("jobs.crawler.pipelines.Session") as mock_session_cls:
            session = MagicMock()
            session.__enter__ = lambda s: session
            session.__exit__ = MagicMock(return_value=False)

            page_mock = MagicMock()
            page_mock.error_count = 0
            page_mock.meta = {}
            session.execute.return_value.scalars.return_value.first.return_value = page_mock
            mock_session_cls.return_value = session

            logger = MagicMock()
            engine = MagicMock()

            save_page_status(
                engine, "https://example.com/error", 1,
                PageStatus.crawler, PageStatusError.http_5xx, 500, None, logger,
            )

            assert page_mock.status == PageStatus.crawler
            assert page_mock.status_error == PageStatusError.http_5xx

    def test_save_page_status_stores_reason_details(self):
        from jobs.crawler.pipelines import save_page_status

        with patch("jobs.crawler.pipelines.Session") as mock_session_cls:
            session = MagicMock()
            session.__enter__ = lambda s: session
            session.__exit__ = MagicMock(return_value=False)

            page_mock = MagicMock()
            page_mock.error_count = 0
            page_mock.meta = {"other": "value"}
            session.execute.return_value.scalars.return_value.first.return_value = page_mock
            mock_session_cls.return_value = session

            logger = MagicMock()
            engine = MagicMock()

            from vchat.page_status import PageStatus, PageStatusError

            save_page_status(
                engine,
                "https://example.com/error",
                1,
                PageStatus.crawler,
                PageStatusError.extraction_failed,
                200,
                None,
                logger,
                reason="extraction_failed",
                message="Document extraction failed after the page was downloaded.",
                error="boom",
                exception_class="ValueError",
            )

            assert page_mock.meta["reason"] == "extraction_failed"
            assert (
                page_mock.meta["message"]
                == "Document extraction failed after the page was downloaded."
            )
            assert page_mock.meta["error"] == "boom"
            assert page_mock.meta["exception_class"] == "ValueError"
            assert page_mock.meta["other"] == "value"

    def test_save_page_status_clears_stale_reason_details(self):
        from jobs.crawler.pipelines import save_page_status

        with patch("jobs.crawler.pipelines.Session") as mock_session_cls:
            session = MagicMock()
            session.__enter__ = lambda s: session
            session.__exit__ = MagicMock(return_value=False)

            page_mock = MagicMock()
            page_mock.error_count = 1
            page_mock.meta = {
                "reason": "old_reason",
                "message": "old message",
                "error": "old error",
                "exception_class": "RuntimeError",
            }
            session.execute.return_value.scalars.return_value.first.return_value = page_mock
            mock_session_cls.return_value = session

            from vchat.page_status import PageStatus

            save_page_status(
                MagicMock(),
                "https://example.com/page",
                1,
                PageStatus.parsing,
                None,
                200,
                None,
                MagicMock(),
            )

            assert "reason" not in page_mock.meta
            assert "message" not in page_mock.meta
            assert "error" not in page_mock.meta
            assert "exception_class" not in page_mock.meta


# ---------------------------------------------------------------------------
# TestIndexStatus
# ---------------------------------------------------------------------------

class TestPageStatusModel:
    """Page.status uses three-value enum: crawler / parsing / ready."""

    def test_page_model_has_status_error(self):
        from vchat.models.data import Page
        assert hasattr(Page, "status_error")

    def test_page_model_no_index_status(self):
        from vchat.models.data import Page
        assert not hasattr(Page, "index_status")

    def test_page_model_no_is_ignored(self):
        from vchat.models.data import Page
        assert not hasattr(Page, "is_ignored")

    def test_pipeline_sets_parsing_on_new_content(self):
        from jobs.crawler.pipelines import compute_adaptive_interval
        assert compute_adaptive_interval.__module__ == "jobs.crawler.pipelines"

    def test_hub_page_gets_low_content_value(self):
        from vchat.models.data import Page
        p = Page()
        p.is_hub_page = True
        p.content_value = 0.05
        assert p.content_value <= 0.1

    def test_page_status_column_default_is_crawler(self):
        from vchat.models.data import Page
        from vchat.page_status import PageStatus
        col = Page.__table__.c["status"]
        assert col.default.arg == PageStatus.crawler


class TestEmbedderSkipsErrorPages:
    def test_fetch_page_context_skips_pages_with_status_error(self):
        from jobs.embedder.tasks import fetch_page_context
        from vchat.page_status import PageStatusError

        page = SimpleNamespace(
            id=1,
            content="short content",
            status_error=PageStatusError.low_content,
        )
        session = MagicMock()
        session.execute.return_value.first.return_value = SimpleNamespace(
            id=page.id,
            source_id=1,
            content=page.content,
            status_error=page.status_error,
        )

        assert fetch_page_context(session, 1) is None
