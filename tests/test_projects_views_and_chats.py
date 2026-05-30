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
