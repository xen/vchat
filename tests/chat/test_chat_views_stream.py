from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from itsdangerous import BadSignature

from vchat.app_keys import SIGNER_KEY
from vchat.guardrails import GuardrailDecision
from vchat.models.source_config import SourceConfig
from vchat.triggers import trigger_key
from vchat.views.chat import views as chat_views


@dataclass
class _WsMessage:
    type: aiohttp.WSMsgType
    data: Any


@dataclass
class _FakeProvider:
    id: str = "openai"

    def request_meta(self) -> dict[str, Any]:
        return {"api_key": "k", "base_url": "https://api.example.local/v1"}


@dataclass
class _FakeModel:
    id: str = "gpt-4o-mini"


@dataclass
class _FakeCtx:
    provider: _FakeProvider
    model: _FakeModel
    system_prompt: str = "sys"

    @property
    def provider_id(self) -> str:
        return self.provider.id

    @property
    def model_id(self) -> str:
        return self.model.id

    def request_meta(self) -> dict[str, Any]:
        return self.provider.request_meta()


class _FakeWs:
    def __init__(self, incoming: list[_WsMessage]) -> None:
        self._incoming = deque(incoming)
        self.sent_json: list[dict[str, Any]] = []
        self.sent_str: list[str] = []
        self.closed_codes: list[int | None] = []

    async def prepare(self, request: Any) -> _FakeWs:
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
        self.closed_codes.append(code)

    def exception(self) -> None:
        return None


class _FakeSession:
    def __init__(self, chat_exists: str = "chat-1") -> None:
        self.chat_exists = chat_exists

    async def scalar(self, stmt: Any) -> str:
        _ = stmt
        return self.chat_exists

    async def execute(self, stmt: Any):
        _ = stmt
        return SimpleNamespace(scalar_one=lambda: 1)

    async def commit(self) -> None:
        return None


class _FakeSessionFactory:
    def __init__(self, chat_exists: str = "chat-1") -> None:
        self.chat_exists = chat_exists

    def __call__(self) -> _FakeSessionFactory:
        return self

    async def __aenter__(self) -> _FakeSession:
        return _FakeSession(chat_exists=self.chat_exists)

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        _ = exc_type, exc, tb
        return False


class _RowsSessionFactory:
    def __init__(self, row: Any) -> None:
        self.row = row

    def __call__(self) -> _RowsSessionFactory:
        return self

    async def __aenter__(self) -> _RowsSessionFactory:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        _ = exc_type, exc, tb
        return False

    async def execute(self, stmt: Any) -> Any:
        _ = stmt
        return SimpleNamespace(one_or_none=lambda: self.row)


class _FakeRedis:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    async def sadd(self, key: str, value: str) -> None:
        self.events.append(("sadd", (key, value)))

    async def srem(self, key: str, value: str) -> None:
        self.events.append(("srem", (key, value)))

    async def publish(self, channel: str, payload: str) -> None:
        self.events.append(("publish", (channel, payload)))


def test_is_trivial_query_variants() -> None:
    assert chat_views.is_trivial_query("hello")
    assert chat_views.is_trivial_query("  hi there ")
    assert chat_views.is_trivial_query("???")
    assert not chat_views.is_trivial_query("Как перенести отпуск?")


def test_load_signed_trigger_page_id_validates_signature() -> None:
    class _Signer:
        def loads(self, payload, salt=None, max_age=None):
            assert payload == "token"
            assert salt == "trigger_page"
            assert max_age == 86400
            return "42"

    app = {SIGNER_KEY: _Signer()}

    assert chat_views.load_signed_trigger_page_id(app, "token") == 42


def test_load_signed_trigger_page_id_rejects_bad_signature() -> None:
    class _Signer:
        def loads(self, payload, salt=None, max_age=None):
            _ = payload, salt, max_age
            raise BadSignature("bad")

    app = {SIGNER_KEY: _Signer()}

    assert chat_views.load_signed_trigger_page_id(app, "bad") is None


@pytest.mark.asyncio
async def test_stream_cached_response_text_drips_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _FakeWs([])
    sleeps = []

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(chat_views.asyncio, "sleep", _sleep)

    await chat_views.stream_cached_response_text(
        ws=ws,
        response_text="abcdefghijklmnopqrstuvwxyz0123456789",
    )

    assert sleeps == [
        chat_views.CACHED_TRIGGER_STREAM_DELAY_SECONDS,
        chat_views.CACHED_TRIGGER_STREAM_DELAY_SECONDS,
    ]
    assert ws.sent_json == [
        {
            "ok": True,
            "content": "abcdefghijklmnopqrstuvwxyz012345",
            "partial": True,
        },
        {
            "ok": True,
            "content": "6789",
            "partial": True,
        },
    ]


@pytest.mark.asyncio
async def test_validate_trigger_cache_request_requires_current_page_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = SimpleNamespace(
        id=20,
        source_id=10,
        has_triggers=True,
        triggers=[
            {
                "key": trigger_key("Valid trigger"),
                "text": "Valid trigger",
                "source": "generated",
            }
        ],
    )
    source = SimpleNamespace(
        id=10,
        enable_triggers=True,
        config=SourceConfig(),
    )
    monkeypatch.setattr(
        chat_views, "async_session_factory", _RowsSessionFactory((page, source))
    )

    assert (
        await chat_views.validate_trigger_cache_request(
            page_id=20,
            trigger_key=trigger_key("Other trigger"),
            user_text="Other trigger",
        )
        is False
    )


@pytest.mark.asyncio
async def test_validate_trigger_cache_request_accepts_current_page_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "Valid trigger"
    page = SimpleNamespace(
        id=20,
        source_id=10,
        has_triggers=True,
        triggers=[
            {
                "key": trigger_key(text),
                "text": text,
                "source": "generated",
            }
        ],
    )
    source = SimpleNamespace(
        id=10,
        enable_triggers=True,
        config=SourceConfig(),
    )
    monkeypatch.setattr(
        chat_views, "async_session_factory", _RowsSessionFactory((page, source))
    )

    assert (
        await chat_views.validate_trigger_cache_request(
            page_id=20,
            trigger_key=trigger_key(text),
            user_text=text,
        )
        is True
    )


def test_extract_total_tokens_variants() -> None:
    class _UsageObj:
        def model_dump(self) -> dict[str, Any]:
            return {"total_tokens": "14"}

    assert chat_views.extract_total_tokens(None) == 0
    assert chat_views.extract_total_tokens({"total_tokens": 10}) == 10
    assert chat_views.extract_total_tokens({"total": "12"}) == 12
    assert (
        chat_views.extract_total_tokens({"prompt_tokens": 7, "completion_tokens": 8})
        == 15
    )
    assert chat_views.extract_total_tokens({"input_tokens": 3, "output_tokens": 4}) == 7
    assert chat_views.extract_total_tokens({"usage": {"total_tokens": 9}}) == 9
    assert chat_views.extract_total_tokens(_UsageObj()) == 14
    assert chat_views.extract_total_tokens("bad") == 0


@pytest.mark.asyncio
async def test_ai_chat_stream_guardrails_client_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Usage:
        def model_dump(self):
            return {"total_tokens": 21}

    def _tool(index: int, call_id: str, name: str, args: str):
        return SimpleNamespace(
            index=index,
            id=call_id,
            type="function",
            function=SimpleNamespace(name=name, arguments=args),
        )

    chunk1 = SimpleNamespace(
        usage=_Usage(),
        choices=[
            SimpleNamespace(
                finish_reason=None,
                delta=SimpleNamespace(
                    role="assistant",
                    content="Hello ",
                    refusal=None,
                    tool_calls=[_tool(0, "t1", "search_doc", '{"q":"pto"')],
                ),
            )
        ],
    )
    chunk2 = SimpleNamespace(
        usage=None,
        choices=[
            SimpleNamespace(
                finish_reason="content_filter",
                delta=SimpleNamespace(
                    role=None,
                    content="world",
                    refusal="refuse",
                    tool_calls=[_tool(0, None, None, ',"limit":3}')],
                ),
            )
        ],
    )

    async def _gen():
        yield chunk1
        yield chunk2

    class _Completions:
        async def create(self, **kwargs):
            _ = kwargs
            return _gen()

    guardrails_client = SimpleNamespace(
        chat=SimpleNamespace(completions=_Completions())
    )
    monkeypatch.setattr(
        chat_views, "get_guardrails_client", lambda api_key, base_url: guardrails_client
    )

    events = [
        event
        async for event in chat_views.ai_chat_stream(
            [{"role": "user", "content": "hi"}],
            _FakeCtx(_FakeProvider(), _FakeModel()),
        )
    ]

    assert any(
        e.get("event") == "usage" and e["usage"]["total_tokens"] == 21 for e in events
    )
    assert any(e.get("event") == "content" and e["data"] == "Hello " for e in events)
    assert any(e.get("event") == "content" and e["data"] == "world" for e in events)
    assert any(
        e.get("event") == "guardrail" and e["reason"] == "content_filter"
        for e in events
    )
    assert any(
        e.get("event") == "guardrail" and e["reason"] == "refusal" for e in events
    )
    tool_event = next(e for e in events if e.get("event") == "tool_call")
    assert tool_event["name"] == "search_doc"
    assert tool_event["arguments"] == {"q": "pto", "limit": 3}
    last = events[-1]
    assert last["event"] == "assistant_message"
    assert last["message"]["content"] == "Hello world"


@pytest.mark.asyncio
async def test_ai_chat_stream_raw_mode_and_error_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        chat_views, "get_guardrails_client", lambda api_key, base_url: None
    )

    lines = [
        b"data: " + json.dumps({"usage": {"total_tokens": 7}}).encode("utf-8") + b"\n",
        b"data: "
        + json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "content_filter",
                        "delta": {"role": "assistant", "content": "A", "refusal": "r"},
                    }
                ]
            }
        ).encode("utf-8")
        + b"\n",
        b"data: "
        + json.dumps(
            {
                "choices": [
                    {
                        "delta": {
                            "content": "B",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "tc-1",
                                    "type": "function",
                                    "function": {"name": "f", "arguments": '{"x":1}'},
                                }
                            ],
                        }
                    }
                ]
            }
        ).encode("utf-8")
        + b"\n",
        b"data: [DONE]\n",
    ]

    class _Resp:
        def __init__(self, status=200):
            self.status = status
            self.request_info = None
            self.history = ()
            self.headers = {}
            self.content = self
            self._lines = lines

        async def text(self):
            return "upstream error"

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return False

        def __aiter__(self):
            async def _gen():
                for line in self._lines:
                    yield line

            return _gen()

    class _Session:
        def __init__(self, status=200):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return False

        def post(self, *args, **kwargs):
            _ = args, kwargs
            return _Resp(status=self.status)

    monkeypatch.setattr(
        chat_views.aiohttp, "ClientSession", lambda: _Session(status=200)
    )
    events = [
        event
        async for event in chat_views.ai_chat_stream(
            [{"role": "user", "content": "x"}],
            _FakeCtx(_FakeProvider(), _FakeModel()),
        )
    ]
    assert any(e.get("event") == "usage" for e in events)
    assert any(e.get("event") == "tool_call" for e in events)
    assert events[-1]["event"] == "assistant_message"
    assert events[-1]["message"]["content"] == "AB"

    monkeypatch.setattr(
        chat_views.aiohttp, "ClientSession", lambda: _Session(status=500)
    )
    with pytest.raises(aiohttp.ClientResponseError):
        _ = [
            event
            async for event in chat_views.ai_chat_stream(
                [{"role": "user", "content": "x"}],
                _FakeCtx(_FakeProvider(), _FakeModel()),
            )
        ]


@pytest.mark.asyncio
async def test_ai_chat_stream_passes_gigachat_ssl_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def _token(*args, **kwargs):
        _ = args, kwargs
        return "access-token"

    class _Resp:
        status = 200
        request_info = None
        history = ()
        headers = {}

        def __init__(self):
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return False

        def __aiter__(self):
            async def _gen():
                yield b'data: {"choices":[{"delta":{"content":"A"}}]}\n'
                yield b"data: [DONE]\n"

            return _gen()

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return False

        def post(self, *args, **kwargs):
            _ = args
            captured.update(kwargs)
            return _Resp()

    monkeypatch.setitem(chat_views.config, "gigachat_verify_ssl_certs", False)
    monkeypatch.setattr(chat_views, "get_gigachat_access_token", _token)
    monkeypatch.setattr(chat_views.aiohttp, "ClientSession", lambda: _Session())

    events = [
        event
        async for event in chat_views.ai_chat_stream(
            [{"role": "user", "content": "x"}],
            _FakeCtx(_FakeProvider(id="gigachat"), _FakeModel(id="GigaChat-Pro")),
        )
    ]

    assert captured["ssl"] is False
    assert events[-1]["message"]["content"] == "A"


@pytest.mark.asyncio
async def test_websocket_invalid_signature_closes_1008(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _FakeWs([])
    monkeypatch.setattr(chat_views.web, "WebSocketResponse", lambda: ws)
    monkeypatch.setattr(chat_views, "redis", _FakeRedis())

    class _Serializer:
        def __init__(self, secret):
            _ = secret

        def loads(self, payload, salt=None, max_age=None):
            _ = payload, salt, max_age
            raise BadSignature("bad")

    monkeypatch.setattr(chat_views, "URLSafeSerializer", _Serializer)
    request = SimpleNamespace(match_info={"payload": "bad"}, app={})

    result_ws = await chat_views.websocket(request)
    assert result_ws is ws
    assert 1008 in ws.closed_codes


@pytest.mark.asyncio
async def test_websocket_sends_internal_error_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _FakeWs(
        [
            _WsMessage(type=aiohttp.WSMsgType.TEXT, data="hello"),
            _WsMessage(type=aiohttp.WSMsgType.ERROR, data=None),
        ]
    )
    monkeypatch.setattr(chat_views.web, "WebSocketResponse", lambda: ws)
    monkeypatch.setattr(chat_views, "redis", _FakeRedis())
    monkeypatch.setattr(
        chat_views, "async_session_factory", _FakeSessionFactory(chat_exists="chat-1")
    )

    class _Serializer:
        def __init__(self, secret):
            _ = secret

        def loads(self, payload, salt=None, max_age=None):
            _ = payload, salt, max_age
            return 1, "chat-1"

        def dumps(self, value, salt=None):
            _ = value, salt
            return "signed"

    monkeypatch.setattr(chat_views, "URLSafeSerializer", _Serializer)
    monkeypatch.setattr(
        chat_views,
        "build_generation_context",
        lambda app, widget=None: _FakeCtx(_FakeProvider(), _FakeModel()),
    )

    async def _raise_input_guardrail(*, text: str, provider: Any) -> GuardrailDecision:
        _ = text, provider
        raise RuntimeError("guardrail failed")

    metrics_calls = []
    monkeypatch.setattr(chat_views, "check_input_guardrails", _raise_input_guardrail)
    monkeypatch.setattr(
        chat_views, "record_chat_request", lambda **kwargs: metrics_calls.append(kwargs)
    )

    request = SimpleNamespace(match_info={"payload": "ok"}, app={})
    result_ws = await chat_views.websocket(request)

    assert result_ws is ws
    assert any(
        item.get("ok") is False and item.get("error") == "RuntimeError"
        for item in ws.sent_json
    )
    assert metrics_calls and metrics_calls[0]["status"] == "internal_error"
