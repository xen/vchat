from __future__ import annotations

import csv
import io
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import jinja2
import pytest
from aiohttp import web

from vchat.app_keys import REDIS_KEY
from vchat.views.projects import views as project_views


def _csv_rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


class _Resp:
    def __init__(self, *, all_rows=None, one_row=None):
        self._all_rows = all_rows or []
        self._one_row = one_row

    def all(self):
        return self._all_rows

    def one(self):
        return self._one_row

    def one_or_none(self):
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
    env.globals["url"] = lambda name, document_id: f"/page/{document_id}"
    template = env.get_template("projects/document_content.html")
    rendered = template.render(
        document=SimpleNamespace(
            id=1,
            title="Doc",
            uri=None,
            status="ready",
            status_error=None,
            meta={},
            content="body",
        ),
        document_display_title="Doc",
        document_content_preview="body",
        document_content_is_truncated=False,
        document_content_preview_chars=500,
        document_content_char_count=4,
        document_pipeline=("ready", None, None),
        document_stats_summary="10 Б, 0 чанков, 0 слов, 0 таблиц",
        document_crawl_summary="код —",
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
        document_crawl_fields=[],
        document_links={"mutual": [], "incoming": [], "outgoing": []},
        document_links_graph={"currentNodeId": "page-1", "nodes": [], "links": []},
    )
    assert "one\ntwo" in rendered


def test_document_content_preview_truncates_at_word_boundary() -> None:
    content = "alpha beta gamm"
    preview, source_chars = project_views._document_content_preview_at_word_boundary(
        content,
        is_truncated=True,
    )

    assert preview == "alpha beta"
    assert source_chars == len("alpha beta")
    assert preview + content[source_chars:] == content


def test_document_content_template_renders_chunk_overlap_without_indent_gaps() -> None:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(
            str(Path(__file__).resolve().parents[1] / "vchat" / "templates")
        )
    )
    env.globals["url"] = lambda name, document_id: f"/page/{document_id}"
    template = env.get_template("projects/document_content.html")
    rendered = template.render(
        document=SimpleNamespace(
            id=1,
            title="Doc",
            uri=None,
            status="ready",
            status_error=None,
            meta={},
            content="body",
        ),
        document_display_title="Doc",
        document_content_preview="body",
        document_content_is_truncated=False,
        document_content_can_expand=False,
        document_content_preview_chars=500,
        document_content_char_count=4,
        document_content_remaining_chars=0,
        document_pipeline=("ready", None, None),
        document_stats_summary="10 Б, 2 чанков, 0 слов, 0 таблиц",
        document_crawl_summary="код —",
        document_structure=[],
        document_outline=[],
        document_extraction={},
        document_chunks=[
            SimpleNamespace(
                chunk_ix=0,
                kind="text",
                section_path="",
                header_text="",
                token_count=1,
                text="first chunk",
                start_offset=0,
                end_offset=10,
                entity_terms=[],
            ),
            SimpleNamespace(
                chunk_ix=1,
                kind="text",
                section_path="",
                header_text="",
                token_count=1,
                text="second chunk",
                start_offset=5,
                end_offset=20,
                entity_terms=[],
            ),
        ],
        document_crawl_fields=[],
        document_links={"mutual": [], "incoming": [], "outgoing": []},
        document_links_graph={"currentNodeId": "page-1", "nodes": [], "links": []},
    )

    assert rendered.index("overlap с предыдущим: 5 символов") < rendered.index(
        "second chunk"
    )
    assert ">overlap с предыдущим: 5 символов</div>second chunk" in rendered
    assert "first chunk<div class=\"mt-1 text-amber-700 text-xs\">" in rendered


def test_document_content_template_renders_expand_button_for_truncated_content() -> None:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(
            str(Path(__file__).resolve().parents[1] / "vchat" / "templates")
        )
    )
    env.globals["url"] = (
        lambda name, document_id: f"/page/{document_id}/content/rest"
    )
    template = env.get_template("projects/document_content.html")
    rendered = template.render(
        document=SimpleNamespace(
            id=7,
            title="Doc",
            uri=None,
            status="ready",
            status_error=None,
            meta={},
            content="body",
        ),
        document_display_title="Doc",
        document_content_preview="preview",
        document_content_is_truncated=True,
        document_content_can_expand=True,
        document_content_preview_chars=500,
        document_content_preview_source_chars=491,
        document_content_char_count=2105,
        document_content_remaining_chars=1614,
        document_pipeline=("ready", None, None),
        document_stats_summary="10 Б, 0 чанков, 0 слов, 0 таблиц",
        document_crawl_summary="код —",
        document_structure=[],
        document_outline=[],
        document_extraction={},
        document_chunks=[],
        document_crawl_fields=[],
        document_links={"mutual": [], "incoming": [], "outgoing": []},
        document_links_graph={"currentNodeId": "page-7", "nodes": [], "links": []},
    )

    assert '<span id="document-content-ellipsis">...</span>' in rendered
    assert '<span id="document-content-rest"></span>' in rendered
    assert 'hx-get="/page/7/content/rest?offset=491"' in rendered
    assert "Показаны: 500 из 2105 символов." in rendered
    assert "первые 500 символов" not in rendered
    assert "[показать остальные 1614 символов]" in rendered


def test_document_content_template_hides_expand_button_for_ignored_content() -> None:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(
            str(Path(__file__).resolve().parents[1] / "vchat" / "templates")
        )
    )
    env.globals["url"] = (
        lambda name, document_id: f"/page/{document_id}/content/rest"
    )
    template = env.get_template("projects/document_content.html")
    rendered = template.render(
        document=SimpleNamespace(
            id=7,
            title="Doc",
            uri=None,
            status="ready",
            status_error="excluded_ignored",
            meta={},
            content="body",
        ),
        document_display_title="Doc",
        document_content_preview="preview",
        document_content_is_truncated=True,
        document_content_can_expand=False,
        document_content_preview_chars=500,
        document_content_preview_source_chars=493,
        document_content_char_count=2105,
        document_content_remaining_chars=1612,
        document_pipeline=("ready", "excluded_ignored", "Исключено вручную"),
        document_stats_summary="10 Б, 0 чанков, 0 слов, 0 таблиц",
        document_crawl_summary="код —",
        document_structure=[],
        document_outline=[],
        document_extraction={},
        document_chunks=[],
        document_crawl_fields=[],
        document_links={"mutual": [], "incoming": [], "outgoing": []},
        document_links_graph={"currentNodeId": "page-7", "nodes": [], "links": []},
    )

    assert "Показаны: 500 из 2105 символов." in rendered
    assert "первые 500 символов" not in rendered
    assert "показать остальные" not in rendered
    assert '<span id="document-content-ellipsis">...</span>' in rendered
    assert "document-content-rest" not in rendered


def test_document_content_template_renders_compact_summary() -> None:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(
            str(Path(__file__).resolve().parents[1] / "vchat" / "templates")
        )
    )
    env.globals["url"] = lambda name, document_id: f"/page/{document_id}"
    template = env.get_template("projects/document_content.html")
    rendered = template.render(
        document=SimpleNamespace(
            id=1,
            title="Doc",
            uri="https://example.local/page",
            status="ready",
            status_error=None,
            meta={},
            content="body",
        ),
        document_display_title="Doc",
        document_content_preview="body",
        document_content_is_truncated=False,
        document_content_preview_chars=500,
        document_content_char_count=4,
        document_pipeline=("ready", None, None),
        document_stats_summary="12.4 КБ, 3 чанков, 120 слов, 1 таблиц, 88% уникальности текста",
        document_crawl_summary="код 200, обход 01.06.2026 10:00, etag abc123",
        document_structure=[],
        document_outline=[],
        document_extraction={},
        document_chunks=[],
        document_crawl_fields=[
            {"label": "HTTP status", "value": "200"},
            {"label": "Hub-страница", "value": "Нет"},
            {"label": "ETag", "value": "abc123"},
        ],
        document_links={"mutual": [], "incoming": [], "outgoing": []},
        document_links_graph={"currentNodeId": "page-1", "nodes": [], "links": []},
    )
    assert "Статистика:" in rendered
    assert "12.4 КБ, 3 чанков, 120 слов, 1 таблиц, 88% уникальности текста" in rendered
    assert "Обход:" in rendered
    assert "код 200, обход 01.06.2026 10:00, etag abc123" in rendered
    assert "Данные обходов" not in rendered


def test_document_content_template_renders_document_links_widget() -> None:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(
            str(Path(__file__).resolve().parents[1] / "vchat" / "templates")
        )
    )
    env.globals["url"] = lambda name, document_id: f"/page/{document_id}"
    template = env.get_template("projects/document_content.html")
    rendered = template.render(
        document=SimpleNamespace(
            title="Doc",
            uri="https://example.local/page",
            status="ready",
            status_error=None,
            meta={},
            content="body",
        ),
        document_display_title="Doc",
        document_content_preview="body",
        document_content_is_truncated=False,
        document_content_preview_chars=500,
        document_content_char_count=4,
        document_pipeline=("ready", None, None),
        document_stats_summary="1.0 КБ, 0 чанков, 0 слов, 0 таблиц, 100% уникальности текста",
        document_crawl_summary="код 200, обход —",
        document_structure=[],
        document_outline=[],
        document_extraction={},
        document_chunks=[],
        document_crawl_fields=[],
        document_links={
            "mutual": [
                {
                    "id": 11,
                    "title": "Mutual page",
                    "uri": "https://example.local/mutual",
                    "status": "ok",
                }
            ],
            "incoming": [
                {
                    "id": 12,
                    "title": "Incoming page",
                    "uri": "https://example.local/incoming",
                }
            ],
            "outgoing": [
                {
                    "id": 13,
                    "title": "Outgoing page",
                    "uri": "https://example.local/outgoing",
                    "status": "not_indexed",
                }
            ],
        },
        document_links_graph={
            "currentNodeId": "page-10",
            "nodes": [
                {"id": "page-10", "title": "Doc", "uri": "https://example.local/page", "relation": "current", "detail_url": "/page/10"},
                {"id": "page-11", "title": "Mutual page", "uri": "https://example.local/mutual", "relation": "mutual", "detail_url": "/page/11"},
            ],
            "links": [{"source": "page-10", "target": "page-11", "relation": "outgoing"}],
        },
    )
    assert "Связанные страницы" in rendered
    assert "document-links-graph" in rendered
    assert "document-links-graph-data" in rendered
    assert "Выбранная страница" in rendered
    assert "Игнорируемые" in rendered
    assert "Другой домен" in rendered
    assert "Взаимные ссылки" not in rendered


def test_sources_template_hides_pause_badge_in_name_column() -> None:
    template_path = Path(__file__).resolve().parents[1] / "vchat" / "templates" / "projects" / "sources.html"
    content = template_path.read_text(encoding="utf-8")

    assert "Пауза" not in content
    assert "pause-circle" not in content


def test_files_template_uses_common_toolbar_controls() -> None:
    template_path = Path(__file__).resolve().parents[1] / "vchat" / "templates" / "projects" / "files.html"
    content = template_path.read_text(encoding="utf-8")

    assert "Добавить файл" in content
    assert "Все авторы" in content
    assert "x-data=\"useProjectFilesTable()\"" in content
    assert "К списку" not in content


def test_document_detail_template_uses_document_title_in_header() -> None:
    template_path = Path(__file__).resolve().parents[1] / "vchat" / "templates" / "projects" / "document_detail.html"
    content = template_path.read_text(encoding="utf-8")

    assert "Структура документа" not in content
    assert "{{ document_display_title }}" in content
    assert "Страницы" in content
    assert "btn btn-ghost btn-sm" not in content


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
            _Resp(
                all_rows=[
                    SimpleNamespace(
                        source_id=1,
                        reason="csv_statistical_dump",
                        doc_count=2,
                    ),
                    SimpleNamespace(
                        source_id=None,
                        reason="large_downloadable_document",
                        doc_count=1,
                    ),
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
    assert payload["total_metadata_only_docs"] == 3
    assert payload["source_stats"]
    assert payload["source_stats"][0]["metadata_only_count"] == 2
    assert payload["source_stats"][0]["metadata_policy_reasons"] == [
        {"reason": "csv_statistical_dump", "doc_count": 2}
    ]
    assert payload["source_stats"][1]["metadata_only_count"] == 1
    assert payload["source_stats"][1]["metadata_policy_reasons"] == [
        {"reason": "large_downloadable_document", "doc_count": 1}
    ]


@pytest.mark.asyncio
async def test_project_document_content_rest_rejects_non_integer_offset() -> None:
    req = _Req(
        match_info={"document_id": "1"},
        rel_url=SimpleNamespace(query={"offset": "abc"}),
        db=_DB(),
    )
    raw = project_views.project_document_content_rest.__wrapped__.__wrapped__

    with pytest.raises(web.HTTPBadRequest) as exc:
        await raw(req)

    assert exc.value.text == "offset must be an integer"


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
                            blocked_reason=None,
                            blocked_message=None,
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
                            blocked_reason=None,
                            blocked_message=None,
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

    req = _Req(db=db, app={REDIS_KEY: _Redis()})
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
async def test_project_documents_csv_and_files_json(
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
    docs_fn = project_views.project_documents_csv.__wrapped__
    docs_resp = await docs_fn(req_docs)
    assert docs_resp.status == 200
    assert docs_resp.content_type == "text/csv"
    rows = _csv_rows(docs_resp.text)
    assert rows[0]["uri"] == "https://example.local/a"
    assert rows[0]["status"] == "ready"
    assert rows[0]["is_ignored"] == "0"
    assert "meta" not in rows[0]
    assert "created_at" not in rows[0]
    assert "updated_at" not in rows[0]
    assert "document_type" not in rows[0]

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
