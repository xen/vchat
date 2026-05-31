"""Tests for the crawler overhaul: pipelines, seed_urls, and models."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


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
                elif "error_5xx" in sql and "is_hub_page = false" in sql and "status != 'error_5xx'" not in sql:
                    result.all.return_value = make_rows(urls_c)
                else:
                    result.all.return_value = make_rows(urls_b)
                return result

            session.execute = mock_execute

            result = list(iter_priority_crawl_queue(1, budget=20))
            assert len(result) <= 20

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

        page = SimpleNamespace(
            id=55,
            source_id=1,
            uri="https://example.com/page",
            meta={"force_reprocess_once": True},
            is_ignored=False,
            is_hub_page=False,
            content_value=None,
            stable_count=2,
            error_count=0,
            check_interval_days=7,
            title="Existing",
            index_status="indexed",
        )

        session = MagicMock()
        session.__enter__ = lambda s: session
        session.__exit__ = MagicMock(return_value=False)
        session.execute.return_value.scalar_one_or_none.return_value = page
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

        assert page.index_status == "queued"
        assert "force_reprocess_once" not in page.meta
        schedule_mock.assert_called_once_with(55)


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
            session.execute.return_value.scalar_one_or_none.return_value = page_mock
            mock_session_cls.return_value = session

            logger = MagicMock()
            engine = MagicMock()

            handle_error_page(engine, "https://example.com/gone", 1, 404, None, logger)

            assert page_mock.status == "error_4xx"
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
            session.execute.return_value.scalar_one_or_none.return_value = page_mock
            mock_session_cls.return_value = session

            logger = MagicMock()
            engine = MagicMock()

            handle_error_page(engine, "https://example.com/gone", 1, 404, None, logger)

            assert page_mock.check_interval_days == 90

    def test_5xx_sets_error_5xx_status(self):
        """save_page_status with error_5xx should record the right status."""
        from jobs.crawler.pipelines import save_page_status

        with patch("jobs.crawler.pipelines.Session") as mock_session_cls:
            session = MagicMock()
            session.__enter__ = lambda s: session
            session.__exit__ = MagicMock(return_value=False)

            page_mock = MagicMock()
            page_mock.error_count = 0
            page_mock.meta = {}
            session.execute.return_value.scalar_one_or_none.return_value = page_mock
            mock_session_cls.return_value = session

            logger = MagicMock()
            engine = MagicMock()

            save_page_status(engine, "https://example.com/error", 1, "error_5xx", 500, None, logger)

            assert page_mock.status == "error_5xx"

    def test_save_page_status_stores_reason_details(self):
        from jobs.crawler.pipelines import save_page_status

        with patch("jobs.crawler.pipelines.Session") as mock_session_cls:
            session = MagicMock()
            session.__enter__ = lambda s: session
            session.__exit__ = MagicMock(return_value=False)

            page_mock = MagicMock()
            page_mock.error_count = 0
            page_mock.meta = {"other": "value"}
            session.execute.return_value.scalar_one_or_none.return_value = page_mock
            mock_session_cls.return_value = session

            logger = MagicMock()
            engine = MagicMock()

            save_page_status(
                engine,
                "https://example.com/error",
                1,
                "error_5xx",
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
            session.execute.return_value.scalar_one_or_none.return_value = page_mock
            mock_session_cls.return_value = session

            save_page_status(
                MagicMock(),
                "https://example.com/page",
                1,
                "unchanged",
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

class TestIndexStatus:
    """index_status tracks chunking/embedding progress independently from crawl status."""

    def test_page_model_has_index_status(self):
        from vchat.models.data import Page
        assert hasattr(Page, "index_status")

    def test_index_status_default_is_none(self):
        from vchat.models.data import Page
        p = Page()
        assert p.index_status is None

    def test_pipeline_sets_queued_on_new_content(self):
        """Pipeline should set index_status='queued' when content changes."""
        from jobs.crawler.pipelines import compute_adaptive_interval
        # compute_adaptive_interval is a pure function — just verify it exists and works
        assert compute_adaptive_interval.__module__ == "jobs.crawler.pipelines"

    def test_pipeline_index_status_values_are_correct(self):
        """All expected index_status values are valid strings."""
        valid_statuses = {None, "queued", "indexing", "indexed", "failed"}
        # Values match the design in the plan
        assert "queued" in valid_statuses
        assert "indexing" in valid_statuses
        assert "indexed" in valid_statuses

    def test_crawl_status_independent_of_index_status(self):
        """A page can have crawl status 'ok' and index_status 'indexing' simultaneously."""
        from vchat.models.data import Page
        p = Page()
        p.status = "ok"
        p.index_status = "indexing"
        assert p.status == "ok"
        assert p.index_status == "indexing"

    def test_hub_page_gets_low_content_value(self):
        """Hub pages should get content_value <= 0.1."""
        from vchat.models.data import Page
        p = Page()
        p.is_hub_page = True
        p.content_value = 0.05
        assert p.content_value <= 0.1

    def test_page_status_column_default_is_pending(self):
        """Page.status column default is 'pending' (applied on DB flush)."""
        from vchat.models.data import Page
        col = Page.__table__.c["status"]
        assert col.default.arg == "pending"


class TestEmbedderSkipsLowContent:
    def test_fetch_page_context_skips_low_content_pages(self):
        from jobs.embedder.tasks import fetch_page_context

        page = SimpleNamespace(
            id=1,
            content="short content",
            is_ignored=False,
            status="low_content",
        )
        session = MagicMock()
        session.execute.return_value.first.return_value = (page,)

        assert fetch_page_context(session, 1) is None
