from __future__ import annotations

from types import SimpleNamespace

import pytest

from vchat import guardrails


@pytest.mark.asyncio
async def test_check_input_guardrails_blocks_russian_pii(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(guardrails.config, "guardrails_ru_pii_enabled", True)
    monkeypatch.setattr(
        guardrails,
        "detect_russian_pii_reasons",
        lambda text: {"passport_ru", "russian_pii"} if "паспорт" in text else set(),
    )
    decision = await guardrails.check_input_guardrails(
        text="Мой паспорт 1234 567890",
        provider=SimpleNamespace(),
    )
    assert decision.allowed is False
    assert "input_blocked" in decision.reasons
    assert "passport_ru" in decision.reasons


@pytest.mark.asyncio
async def test_check_output_guardrails_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(guardrails.config, "guardrails_ru_pii_enabled", False)
    decision = await guardrails.check_output_guardrails(
        text="Телефон +7 999 123 45 67",
        provider=SimpleNamespace(),
    )
    assert decision.allowed is True
    assert decision.reasons == set()


def test_mask_russian_pii_without_patterns_returns_original() -> None:
    text = "Обычный текст без персональных данных"
    masked, has_pii = guardrails.mask_russian_pii(text)
    assert has_pii is False
    assert masked == text


def test_mask_russian_pii_uses_presidio_for_russian_patterns() -> None:
    text = "Телефон: +7 999 123-45-67, паспорт: 12 34 567890"
    masked, has_pii = guardrails.mask_russian_pii(text)
    assert has_pii is True
    assert masked.count("***") == 2
    assert "+7 999 123-45-67" not in masked
    assert "12 34 567890" not in masked


def test_mask_russian_pii_keeps_original_when_presidio_finds_nothing() -> None:
    masked, has_pii = guardrails.mask_russian_pii("Просто сообщение без PII")
    assert has_pii is False
    assert masked == "Просто сообщение без PII"


def test_detect_russian_pii_reasons_passport_format() -> None:
    reasons = guardrails.detect_russian_pii_reasons("Серия и номер: 12 34 567890")
    assert "passport_ru" in reasons
    assert "russian_pii" in reasons


def test_extract_tripwire_details_normalizes_values() -> None:
    exc = SimpleNamespace(
        guardrail_result=SimpleNamespace(
            info={"stage_name": "pre_flight", "guardrail_name": "PII Blocker!"}
        )
    )
    stage, reason = guardrails.extract_tripwire_details(exc)  # type: ignore[arg-type]
    assert stage == "input"
    assert reason == "pii_blocker"


def test_get_guardrails_client_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(guardrails.config, "openai_guardrails_enabled", False)
    client = guardrails.get_guardrails_client(
        api_key="k",
        base_url="https://example.com",
    )
    assert client is None


def test_get_guardrails_client_init_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(guardrails.config, "openai_guardrails_enabled", True)
    monkeypatch.setattr(guardrails, "_cached_client", None)
    monkeypatch.setattr(guardrails, "_cached_key", None)

    def _boom(**kwargs):
        _ = kwargs
        raise RuntimeError("broken")

    monkeypatch.setattr(guardrails, "GuardrailsAsyncOpenAI", _boom)
    client = guardrails.get_guardrails_client(
        api_key="k",
        base_url="https://example.com",
    )
    assert client is None
