"""Tests for source settings view (stats, 500 regression) and pause/resume actions."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from yarl import URL

from vchat.models.source_config import SourceConfig
from vchat.views.projects.forms import SourceSettingsForm
from vchat.views.projects import views as project_views


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _Route:
    def __init__(self, path: str):
        self._path = path

    def url_for(self, **kwargs):
        return URL(self._path)


class _App(dict):
    def __init__(self, routes: dict | None = None):
        super().__init__()
        self.router = {
            "project_edit": _Route("/edit"),
            "project_source_settings": _Route("/sources/10/settings"),
            **(routes or {}),
        }
        self[project_views.SIGNER_KEY] = SimpleNamespace(loads=lambda token, max_age: 1)
        self[project_views.SETTINGS_KEY] = {}


class _StatsDB:
    """DB mock that returns configurable scalar and execute (one-row) results."""

    def __init__(
        self,
        scalar_values: list | None = None,
        one_rows: list | None = None,
    ):
        self.scalar_values = list(scalar_values or [])
        self.one_rows = list(one_rows or [])
        self.commits = 0
        self.added = []
        self.deleted = []

    async def scalar(self, stmt):
        _ = stmt
        return self.scalar_values.pop(0) if self.scalar_values else None

    async def execute(self, stmt):
        _ = stmt
        row = self.one_rows.pop(0) if self.one_rows else SimpleNamespace()
        return SimpleNamespace(
            one=lambda: row,
            all=lambda: [],
            scalars=lambda: SimpleNamespace(all=lambda: []),
        )

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.commits += 1

    async def flush(self):
        pass

    async def refresh(self, obj):
        pass


class _Req(dict):
    def __init__(self, *, method="GET", post_data=None, source_id="10"):
        super().__init__()
        self.method = method
        self._post_data = post_data or {}
        self.path = f"/sources/{source_id}/settings"
        self.headers = {}
        self.app = _App()
        self.match_info = {"source_id": source_id, "action": "", "item_id": source_id}

    async def post(self):
        return _MultiDict(self._post_data)

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


class _MultiDict(dict):
    def getall(self, key, default=None):
        v = self.get(key, default or [])
        return v if isinstance(v, list) else [v]


def _make_source(source_id=10, uri="https://example.com", is_paused=False):
    s = MagicMock()
    s.id = source_id
    s.uri = uri
    s.title = "Example"
    s.reindex_cron = "0 3 * * 1"
    s.last_reindexed_at = None
    s.config = SourceConfig()
    s.is_paused = is_paused
    s.updated_at = None
    return s


def _make_stats_db(
    source, doc_count=5, doc_bytes=10240, chunk_count=3, chunk_bytes=4096
):
    doc_row = SimpleNamespace(doc_count=doc_count, doc_size_bytes=doc_bytes)
    chunk_row = SimpleNamespace(chunk_count=chunk_count, chunk_size_bytes=chunk_bytes)
    return _StatsDB(
        scalar_values=[source],
        one_rows=[doc_row, chunk_row],
    )


def _raw(func):
    while hasattr(func, "__wrapped__"):
        func = func.__wrapped__
    return func


# ---------------------------------------------------------------------------
# next_reindex_at
# ---------------------------------------------------------------------------


class TestNextReindexAt:
    def test_returns_none_for_manual(self):
        from vchat.views.projects.views import next_reindex_at

        assert next_reindex_at("manual", datetime.now(timezone.utc)) is None

    def test_returns_none_for_empty(self):
        from vchat.views.projects.views import next_reindex_at

        assert next_reindex_at("", datetime.now(timezone.utc)) is None

    def test_returns_datetime_for_valid_cron(self):
        from vchat.views.projects.views import next_reindex_at

        now = datetime.now(timezone.utc)
        result = next_reindex_at("0 3 * * 1", now)
        assert result is not None
        assert result > now

    def test_returns_none_for_invalid_cron(self):
        from vchat.views.projects.views import next_reindex_at

        result = next_reindex_at("not a cron", datetime.now(timezone.utc))
        assert result is None


# ---------------------------------------------------------------------------
# project_source_settings GET — 500 regression test
# The bug: view returned only {project, source, form}, missing stats vars
# Template needs doc_count, doc_size_bytes, chunk_count, chunk_size_bytes, next_reindex
# ---------------------------------------------------------------------------


class TestSourceSettingsView:
    def test_template_references_only_existing_form_fields(self) -> None:
        template_path = (
            Path(__file__).resolve().parents[1]
            / "vchat"
            / "templates"
            / "projects"
            / "source_settings.html"
        )
        field_names = set(
            re.findall(r"\bform\.([A-Za-z_][A-Za-z0-9_]*)\b", template_path.read_text())
        )
        form = SourceSettingsForm(meta={"csrf_context": {}})

        missing = sorted(name for name in field_names if name not in form._fields)
        assert missing == []

    @pytest.mark.asyncio
    async def test_get_returns_all_required_template_vars(self, monkeypatch):
        """Regression: source settings 500 due to missing doc_count/chunk_count vars."""
        source = _make_source()
        db = _make_stats_db(
            source, doc_count=42, doc_bytes=8192, chunk_count=7, chunk_bytes=2048
        )
        req = _Req(method="GET")
        req["db"] = db
        req["user"] = SimpleNamespace(id=1)

        monkeypatch.setattr(
            project_views,
            "_project_context",
            lambda request: SimpleNamespace(id="global"),
        )

        with patch("vchat.views.projects.views.get_session") as mock_session:
            mock_session.return_value = {}
            raw = _raw(project_views.project_source_settings)
            result = await raw(req)

        assert isinstance(result, dict)
        assert "doc_count" in result, "doc_count must be in template context"
        assert "doc_size_bytes" in result, "doc_size_bytes must be in template context"
        assert "chunk_count" in result, "chunk_count must be in template context"
        assert "chunk_size_bytes" in result, (
            "chunk_size_bytes must be in template context"
        )
        assert "next_reindex" in result, "next_reindex must be in template context"
        assert result["doc_count"] == 42
        assert result["chunk_count"] == 7

    @pytest.mark.asyncio
    async def test_get_source_not_found_raises_404(self, monkeypatch):
        db = _StatsDB(scalar_values=[None])
        req = _Req(method="GET")
        req["db"] = db
        req["user"] = SimpleNamespace(id=1)

        monkeypatch.setattr(
            project_views,
            "_project_context",
            lambda request: SimpleNamespace(id="global"),
        )

        with patch("vchat.views.projects.views.get_session"):
            raw = _raw(project_views.project_source_settings)
            with pytest.raises(web.HTTPNotFound):
                await raw(req)

    @pytest.mark.asyncio
    async def test_get_paused_source_includes_is_paused_true(self, monkeypatch):
        source = _make_source(is_paused=True)
        db = _make_stats_db(source)
        req = _Req(method="GET")
        req["db"] = db
        req["user"] = SimpleNamespace(id=1)

        monkeypatch.setattr(
            project_views,
            "_project_context",
            lambda request: SimpleNamespace(id="global"),
        )

        with patch("vchat.views.projects.views.get_session"):
            raw = _raw(project_views.project_source_settings)
            result = await raw(req)

        assert result["source"].is_paused is True


# ---------------------------------------------------------------------------
# pause_source / resume_source actions
# ---------------------------------------------------------------------------


class TestPauseResumeActions:
    def _make_action_req(self, action, item_id="10"):
        req = _Req(method="POST")
        req.match_info["action"] = action
        req.match_info["item_id"] = item_id
        req["user"] = SimpleNamespace(id=1)
        req.headers["X-CSRFToken"] = "token"
        req.app[project_views.SIGNER_KEY] = SimpleNamespace(
            loads=lambda token, max_age: 1
        )
        return req

    @pytest.mark.asyncio
    async def test_pause_source_sets_is_paused_true(self, monkeypatch):
        source = _make_source(is_paused=False)
        db = _StatsDB(scalar_values=[source])
        req = self._make_action_req("pause_source")
        req["db"] = db

        events = []

        async def fake_event(name, _req):
            events.append(name)

        monkeypatch.setattr(project_views, "admin_event", fake_event)

        raw = _raw(project_views.project_action)
        resp = await raw(req)

        assert resp.status == 200
        assert source.is_paused is True
        assert db.commits == 1
        assert "source_pause" in events

    @pytest.mark.asyncio
    async def test_resume_source_sets_is_paused_false(self, monkeypatch):
        source = _make_source(is_paused=True)
        db = _StatsDB(scalar_values=[source])
        req = self._make_action_req("resume_source")
        req["db"] = db

        events = []

        async def fake_event(name, _req):
            events.append(name)

        monkeypatch.setattr(project_views, "admin_event", fake_event)

        raw = _raw(project_views.project_action)
        resp = await raw(req)

        assert resp.status == 200
        assert source.is_paused is False
        assert db.commits == 1
        assert "source_resume" in events

    @pytest.mark.asyncio
    async def test_pause_source_htmx_returns_toggle_partial(self, monkeypatch):
        source = _make_source(is_paused=False)
        row = SimpleNamespace(
            id=source.id,
            title=source.title,
            uri=source.uri,
            is_paused=True,
            excluded=0,
            errors=0,
            pending=0,
            processing=0,
            ready=0,
        )
        db = _StatsDB(scalar_values=[source], one_rows=[row])
        req = self._make_action_req("pause_source")
        req["db"] = db
        req.headers["HX-Request"] = "true"

        async def fake_event(*_args):
            return None

        monkeypatch.setattr(project_views, "admin_event", fake_event)

        captured = {}

        def fake_render(template, request, context):
            captured["template"] = template
            captured["request"] = request
            captured["context"] = context
            return "<button>pause</button>"

        monkeypatch.setattr(project_views.aiohttp_jinja2, "render_string", fake_render)

        raw = _raw(project_views.project_action)
        resp = await raw(req)

        assert captured["template"] == "projects/_source_toggle_button.html"
        assert captured["context"]["s"]["id"] == source.id
        assert captured["context"]["s"]["is_paused"] is True
        assert resp.text == "<button>pause</button>"

    @pytest.mark.asyncio
    async def test_resume_source_htmx_returns_toggle_partial(self, monkeypatch):
        source = _make_source(is_paused=True)
        row = SimpleNamespace(
            id=source.id,
            title=source.title,
            uri=source.uri,
            is_paused=False,
            excluded=0,
            errors=0,
            pending=0,
            processing=0,
            ready=0,
        )
        db = _StatsDB(scalar_values=[source], one_rows=[row])
        req = self._make_action_req("resume_source")
        req["db"] = db
        req.headers["HX-Request"] = "true"

        async def fake_event(*_args):
            return None

        monkeypatch.setattr(project_views, "admin_event", fake_event)

        captured = {}

        def fake_render(template, request, context):
            captured["template"] = template
            captured["request"] = request
            captured["context"] = context
            return "<button>resume</button>"

        monkeypatch.setattr(project_views.aiohttp_jinja2, "render_string", fake_render)

        raw = _raw(project_views.project_action)
        resp = await raw(req)

        assert captured["template"] == "projects/_source_toggle_button.html"
        assert captured["context"]["s"]["id"] == source.id
        assert captured["context"]["s"]["is_paused"] is False
        assert resp.text == "<button>resume</button>"

    @pytest.mark.asyncio
    async def test_pause_nonexistent_source_raises_404(self, monkeypatch):
        db = _StatsDB(scalar_values=[None])
        req = self._make_action_req("pause_source")
        req["db"] = db

        monkeypatch.setattr(project_views, "admin_event", lambda *a: None)

        raw = _raw(project_views.project_action)
        with pytest.raises(web.HTTPNotFound):
            await raw(req)

    @pytest.mark.asyncio
    async def test_resume_nonexistent_source_raises_404(self, monkeypatch):
        db = _StatsDB(scalar_values=[None])
        req = self._make_action_req("resume_source")
        req["db"] = db

        monkeypatch.setattr(project_views, "admin_event", lambda *a: None)

        raw = _raw(project_views.project_action)
        with pytest.raises(web.HTTPNotFound):
            await raw(req)


# ---------------------------------------------------------------------------
# Crawler tasks respect is_paused
# ---------------------------------------------------------------------------


class TestCrawlerSkipsPausedSources:
    def test_crawl_source_task_skips_paused_source(self):
        """crawl_source_task exits early when source.is_paused is True."""
        from jobs.crawler.tasks import crawl_source_task

        source = _make_source(is_paused=True)

        with patch("jobs.crawler.tasks.create_sync_engine") as mock_engine:
            engine = MagicMock()
            session = MagicMock()
            session.__enter__ = lambda s: session
            session.__exit__ = MagicMock(return_value=False)
            session.get.return_value = source
            session.execute.return_value = MagicMock()
            mock_engine.return_value = engine
            engine.__enter__ = lambda s: engine
            engine.__exit__ = MagicMock(return_value=False)

            with patch("jobs.crawler.tasks.Session") as mock_session_cls:
                mock_session_cls.return_value = session

                with patch("subprocess.run") as mock_run:
                    crawl_source_task(source.id)
                    mock_run.assert_not_called()

    def test_crawl_source_task_proceeds_when_not_paused(self):
        """crawl_source_task runs subprocess when source.is_paused is False."""
        from jobs.crawler.tasks import crawl_source_task

        source = _make_source(is_paused=False)

        with patch("jobs.crawler.tasks.create_sync_engine") as mock_engine:
            engine = MagicMock()
            session = MagicMock()
            session.__enter__ = lambda s: session
            session.__exit__ = MagicMock(return_value=False)
            session.get.return_value = source
            session.execute.return_value = MagicMock()
            mock_engine.return_value = engine
            engine.dispose = MagicMock()

            with patch("jobs.crawler.tasks.Session") as mock_session_cls:
                mock_session_cls.return_value = session

                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=1)
                    crawl_source_task(source.id)
                    mock_run.assert_called_once()
