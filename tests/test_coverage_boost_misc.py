from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from vchat import embeddings
from vchat import guardrails
from vchat.document_types import get_document_type_label, guess_document_type
from vchat.models.base import (
    Base,
    DateTime_,
    JsonColumnError,
    JsonTypeError,
    dict_to_json,
    generate_uuid7,
)
from vchat.views.user import views as user_views


def test_models_base_dict_to_json_and_datetime_type() -> None:
    payload = {
        "a": 1,
        "b": datetime(2025, 1, 1, 12, 0, 0),
        "c": Decimal("1.5"),
        "d": b"abc",
        "e": None,
    }
    text = dict_to_json(payload)
    assert '"a":1' in text
    assert "2025-01-01T12:00:00" in text

    dt = DateTime_()
    assert dt.process_bind_param("2025-01-01T01:02:03", None).year == 2025
    assert dt.python_type is datetime


def test_models_base_json_errors_and_from_json(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Dummy:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    _Dummy.from_json = classmethod(Base.from_json.__func__)  # type: ignore[attr-defined]

    class _Type:
        def __init__(self, py):
            self.python_type = py

    columns = {
        "name": SimpleNamespace(type=_Type(str)),
        "price": SimpleNamespace(type=_Type(Decimal)),
        "created_at": SimpleNamespace(type=_Type(datetime)),
        "blob": SimpleNamespace(type=_Type(bytes)),
    }
    monkeypatch.setattr(sa, "inspect", lambda cls: SimpleNamespace(columns=columns))

    item = _Dummy.from_json(
        '{"name":"x","price":"10.5","created_at":"2025-01-01T00:00:00","blob":"YQ=="}'
    )
    assert item.name == "x"
    assert item.price == Decimal("10.5")
    assert item.created_at.year == 2025
    assert item.blob == b"a"

    with pytest.raises(JsonColumnError):
        _Dummy.from_json('{"unknown":"x"}')
    with pytest.raises(JsonTypeError):
        _Dummy.from_json("[]")
    with pytest.raises(JsonTypeError):
        dict_to_json({"x": object()})

    assert isinstance(generate_uuid7(), str)


def test_guardrails_reason_detection_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    class _R:
        def __init__(self, hit: bool):
            self.hit = hit

        def search(self, text):
            _ = text
            return object() if self.hit else None

    monkeypatch.setattr(guardrails, "_RU_PHONE_RE", _R(True))
    monkeypatch.setattr(guardrails, "_RU_PASSPORT_RE", _R(True))
    monkeypatch.setattr(guardrails, "_RU_INN_RE", _R(False))
    monkeypatch.setattr(guardrails, "_RU_SNILS_RE", _R(False))
    monkeypatch.setattr(guardrails, "_RU_OMS_RE", _R(True))
    reasons = guardrails.detect_russian_pii_reasons("x")
    assert "phone_number_ru" in reasons
    assert "passport_ru" in reasons
    assert "oms_ru" in reasons
    assert "russian_pii" in reasons


def test_embeddings_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(embeddings.config, "embedding_model_dir", "data")
    monkeypatch.setitem(embeddings.config, "embedding_max_seq_length", 123)
    calls = {}

    class _FakeST:
        def __init__(self, model_path, device, tokenizer_kwargs, trust_remote_code):
            calls["model_path"] = model_path
            calls["device"] = device
            calls["tokenizer_kwargs"] = tokenizer_kwargs
            calls["trust_remote_code"] = trust_remote_code

    monkeypatch.setattr(embeddings, "SentenceTransformer", _FakeST)
    model = embeddings.load_embedding_model(device="cpu")
    assert isinstance(model, _FakeST)
    assert calls["model_path"] == "data"
    assert calls["tokenizer_kwargs"] == {"truncation": True, "max_length": 123}
    assert model.max_seq_length == 123


def test_resolve_embedding_device_prefers_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMBEDDING_DEVICE", "cpu")
    monkeypatch.setitem(embeddings.config, "embedding_device", "mps")
    assert embeddings.resolve_embedding_device() == "cpu"


def test_openai_guardrails_cache_and_extract(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _ = tmp_path
    monkeypatch.setitem(guardrails.config, "openai_guardrails_enabled", True)
    monkeypatch.setattr(guardrails, "_cached_client", None)
    monkeypatch.setattr(guardrails, "_cached_key", None)

    created = []

    class _FakeClient:
        def __init__(self, **kwargs):
            created.append(kwargs)

    monkeypatch.setattr(guardrails, "GuardrailsAsyncOpenAI", _FakeClient)
    one = guardrails.get_guardrails_client(
        api_key="k",
        base_url="https://example.com",
    )
    two = guardrails.get_guardrails_client(
        api_key="k",
        base_url="https://example.com",
    )
    assert one is two
    assert len(created) == 1
    assert created[0]["config"] == guardrails._OPENAI_GUARDRAILS_PIPELINE

    stage, reason = guardrails.extract_tripwire_details(
        SimpleNamespace(
            guardrail_result=SimpleNamespace(
                info={"stage_name": "output", "guardrail_name": ""}
            )
        )
    )
    assert stage == "output"
    assert reason == "guardrail_tripwire"


def test_document_type_branches() -> None:
    assert guess_document_type(uri="https://x/a.css") == "code"
    assert guess_document_type(content_type="video/mp4") == "video"
    assert guess_document_type(content_type="text/html; charset=utf-8") == "html"
    assert guess_document_type(content_type="application/custom+json") == "code"
    assert get_document_type_label("")  # fallback label


@pytest.mark.asyncio
async def test_user_forward_notifications_covers_message_paths() -> None:
    sent = []

    class _PubSub:
        async def subscribe(self, channel):
            self.channel = channel

        async def listen(self):
            for payload in [
                {"type": "subscribe"},
                {"type": "message", "data": b"hello"},
                {"type": "message", "data": ""},
            ]:
                yield payload

        async def unsubscribe(self, channel):
            self.unsub = channel

        async def close(self):
            self.closed = True

    pubsub = _PubSub()
    request = _Req(
        {
            "user": SimpleNamespace(id=7),
            "app": {
                user_views.REDIS_KEY: SimpleNamespace(
                    pubsub=lambda: pubsub,
                    lrem=lambda *args, **kwargs: _awaitable_append([], None),
                )
            },
        }
    )
    ws = SimpleNamespace(send_str=lambda payload: _awaitable_append(sent, payload))
    await user_views._forward_notifications(ws, request)  # type: ignore[arg-type]
    assert sent == ["hello"]
    assert pubsub.channel == "user_7"
    assert pubsub.unsub == "user_7"


@pytest.mark.asyncio
async def test_user_forward_notifications_handles_redis_disconnect() -> None:
    closed = []

    class _PubSub:
        async def subscribe(self, channel):
            self.channel = channel

        async def listen(self):
            raise user_views.RedisError("boom")
            yield

        async def unsubscribe(self, channel):
            raise user_views.RedisError(f"unsubscribe {channel}")

        async def close(self):
            raise user_views.RedisError("close")

    request = _Req(
        {
            "user": SimpleNamespace(id=9),
            "app": {user_views.REDIS_KEY: SimpleNamespace(pubsub=lambda: _PubSub())},
        }
    )

    class _WS:
        closed = False

        async def close(self):
            closed.append(True)
            self.closed = True

    await user_views._forward_notifications(_WS(), request)  # type: ignore[arg-type]
    assert closed == [True]


async def _awaitable_append(items: list[str], payload: str):
    items.append(payload)
    await asyncio.sleep(0)


class _Req(dict):
    @property
    def app(self):
        return self["app"]
