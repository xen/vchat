from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from collections import namedtuple
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from itsdangerous import URLSafeSerializer

from vchat.settings import cfg
from vchat.views.chat.guardrails import GuardrailDecision
from vchat.views.chat import views as chat_views


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


def _assert_user_message_has_embedding(state: dict[str, Any]) -> None:
    user_insert = next(
        item for item in state["chat_msg_inserts"] if item["role"] == "user"
    )
    assert user_insert["embedding"] == [0.1, 0.2]
    assert user_insert["text_hash"]


class _FakeSession:
    def __init__(self, state: dict[str, Any]) -> None:
        self._state = state

    async def scalar(self, stmt: Any) -> Any:
        _ = stmt
        return self._state["chat_exists"]

    async def execute(self, stmt: Any) -> _FakeResult:
        self._state["executed"].append(stmt)
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
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []
        self.active: set[str] = set()

    async def publish(self, channel: str, payload: str) -> None:
        self.published.append((channel, payload))

    async def sadd(self, key: str, value: str) -> None:
        if key == "active_chats":
            self.active.add(value)

    async def srem(self, key: str, value: str) -> None:
        if key == "active_chats":
            self.active.discard(value)


@dataclass
class _FakeProvider:
    id: str = "openai"

    def request_meta(self) -> dict[str, Any]:
        return {}


@dataclass
class _FakeModel:
    id: str = "gpt-4o-mini"


@dataclass
class _FakeContext:
    provider: _FakeProvider
    model: _FakeModel

    @property
    def provider_id(self) -> str:
        return self.provider.id

    @property
    def model_id(self) -> str:
        return self.model.id


def _make_request() -> Any:
    payload = URLSafeSerializer(cfg.secret_key).dumps(
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
            self.sent_str: list[str] = []
            self.closed = False
            self.close_code = None
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
            self.sent_str.append(payload)

        async def close(self, code: int | None = None) -> None:
            self.closed = True
            self.close_code = code

        def exception(self) -> None:
            return None

    monkeypatch.setattr(chat_views.web, "WebSocketResponse", _FakeWebSocketResponse)
    return holder


def _setup_common(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], _FakeRedis, list[dict[str, Any]]]:
    state = {
        "chat_exists": "chat-1",
        "next_insert_id": 0,
        "executed": [],
        "chat_msg_inserts": [],
        "commits": 0,
    }
    redis = _FakeRedis()
    metrics_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(chat_views, "async_session_factory", _FakeSessionFactory(state))
    monkeypatch.setattr(chat_views, "redis", redis)

    async def _fake_generate_suggestions(*args, **kwargs) -> list[str]:
        _ = args, kwargs
        return []

    monkeypatch.setattr(chat_views, "generate_suggestions", _fake_generate_suggestions)

    async def _fake_enrich_source_payloads(_db: Any, sources: list[dict[str, Any]]):
        return sources

    monkeypatch.setattr(chat_views, "enrich_source_payloads", _fake_enrich_source_payloads)

    async def _embed_query_async(_text: str) -> list[float]:
        return [0.1, 0.2]

    monkeypatch.setattr(chat_views, "embed_query_async", _embed_query_async)
    monkeypatch.setattr(
        chat_views,
        "build_generation_context",
        lambda widget=None: _FakeContext(_FakeProvider(), _FakeModel()),
    )

    async def _fake_metrics(**kwargs) -> None:
        metrics_calls.append(kwargs)

    def _record_chat_request(**kwargs) -> None:
        metrics_calls.append(kwargs)

    monkeypatch.setattr(chat_views, "record_chat_request", _record_chat_request)
    return state, redis, metrics_calls


@pytest.mark.asyncio
async def test_websocket_blocks_input_by_guardrail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _redis, metrics_calls = _setup_common(monkeypatch)
    holder = _patch_websocket(
        monkeypatch,
        [
            _WsMessage(type=aiohttp.WSMsgType.TEXT, data="Паспорт 1234 567890"),
            _WsMessage(type=aiohttp.WSMsgType.ERROR, data=None),
        ],
    )

    async def _input_block(*, text: str, provider: Any) -> GuardrailDecision:
        _ = text, provider
        return GuardrailDecision(
            allowed=False,
            reasons={"passport_ru", "input_blocked"},
            message="blocked",
        )

    async def _output_ok(*, text: str, provider: Any) -> GuardrailDecision:
        _ = text, provider
        return GuardrailDecision(allowed=True)

    monkeypatch.setattr(chat_views, "check_input_guardrails", _input_block)
    monkeypatch.setattr(chat_views, "check_output_guardrails", _output_ok)

    request = _make_request()
    await chat_views.websocket(request)
    ws = holder["ws"]

    assert ws.sent_json
    assert ws.sent_json[0]["guardrail"] is True
    assert ws.sent_json[0]["partial"] is False
    assert ws.sent_json[0]["content"] == chat_views.GUARDRAIL_USER_MESSAGE

    assistant_insert = state["chat_msg_inserts"][-1]
    assert assistant_insert["guardrail_triggered"] is True
    assert assistant_insert["guardrail_stage"] == "input"
    assert set(assistant_insert["guardrail_reasons"]) == {
        "passport_ru",
        "input_blocked",
    }
    _assert_user_message_has_embedding(state)

    assert metrics_calls
    assert metrics_calls[-1]["status"] == "guardrail_blocked_input"


@pytest.mark.asyncio
async def test_websocket_blocks_output_by_guardrail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _redis, metrics_calls = _setup_common(monkeypatch)
    holder = _patch_websocket(
        monkeypatch,
        [
            _WsMessage(type=aiohttp.WSMsgType.TEXT, data="привет"),
            _WsMessage(type=aiohttp.WSMsgType.ERROR, data=None),
        ],
    )

    async def _input_ok(*, text: str, provider: Any) -> GuardrailDecision:
        _ = text, provider
        return GuardrailDecision(allowed=True)

    async def _output_block(*, text: str, provider: Any) -> GuardrailDecision:
        _ = text, provider
        return GuardrailDecision(
            allowed=False,
            reasons={"output_blocked", "phone_number_ru"},
            message="blocked",
        )

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
            messages=[],
            used_chunks=[],
            sources=[],
            policy={},
            coverage={},
            query_embedding=[0.1, 0.2],
        )

    async def _stream(messages: list[dict[str, Any]], ctx: Any):
        _ = messages, ctx
        yield {"event": "content", "data": "Ответ с номером +7 999 123 45 67"}
        yield {
            "event": "assistant_message",
            "message": {
                "role": "assistant",
                "content": "Ответ с номером +7 999 123 45 67",
            },
        }

    monkeypatch.setattr(chat_views, "check_input_guardrails", _input_ok)
    monkeypatch.setattr(chat_views, "check_output_guardrails", _output_block)
    monkeypatch.setattr(chat_views, "get_context", _context)
    monkeypatch.setattr(chat_views, "ai_chat_stream", _stream)

    request = _make_request()
    await chat_views.websocket(request)
    ws = holder["ws"]

    assert any(item.get("partial") is True for item in ws.sent_json)
    assert any(item.get("guardrail") is True for item in ws.sent_json)

    assistant_insert = state["chat_msg_inserts"][-1]
    assert assistant_insert["guardrail_triggered"] is True
    assert assistant_insert["guardrail_stage"] == "output"
    assert set(assistant_insert["guardrail_reasons"]) == {
        "output_blocked",
        "phone_number_ru",
    }
    _assert_user_message_has_embedding(state)

    assert metrics_calls
    assert metrics_calls[-1]["status"] == "guardrail_blocked_output"


@pytest.mark.asyncio
async def test_websocket_no_guardrail_for_regular_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _redis, metrics_calls = _setup_common(monkeypatch)
    holder = _patch_websocket(
        monkeypatch,
        [
            _WsMessage(type=aiohttp.WSMsgType.TEXT, data="привет"),
            _WsMessage(type=aiohttp.WSMsgType.ERROR, data=None),
        ],
    )

    async def _input_ok(*, text: str, provider: Any) -> GuardrailDecision:
        _ = text, provider
        return GuardrailDecision(allowed=True)

    async def _output_ok(*, text: str, provider: Any) -> GuardrailDecision:
        _ = text, provider
        return GuardrailDecision(allowed=True)

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
            messages=[],
            used_chunks=[],
            sources=[],
            policy={},
            coverage={},
            query_embedding=[0.1, 0.2],
        )

    async def _stream(messages: list[dict[str, Any]], ctx: Any):
        _ = messages, ctx
        yield {"event": "content", "data": "Привет! Чем помочь?"}
        yield {"event": "usage", "usage": {"total_tokens": 12}}
        yield {
            "event": "assistant_message",
            "message": {"role": "assistant", "content": "Привет! Чем помочь?"},
        }

    monkeypatch.setattr(chat_views, "check_input_guardrails", _input_ok)
    monkeypatch.setattr(chat_views, "check_output_guardrails", _output_ok)
    monkeypatch.setattr(chat_views, "get_context", _context)
    monkeypatch.setattr(chat_views, "ai_chat_stream", _stream)

    request = _make_request()
    await chat_views.websocket(request)
    ws = holder["ws"]

    assert any(item.get("partial") is True for item in ws.sent_json)
    assert any(
        item.get("partial") is False and item.get("guardrail") is not True
        for item in ws.sent_json
    )
    assert all(item.get("guardrail") is not True for item in ws.sent_json)

    assistant_insert = state["chat_msg_inserts"][-1]
    assert assistant_insert["guardrail_triggered"] is False
    assert assistant_insert["guardrail_stage"] is None
    assert assistant_insert["guardrail_reasons"] is None
    _assert_user_message_has_embedding(state)

    assert metrics_calls
    assert metrics_calls[-1]["status"] == "ok"


@pytest.mark.asyncio
async def test_websocket_returns_document_recommendations_and_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _redis, metrics_calls = _setup_common(monkeypatch)
    holder = _patch_websocket(
        monkeypatch,
        [
            _WsMessage(
                type=aiohttp.WSMsgType.TEXT,
                data="Покажи раздел про отпуск из employee-handbook",
            ),
            _WsMessage(type=aiohttp.WSMsgType.ERROR, data=None),
        ],
    )

    async def _input_ok(*, text: str, provider: Any) -> GuardrailDecision:
        _ = text, provider
        return GuardrailDecision(allowed=True)

    async def _output_ok(*, text: str, provider: Any) -> GuardrailDecision:
        _ = text, provider
        return GuardrailDecision(allowed=True)

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
        _ = db, provider, model, vector_top_k, ft_top_m
        assert chat_id == "chat-1"
        assert "employee-handbook" in prompt
        return SimpleNamespace(
            messages=[_HistoryMessage("user", prompt)],
            used_chunks=[
                {
                    "uri": "https://docs.example.local/employee-handbook/pto",
                    "title": "Employee Handbook: Paid Time Off",
                    "chunk_id": 101,
                }
            ],
            sources=[
                {
                    "uri": "https://docs.example.local/employee-handbook/pto",
                    "title": "Employee Handbook: Paid Time Off",
                }
            ],
            policy={},
            coverage={},
            query_embedding=[0.1, 0.2],
        )

    async def _stream(messages: list[dict[str, Any]], ctx: Any):
        _ = ctx
        assert messages and messages[-1]["role"] == "user"
        yield {"event": "content", "data": "Раздел по отпуску найден в handbook."}
        yield {"event": "usage", "usage": {"total_tokens": 48}}
        yield {
            "event": "assistant_message",
            "message": {
                "role": "assistant",
                "content": "Раздел по отпуску найден в handbook.",
            },
        }

    async def _suggestions(**kwargs: Any) -> list[str]:
        assert kwargs["user_text"] == "Покажи раздел про отпуск из employee-handbook"
        assert kwargs["assistant_text"] == "Раздел по отпуску найден в handbook."
        assert kwargs["sources"][0]["title"] == "Employee Handbook: Paid Time Off"
        return [
            "Открыть Employee Handbook: Paid Time Off",
            "Показать правила переноса отпуска",
        ]

    monkeypatch.setattr(chat_views, "check_input_guardrails", _input_ok)
    monkeypatch.setattr(chat_views, "check_output_guardrails", _output_ok)
    monkeypatch.setattr(chat_views, "get_context", _context)
    monkeypatch.setattr(chat_views, "ai_chat_stream", _stream)
    monkeypatch.setattr(chat_views, "generate_suggestions", _suggestions)

    request = _make_request()
    await chat_views.websocket(request)
    ws = holder["ws"]

    suggestions_payload = next(
        item for item in ws.sent_json if item.get("type") == "suggested_actions"
    )
    assert suggestions_payload["actions"]
    assert suggestions_payload["msg_id"]
    assert "Employee Handbook: Paid Time Off" in suggestions_payload["actions"][0]

    final_payload = next(
        item
        for item in ws.sent_json
        if item.get("partial") is False and "sources" in item
    )
    assert ws.sent_json.index(final_payload) < ws.sent_json.index(suggestions_payload)
    assert final_payload["sources"] == [
        {
            "uri": "https://docs.example.local/employee-handbook/pto",
            "title": "Employee Handbook: Paid Time Off",
        }
    ]

    assistant_insert = state["chat_msg_inserts"][-1]
    assert (
        assistant_insert["used_chunks"][0]["uri"]
        == "https://docs.example.local/employee-handbook/pto"
    )
    assert assistant_insert["tokens"] == 48

    assert metrics_calls
    assert metrics_calls[-1]["status"] == "ok"


@pytest.mark.asyncio
async def test_websocket_deduplicates_source_links_for_same_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _state, _redis, _metrics_calls = _setup_common(monkeypatch)
    holder = _patch_websocket(
        monkeypatch,
        [
            _WsMessage(
                type=aiohttp.WSMsgType.TEXT,
                data="Найди контакт службы ИБ в политике безопасности",
            ),
            _WsMessage(type=aiohttp.WSMsgType.ERROR, data=None),
        ],
    )

    async def _input_ok(*, text: str, provider: Any) -> GuardrailDecision:
        _ = text, provider
        return GuardrailDecision(allowed=True)

    async def _output_ok(*, text: str, provider: Any) -> GuardrailDecision:
        _ = text, provider
        return GuardrailDecision(allowed=True)

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
            messages=[_HistoryMessage("user", "Контакты ИБ")],
            used_chunks=[
                {
                    "uri": "https://kb.example.local/security-policy",
                    "title": "Политика информационной безопасности",
                    "chunk_id": 1,
                },
                {
                    "uri": "https://kb.example.local/security-policy",
                    "title": "Политика информационной безопасности",
                    "chunk_id": 2,
                },
            ],
            sources=[
                {
                    "uri": "https://kb.example.local/security-policy",
                    "title": "Политика информационной безопасности",
                }
            ],
            policy={},
            coverage={},
            query_embedding=[0.1, 0.2],
        )

    async def _stream(messages: list[dict[str, Any]], ctx: Any):
        _ = messages, ctx
        yield {"event": "content", "data": "Контакты ИБ указаны в документе."}
        yield {
            "event": "assistant_message",
            "message": {
                "role": "assistant",
                "content": "Контакты ИБ указаны в документе.",
            },
        }

    monkeypatch.setattr(chat_views, "check_input_guardrails", _input_ok)
    monkeypatch.setattr(chat_views, "check_output_guardrails", _output_ok)
    monkeypatch.setattr(chat_views, "get_context", _context)
    monkeypatch.setattr(chat_views, "ai_chat_stream", _stream)

    request = _make_request()
    await chat_views.websocket(request)
    ws = holder["ws"]

    final_payload = next(
        item
        for item in ws.sent_json
        if item.get("partial") is False and "sources" in item
    )
    assert final_payload["sources"] == [
        {
            "uri": "https://kb.example.local/security-policy",
            "title": "Политика информационной безопасности",
        }
    ]
