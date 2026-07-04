from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from jobs.embedder import model as embeddings
from vchat.views.chat import ctx as chat_ctx
from vchat.views.chat import guardrails
from jobs.documents.types import guess_document_type
from vchat.models.base import (
    DateTime_,
    generate_uuid7,
)
from vchat.models.data import Page
from vchat.views.user import views as user_views


def test_models_base_datetime_type_and_uuid() -> None:
    dt = DateTime_()
    assert dt.process_bind_param("2025-01-01T01:02:03", None).year == 2025
    assert dt.python_type is datetime
    assert isinstance(generate_uuid7(), str)


def test_page_patch_meta_reassigns_copy() -> None:
    page = Page(meta={"old": "keep", "error": "drop"})
    original_meta = page.meta

    patched = page.patch_meta(remove=("error", "missing"), reason="too_big")

    assert patched == {"old": "keep", "reason": "too_big"}
    assert page.meta == patched
    assert page.meta is not original_meta


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
    monkeypatch.setattr(embeddings.cfg, "embedding_device", "cpu")
    monkeypatch.setattr(embeddings.cfg, "embedding_max_seq_length", 123)
    calls = {}

    class _FakeST:
        def __init__(self, model_path, device, tokenizer_kwargs, trust_remote_code):
            calls["model_path"] = model_path
            calls["device"] = device
            calls["tokenizer_kwargs"] = tokenizer_kwargs
            calls["trust_remote_code"] = trust_remote_code

    monkeypatch.setattr(embeddings, "SentenceTransformer", _FakeST)
    model = embeddings.load_embedding_model()
    assert isinstance(model, _FakeST)
    assert calls["model_path"] == "models/embedder"
    assert calls["device"] == "cpu"
    assert calls["tokenizer_kwargs"] == {"truncation": True, "max_length": 123}
    assert model.max_seq_length == 123


def test_resolve_embedding_device_uses_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embeddings.cfg, "embedding_device", "cpu")
    assert embeddings.resolve_embedding_device() == "cpu"


def test_resolve_embedding_device_auto_uses_detector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embeddings.cfg, "embedding_device", "auto")
    monkeypatch.setattr(embeddings, "detect_best_device", lambda: "cpu")

    assert embeddings.resolve_embedding_device() == "cpu"


def test_resolve_embedding_device_rejects_unavailable_mps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embeddings.cfg, "embedding_device", "mps")
    monkeypatch.setattr(
        embeddings.torch,
        "backends",
        SimpleNamespace(mps=SimpleNamespace(is_available=lambda: False)),
    )

    with pytest.raises(RuntimeError, match="mps"):
        embeddings.resolve_embedding_device()


def test_resolve_embedding_device_rejects_unavailable_cuda(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embeddings.cfg, "embedding_device", "cuda")
    monkeypatch.setattr(
        embeddings.torch,
        "cuda",
        SimpleNamespace(is_available=lambda: False),
    )

    with pytest.raises(RuntimeError, match="cuda"):
        embeddings.resolve_embedding_device()


def test_loadrerank_rejects_unavailable_configured_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chat_ctx, "_rerank_model", None)
    monkeypatch.setattr(chat_ctx.cfg, "reranker_device", "cuda")
    monkeypatch.setattr(
        chat_ctx.torch,
        "cuda",
        SimpleNamespace(is_available=lambda: False),
    )

    with pytest.raises(RuntimeError, match="Reranker device cuda"):
        chat_ctx.loadrerank()


def test_openai_guardrails_cache_and_extract(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _ = tmp_path
    monkeypatch.setattr(guardrails.cfg, "openai_guardrails_enabled", True)
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
