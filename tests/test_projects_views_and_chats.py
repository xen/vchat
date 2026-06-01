from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiohttp import web

from vchat.views.projects import chats as chats_views
from vchat.views.projects import views as project_views


class _Req:
    def __init__(self, **data):
        self._data = {}
        self.query = data.pop("query", {})
        self.match_info = data.pop("match_info", {})
        for key, value in data.items():
            self._data[key] = value

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        self._data[key] = value

    def get(self, key, default=None):
        return self._data.get(key, default)


@pytest.mark.asyncio
async def test_chats_list_returns_active_chats(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Redis:
        async def smembers(self, key):
            _ = key
            return {"chat-1"}

    class _Result:
        def scalars(self):
            class _S:
                def all(self):
                    return [SimpleNamespace(id="chat-1")]

            return _S()

    class _Db:
        async def execute(self, stmt):
            _ = stmt
            return _Result()

    class _Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return _Db()

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return False

    monkeypatch.setattr(chats_views, "redis", _Redis())
    monkeypatch.setattr(chats_views, "async_session_factory", _Factory())
    monkeypatch.setattr(
        chats_views, "_project_context", lambda request: SimpleNamespace(id="global")
    )
    raw = chats_views.chats_list.__wrapped__.__wrapped__.__wrapped__
    payload = await raw(
        _Req(user=SimpleNamespace(id=1), app={"login": None}, path="/chats")
    )
    assert payload["active_chats"]


@pytest.mark.asyncio
async def test_history_list_builds_pagination_and_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    fake_chat = SimpleNamespace(
        id="c1",
        created_at=now,
        title="t",
        user_uid="u",
        meta={},
    )

    class _RowsResult:
        def all(self):
            return [
                SimpleNamespace(
                    Chat=fake_chat, upvotes=1, downvotes=0, guardrail_hits=1
                )
            ]

    class _Db:
        async def scalar(self, stmt):
            _ = stmt
            return 1

        async def execute(self, stmt):
            _ = stmt
            return _RowsResult()

    class _HistoryReq:
        def __init__(self):
            self.query = {
                "page": "1",
                "search": "отпуск",
                "date_from": "2026/01",
                "date_to": "2026/02/01",
                "guardrail": "1",
                "guardrail_reason": "passport_ru",
            }
            self._store = {"db": _Db()}

        def __getitem__(self, item):
            return self._store[item]

        def __setitem__(self, key, value):
            self._store[key] = value

    request = _HistoryReq()
    monkeypatch.setattr(
        chats_views, "_project_context", lambda request: SimpleNamespace(id="global")
    )

    raw = chats_views.history_list.__wrapped__.__wrapped__.__wrapped__
    payload = await raw(request)
    assert payload["pagination"]["total"] == 1
    assert payload["pagination"]["page"] == 1
    assert payload["guardrail_filter"] is True
    assert payload["chats"][0].guardrail_triggered is True


@pytest.mark.asyncio
async def test_history_detail_masks_pii_and_maps_guardrail_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = SimpleNamespace(id="chat-1", title="Demo", meta={})
    msgs = [
        SimpleNamespace(
            role="user",
            text="Мой паспорт 12 34 567890",
            full_context="",
            guardrail_reasons=None,
            guardrail_triggered=False,
            guardrail_stage=None,
        ),
        SimpleNamespace(
            role="assistant",
            text="Ответ",
            full_context="guardrail_blocked_output|passport_ru",
            guardrail_reasons=["passport_ru"],
            guardrail_triggered=True,
            guardrail_stage="output",
        ),
    ]

    class _Scalars:
        def all(self):
            return msgs

    class _Res:
        def scalars(self):
            return _Scalars()

    class _Db:
        async def scalar(self, stmt):
            _ = stmt
            return chat

        async def execute(self, stmt):
            _ = stmt
            return _Res()

    request = _Req(db=_Db(), match_info={"chat_id": "chat-1"})
    monkeypatch.setattr(
        chats_views, "_project_context", lambda request: SimpleNamespace(id="global")
    )

    raw = chats_views.history_detail.__wrapped__.__wrapped__.__wrapped__
    payload = await raw(request)
    assert payload["chat"].id == "chat-1"
    assert isinstance(payload["messages"][0].has_masked_pii, bool)
    assert payload["messages"][1].guardrail_hit is True
    assert payload["messages"][1].guardrail_rules


@pytest.mark.asyncio
async def test_history_detail_404_when_chat_missing() -> None:
    class _Db:
        async def scalar(self, stmt):
            _ = stmt
            return None

    raw = chats_views.history_detail.__wrapped__.__wrapped__.__wrapped__
    with pytest.raises(web.HTTPNotFound):
        await raw(_Req(db=_Db(), match_info={"chat_id": "x"}))


def test_document_pipeline_steps_returns_error_description() -> None:
    from vchat.page_status import PageStatus, PageStatusError

    document = SimpleNamespace(
        status=PageStatus.crawler,
        status_error=PageStatusError.extraction_failed,
        meta={
            "reason": "extraction_failed",
            "error": "boom",
        },
    )

    status, status_error, msg = project_views._document_pipeline_steps(document)

    assert status == PageStatus.crawler
    assert status_error == PageStatusError.extraction_failed
    assert msg is not None
    assert "Ошибка извлечения" in msg
    assert "boom" in msg


def test_document_pipeline_steps_returns_embedder_error_description() -> None:
    from vchat.page_status import PageStatus, PageStatusError

    document = SimpleNamespace(
        status=PageStatus.parsing,
        status_error=PageStatusError.embedder_failed,
        meta={
            "reason": "embedder_failed",
            "message": "Chunk 3 is too large for embedder",
        },
    )

    status, status_error, msg = project_views._document_pipeline_steps(document)

    assert status == PageStatus.parsing
    assert status_error == PageStatusError.embedder_failed
    assert msg is not None
    assert "Ошибка эмбеддера" in msg
    assert "Chunk 3 is too large for embedder" in msg


def test_document_uniqueness_percent_uses_boilerplate_overlap() -> None:
    content = (
        "общий текст меню подвала повторяется\n"
        "общий текст меню подвала повторяется\n"
        "уникальный раздел страницы со смыслом\n"
    )
    document = SimpleNamespace(content=content)
    boilerplate = project_views.compute_trigram_hashes(
        "общий текст меню подвала повторяется"
    )

    uniqueness = project_views._document_uniqueness_percent(document, boilerplate)

    assert uniqueness is not None
    assert 0 < uniqueness < 100


def test_document_stats_summary_includes_requested_metrics() -> None:
    document = SimpleNamespace(content="слово " * 200, _length=0)
    extraction = {"word_count": 200, "table_count": 3}
    chunks = [SimpleNamespace(), SimpleNamespace()]

    summary = project_views._document_stats_summary(document, chunks, extraction, 87)

    assert "чанков" in summary
    assert "слов" in summary
    assert "таблиц" in summary
    assert "87% уникальности текста" in summary


@pytest.mark.asyncio
async def test_document_link_groups_split_mutual_incoming_and_outgoing() -> None:
    document = SimpleNamespace(id=10, uri="https://example.local/current")

    outgoing_rows = [
        (
            SimpleNamespace(target_page_id=21, target_uri="https://example.local/mutual", target_status="ok"),
            SimpleNamespace(id=21, title="Mutual page", uri="https://example.local/mutual"),
        ),
        (
            SimpleNamespace(target_page_id=22, target_uri="https://example.local/outgoing", target_status="not_indexed"),
            SimpleNamespace(id=22, title="Outgoing page", uri="https://example.local/outgoing"),
        ),
    ]
    incoming_rows = [
        (
            SimpleNamespace(source_page_id=21),
            SimpleNamespace(id=21, title="Mutual page", uri="https://example.local/mutual"),
        ),
        (
            SimpleNamespace(source_page_id=23),
            SimpleNamespace(id=23, title="Incoming page", uri="https://example.local/incoming"),
        ),
    ]

    class _Res:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _Db:
        def __init__(self):
            self.calls = 0

        async def execute(self, stmt):
            _ = stmt
            self.calls += 1
            if self.calls == 1:
                return _Res(outgoing_rows)
            return _Res(incoming_rows)

    groups = await project_views._document_link_groups(_Db(), document)

    assert [item["id"] for item in groups["mutual"]] == [21]
    assert [item["id"] for item in groups["incoming"]] == [23]
    assert [item["id"] for item in groups["outgoing"]] == [22]
    assert groups["mutual"][0]["status"] == "ok"
    assert groups["outgoing"][0]["status"] == "not_indexed"


def test_document_links_graph_builds_nodes_and_bidirectional_edges() -> None:
    document = SimpleNamespace(
        id=10,
        uri="https://example.local/current",
        status="ready",
        status_error=None,
    )
    groups = {
        "mutual": [
            {
                "id": 21,
                "title": "Mutual",
                "uri": "https://other.local/mutual",
                "status": "ok",
                "status_error": None,
            }
        ],
        "incoming": [
            {
                "id": 22,
                "title": "Incoming",
                "uri": "https://example.local/incoming",
                "status": "blocked",
                "status_error": "excluded_rules",
            }
        ],
        "outgoing": [
            {
                "id": 23,
                "title": "Outgoing",
                "uri": "https://example.local/outgoing",
                "status": "not_indexed",
                "status_error": None,
            }
        ],
    }

    graph = project_views._document_links_graph(document, "Current", groups)

    assert graph["currentNodeId"] == "page-10"
    assert {node["id"] for node in graph["nodes"]} == {"page-10", "page-21", "page-22", "page-23"}
    node_by_id = {node["id"]: node for node in graph["nodes"]}
    assert node_by_id["page-21"]["is_external"] is True
    assert node_by_id["page-22"]["is_ignored"] is True
    assert node_by_id["page-23"]["is_external"] is False
    assert ("page-21", "page-10", "incoming") in {
        (link["source"], link["target"], link["relation"]) for link in graph["links"]
    }
    assert ("page-10", "page-21", "outgoing") in {
        (link["source"], link["target"], link["relation"]) for link in graph["links"]
    }
