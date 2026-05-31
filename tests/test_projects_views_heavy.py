from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import jinja2
import pytest

from vchat.views.projects import views as project_views


class _Resp:
    def __init__(self, *, all_rows=None, one_row=None):
        self._all_rows = all_rows or []
        self._one_row = one_row

    def all(self):
        return self._all_rows

    def one(self):
        return self._one_row

    def scalar(self):
        return self._one_row


class _DB:
    def __init__(self, *, execute_results=None, scalar_results=None):
        self.execute_results = deque(execute_results or [])
        self.scalar_results = deque(scalar_results or [])
        self.added = []
        self.commits = 0
        self.flushed = 0

    async def execute(self, stmt):
        _ = stmt
        return self.execute_results.popleft()

    async def scalar(self, stmt):
        _ = stmt
        return self.scalar_results.popleft()

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        self.flushed += 1
        if self.added:
            self.added[-1].id = 321

    async def commit(self):
        self.commits += 1


class _Req(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def test_document_content_template_renders_structure_items() -> None:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(
            str(Path(__file__).resolve().parents[1] / "vchat" / "templates")
        )
    )
    template = env.get_template("projects/document_content.html")
    rendered = template.render(
        document=SimpleNamespace(
            title="Doc",
            uri=None,
            status="ready",
            status_error=None,
            meta={},
            content="body",
        ),
        document_pipeline=("ready", None, None),
        document_structure=[
            {
                "type": "list",
                "level": None,
                "ordered": False,
                "section_path": "",
                "items": ["one", "two"],
                "content": "",
            }
        ],
        document_outline=[],
        document_extraction={},
        document_chunks=[],
    )
    assert "one\ntwo" in rendered


def test_sources_template_hides_pause_badge_in_name_column() -> None:
    template_path = Path(__file__).resolve().parents[1] / "vchat" / "templates" / "projects" / "sources.html"
    content = template_path.read_text(encoding="utf-8")

    assert "Пауза" not in content
    assert "pause-circle" not in content


@pytest.mark.asyncio
async def test_project_stats_aggregates(monkeypatch: pytest.MonkeyPatch) -> None:
    day = datetime(2026, 3, 1, tzinfo=timezone.utc)

    db = _DB(
        execute_results=[
            _Resp(all_rows=[SimpleNamespace(day=day, count=2, users=1)]),
            _Resp(all_rows=[SimpleNamespace(day=day, count=4, hits=3, tokens=100)]),
            _Resp(all_rows=[SimpleNamespace(day=day, likes=5, dislikes=2)]),
            _Resp(
                all_rows=[
                    SimpleNamespace(provider="openai", model="gpt-4o-mini", tokens=100)
                ]
            ),
            _Resp(
                all_rows=[
                    SimpleNamespace(
                        id=1, type="site", title="Main", doc_count=7, data_volume=70
                    )
                ]
            ),
            _Resp(all_rows=[SimpleNamespace(id=1, chunk_count=9, chunk_storage=90)]),
            _Resp(one_row=SimpleNamespace(doc_count=1, data_volume=10)),
            _Resp(one_row=SimpleNamespace(chunk_count=2, chunk_storage=20)),
        ],
        scalar_results=[3, 4],
    )

    req = _Req(db=db, app={})
    monkeypatch.setattr(
        project_views, "_project_context", lambda request: SimpleNamespace(id="global")
    )

    raw = project_views.project_stats.__wrapped__.__wrapped__.__wrapped__
    payload = await raw(req)

    assert payload["total_users"] == 3
    assert payload["pending_embeddings"] == 4
    assert payload["total_docs"] == 8
    assert payload["total_chunks"] == 11
    assert payload["total_tokens"] >= 100
    assert payload["source_stats"]


@pytest.mark.asyncio
async def test_project_edit_sources_keeps_active_sources_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _DB(
        execute_results=[
            _Resp(
                all_rows=[
                    SimpleNamespace(
                        id=2,
                        title="Active source",
                        uri="https://active.local",
                        is_paused=False,
                        excluded=0,
                        errors=0,
                        pending=0,
                        processing=0,
                        ready=3,
                    ),
                    SimpleNamespace(
                        id=1,
                        title="Paused source",
                        uri="https://paused.local",
                        is_paused=True,
                        excluded=0,
                        errors=0,
                        pending=0,
                        processing=0,
                        ready=1,
                    ),
                ]
            ),
            _Resp(
                one_row=SimpleNamespace(
                    excluded=0,
                    errors=0,
                    pending=0,
                    processing=0,
                    ready=4,
                )
            ),
            _Resp(one_row=2.0),
        ]
    )

    class _Redis:
        async def llen(self, _key):
            return 0

        async def lrange(self, _key, _start, _end):
            return []

    req = _Req(db=db, app={project_views.REDIS_KEY: _Redis()})
    monkeypatch.setattr(
        project_views, "_project_context", lambda request: SimpleNamespace(id="global")
    )

    async def _get_session(_request):
        return {}

    monkeypatch.setattr(project_views, "get_session", _get_session)
    monkeypatch.setattr(
        project_views.forms, "SourceForm", lambda *args, **kwargs: SimpleNamespace()
    )

    raw = project_views.project_edit_sources.__wrapped__.__wrapped__.__wrapped__
    payload = await raw(req)

    assert [source["title"] for source in payload["sources"]] == [
        "Active source",
        "Paused source",
    ]
    assert payload["sources"][0]["is_paused"] is False
    assert payload["sources"][1]["is_paused"] is True


@pytest.mark.asyncio
async def test_project_documents_and_files_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vchat.page_status import PageStatus

    now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    source = SimpleNamespace(title="S", uri="https://example.local")

    db_docs = _DB(
        execute_results=[
            _Resp(
                all_rows=[
                    (
                        1,
                        "Doc",
                        "https://example.local/a",
                        PageStatus.ready,
                        None,
                        source.title,
                        source.uri,
                        120,
                        2,
                    )
                ]
            ),
        ]
    )
    req_docs = _Req(db=db_docs)
    docs_fn = project_views.project_documents_json.__wrapped__
    docs_resp = await docs_fn(req_docs)
    assert docs_resp.status == 200
    assert b'"meta"' not in docs_resp.body
    assert b'"uri": "https://example.local/a"' in docs_resp.body
    assert b'"created_at"' not in docs_resp.body
    assert b'"updated_at"' not in docs_resp.body
    assert b'"document_type"' not in docs_resp.body

    file_doc = SimpleNamespace(
        id=5,
        title="",
        created_at=now,
        meta={"filename": "manual.pdf"},
    )
    db_files = _DB(
        execute_results=[
            _Resp(all_rows=[(file_doc, 512, 4)]),
        ]
    )
    req_files = _Req(db=db_files)
    files_fn = project_views.project_files_json.__wrapped__
    files_resp = await files_fn(req_files)
    assert files_resp.status == 200
    assert b"manual.pdf" in files_resp.body
