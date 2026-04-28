from __future__ import annotations

import os
from collections import deque, namedtuple
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from itsdangerous import URLSafeSerializer

from vchat.ai_providers import resolve_ai_settings
from vchat.guardrails import GuardrailDecision
from vchat.views.chat import views as chat_views


RUN_LIVE = os.getenv("RUN_LIVE_LLM_TESTS", "").strip() == "1"
OPENAI_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
LIVE_MODEL = (os.getenv("LIVE_LLM_MODEL") or "gpt-4o-mini").strip()


pytestmark = pytest.mark.skipif(
    not RUN_LIVE or not OPENAI_KEY,
    reason="Live LLM tests are disabled. Set RUN_LIVE_LLM_TESTS=1 and OPENAI_API_KEY.",
)


@dataclass
class _WsMessage:
    type: aiohttp.WSMsgType
    data: Any


class _FakeResult:
    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value


def _extract_insert_values(stmt: Any) -> dict[str, Any]:
    values: dict[str, Any] = {}
    raw_values = getattr(stmt, "_values", {}) or {}
    for column, bind in raw_values.items():
        key = getattr(column, "key", str(column))
        values[key] = getattr(bind, "value", bind)
    return values


class _FakeSession:
    def __init__(self, state: dict[str, Any]) -> None:
        self._state = state

    async def scalar(self, stmt: Any) -> Any:
        _ = stmt
        return self._state["chat_exists"]

    async def execute(self, stmt: Any) -> _FakeResult:
        if getattr(stmt, "is_insert", False):
            table = getattr(getattr(stmt, "table", None), "name", "")
            values = _extract_insert_values(stmt)
            if table == "chat_msg":
                self._state["chat_msg_inserts"].append(values)
            self._state["next_insert_id"] += 1
            return _FakeResult(self._state["next_insert_id"])
        return _FakeResult(0)

    async def commit(self) -> None:
        self._state["commits"] += 1


class _FakeSessionFactory:
    def __init__(self, state: dict[str, Any]) -> None:
        self._state = state

    def __call__(self) -> _FakeSessionFactory:
        return self

    async def __aenter__(self) -> _FakeSession:
        return _FakeSession(self._state)

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        _ = exc_type, exc, tb
        return False


class _FakeRedis:
    async def publish(self, channel: str, payload: str) -> None:
        _ = channel, payload

    async def sadd(self, key: str, value: str) -> None:
        _ = key, value

    async def srem(self, key: str, value: str) -> None:
        _ = key, value


def _make_request() -> Any:
    payload = URLSafeSerializer(chat_views.SECRET_KEY).dumps(
        [1, "chat-1"], salt="vchat"
    )
    return type(
        "Request",
        (),
        {
            "match_info": {"payload": payload},
            "app": {},
        },
    )()


def _patch_websocket(
    monkeypatch: pytest.MonkeyPatch, incoming: list[_WsMessage]
) -> dict[str, Any]:
    holder: dict[str, Any] = {}

    class _FakeWebSocketResponse:
        def __init__(self) -> None:
            self._incoming = deque(incoming)
            self.sent_json: list[dict[str, Any]] = []
            holder["ws"] = self

        async def prepare(self, request: Any) -> _FakeWebSocketResponse:
            _ = request
            return self

        async def receive(self) -> _WsMessage:
            if self._incoming:
                return self._incoming.popleft()
            return _WsMessage(type=aiohttp.WSMsgType.ERROR, data=None)

        async def send_json(self, payload: dict[str, Any]) -> None:
            self.sent_json.append(payload)

        async def send_str(self, payload: str) -> None:
            _ = payload

        async def close(self, code: int | None = None) -> None:
            _ = code

        def exception(self) -> None:
            return None

    monkeypatch.setattr(chat_views.web, "WebSocketResponse", _FakeWebSocketResponse)
    return holder


def _live_generation_context() -> chat_views.GenerationContext:
    provider, model = resolve_ai_settings("openai", LIVE_MODEL)
    return chat_views.GenerationContext(
        provider=provider,
        model=model,
        system_prompt=chat_views.SYSTEM_PROMPT,
        topics=["кадровая политика", "отпуск"],
        intents=["поиск правил", "уточнение по документу"],
    )


@pytest.mark.asyncio
async def test_live_recommendations_are_relevant_to_document_context() -> None:
    ctx = _live_generation_context()
    messages = [
        {
            "role": "user",
            "content": "Где в Employee Handbook описан перенос отпуска?",
        },
        {
            "role": "assistant",
            "content": (
                "Правила описаны в документе Employee Handbook: Paid Time Off, "
                "раздел 'Перенос отпуска'."
            ),
        },
    ]

    suggestions = await chat_views.generate_suggestions(messages, ctx)

    assert len(suggestions) >= 2
    assert len({s.strip().lower() for s in suggestions}) == len(suggestions)
    assert all(len(s.strip()) >= 10 for s in suggestions)

    keywords = ("отпуск", "перенос", "handbook", "pto")
    assert any(
        any(word in suggestion.lower() for word in keywords)
        for suggestion in suggestions
    )


@pytest.mark.asyncio
async def test_live_websocket_user_mentions_document_gets_recommendations_and_source_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {
        "chat_exists": "chat-1",
        "next_insert_id": 0,
        "chat_msg_inserts": [],
        "commits": 0,
    }
    holder = _patch_websocket(
        monkeypatch,
        [
            _WsMessage(
                type=aiohttp.WSMsgType.TEXT,
                data="Покажи из handbook документ про перенос отпуска и дай ссылку",
            ),
            _WsMessage(type=aiohttp.WSMsgType.ERROR, data=None),
        ],
    )

    monkeypatch.setattr(chat_views, "async_session_factory", _FakeSessionFactory(state))
    monkeypatch.setattr(chat_views, "redis", _FakeRedis())
    monkeypatch.setattr(
        chat_views, "build_generation_context", lambda app: _live_generation_context()
    )

    async def _input_ok(*, text: str, provider: Any) -> GuardrailDecision:
        _ = text, provider
        return GuardrailDecision(allowed=True)

    async def _output_ok(*, text: str, provider: Any) -> GuardrailDecision:
        _ = text, provider
        return GuardrailDecision(allowed=True)

    monkeypatch.setattr(chat_views, "check_input_guardrails", _input_ok)
    monkeypatch.setattr(chat_views, "check_output_guardrails", _output_ok)

    _HistoryMessage = namedtuple("_HistoryMessage", ["role", "content"])

    async def _context(
        *,
        db: Any,
        chat_id: str,
        prompt: str,
        provider: Any,
        model: Any,
        vector_top_k: int,
        ft_top_m: int,
    ):
        _ = db, chat_id, prompt, provider, model, vector_top_k, ft_top_m
        return SimpleNamespace(
            messages=[_HistoryMessage("user", "Где описан перенос отпуска?")],
            used_chunks=[
                {
                    "uri": "https://docs.example.local/employee-handbook/pto-transfer",
                    "title": "Employee Handbook: Перенос отпуска",
                    "chunk_id": 7,
                }
            ],
            sources=[
                {
                    "uri": "https://docs.example.local/employee-handbook/pto-transfer",
                    "title": "Employee Handbook: Перенос отпуска",
                }
            ],
            policy={},
            coverage={},
        )

    async def _run_task_noop(*args, **kwargs) -> None:
        _ = args, kwargs

    monkeypatch.setattr(chat_views, "get_context", _context)
    monkeypatch.setattr(chat_views, "run_task", _run_task_noop)

    request = _make_request()
    await chat_views.websocket(request)
    ws = holder["ws"]

    suggestions_payload = next(
        item for item in ws.sent_json if item.get("type") == "suggested_actions"
    )
    assert suggestions_payload["actions"]
    keywords = ("отпуск", "перенос", "handbook", "pto")
    assert any(
        any(word in action.lower() for word in keywords)
        for action in suggestions_payload["actions"]
    )

    final_payload = next(
        item
        for item in ws.sent_json
        if item.get("partial") is False and "sources" in item
    )
    assert final_payload["sources"] == [
        {
            "uri": "https://docs.example.local/employee-handbook/pto-transfer",
            "title": "Employee Handbook: Перенос отпуска",
        }
    ]
