from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import jinja2
import pytest
from aiohttp import web

from vchat.views.projects import views as project_views


class _Resp:
    def __init__(self, *, all_rows=None, one_row=None):
        self._all_rows = all_rows or []
        self._one_row = one_row

    def all(self):
        return self._all_rows

    def one(self):
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
            status="indexed",
            meta={},
            content="body",
        ),
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
async def test_project_documents_and_files_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    doc = SimpleNamespace(
        id=1,
        title="Doc",
        uri="https://example.local/a",
        created_at=now,
        updated_at=now,
        status="indexed",
        is_ignored=False,
        meta={"doc_type": "html"},
    )
    source = SimpleNamespace(title="S", uri="https://example.local")

    db_docs = _DB(
        execute_results=[
            _Resp(
                all_rows=[
                    (
                        doc.id,
                        doc.title,
                        doc.uri,
                        doc.created_at,
                        doc.updated_at,
                        doc.status,
                        doc.is_ignored,
                        source.title,
                        source.uri,
                        120,
                        2,
                        "html",
                    )
                ]
            ),
        ]
    )
    req_docs = _Req(db=db_docs)
    docs_fn = project_views.project_documents_json.__wrapped__
    docs_resp = await docs_fn(req_docs)
    assert docs_resp.status == 200
    assert b'"document_type": "html"' in docs_resp.body
    assert b'"meta"' not in docs_resp.body
    assert b'"uri"' not in docs_resp.body

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


@pytest.mark.asyncio
async def test_secure_download_and_on_upload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    existing = tmp_path / "f.txt"
    existing.write_text("ok", encoding="utf-8")

    db = _DB(scalar_results=[SimpleNamespace(uri=str(existing))])
    req = _Req(db=db, match_info={"file_id": "10"})

    secure_fn = project_views.secure_download.__wrapped__
    resp = await secure_fn(req)
    assert isinstance(resp, web.FileResponse)

    src_file = tmp_path / "upload.tmp"
    src_file.write_text("u", encoding="utf-8")

    class _Resource:
        metadata_header = "filename dGVzdC50eHQ=,filetype dGV4dC9wbGFpbg=="
        file_name = "test.txt"

    class _DelayTask:
        def __init__(self):
            self.calls = []

        def delay(self, value):
            self.calls.append(value)

    delay_task = _DelayTask()
    monkeypatch.setattr(project_views, "crawl_file_task", delay_task)

    events = []

    async def _admin_event(name, request):
        events.append((name, request))

    monkeypatch.setattr(project_views, "admin_event", _admin_event)

    upload_req = _Req(
        db=db, user=SimpleNamespace(id=7, name="Uploader", email="u@example.com")
    )
    await project_views.on_upload(upload_req, _Resource(), src_file)

    assert db.flushed == 1
    assert db.commits >= 1
    assert events and events[0][0] == "file_upload"
    assert delay_task.calls == [321]
