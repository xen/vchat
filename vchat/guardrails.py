from __future__ import annotations

import re
from dataclasses import dataclass, field

from vchat.ai_providers import BaseAIProvider
from vchat.settings import config


@dataclass
class GuardrailDecision:
    allowed: bool = True
    reasons: set[str] = field(default_factory=set)
    message: str | None = None


def _enabled(key: str, default: bool = True) -> bool:
    return bool(config.get(key, default))


_RU_PHONE_RE = re.compile(
    r"(?:\\+7|8)\\s*\(?\\d{3}\)?[\\s-]*\\d{3}[\\s-]*\\d{2}[\\s-]*\\d{2}"
)
_RU_PASSPORT_RE = re.compile(r"\\b\\d{2}\\s?\\d{2}\\s?\\d{6}\\b")
_RU_INN_RE = re.compile(r"\\b(?:\\d{10}|\\d{12})\\b")
_RU_SNILS_RE = re.compile(r"\\b\\d{3}-?\\d{3}-?\\d{3}\\s?\\d{2}\\b")
_RU_OMS_RE = re.compile(r"\\b\\d{16}\\b")


def _detect_russian_pii_reasons(text: str) -> set[str]:
    reasons: set[str] = set()
    if not text:
        return reasons

    if _RU_PHONE_RE.search(text):
        reasons.add("phone_number_ru")
    if _RU_PASSPORT_RE.search(text):
        reasons.add("passport_ru")
    if _RU_INN_RE.search(text):
        reasons.add("inn_ru")
    if _RU_SNILS_RE.search(text):
        reasons.add("snils_ru")
    if _RU_OMS_RE.search(text):
        reasons.add("oms_ru")

    if reasons:
        reasons.add("russian_pii")

    return reasons


def detect_russian_pii_reasons(text: str) -> set[str]:
    return _detect_russian_pii_reasons(text)


def mask_russian_pii(text: str) -> tuple[str, bool]:
    if not text:
        return text, False

    patterns = (
        _RU_PHONE_RE,
        _RU_PASSPORT_RE,
        _RU_SNILS_RE,
        _RU_OMS_RE,
        _RU_INN_RE,
    )
    masked = text
    has_pii = False
    for pattern in patterns:
        masked_next, count = pattern.subn("***", masked)
        if count:
            has_pii = True
            masked = masked_next
    return masked, has_pii


async def check_input_guardrails(
    *,
    text: str,
    provider: BaseAIProvider,
) -> GuardrailDecision:
    _ = provider
    decision = GuardrailDecision()
    if not _enabled("guardrails_ru_pii_enabled", True):
        return decision

    reasons = detect_russian_pii_reasons(text)
    if reasons:
        decision.allowed = False
        decision.reasons.update(reasons)
        decision.reasons.add("input_blocked")
        decision.message = "Сообщение заблокировано: обнаружены персональные данные РФ."

    return decision


async def check_output_guardrails(
    *,
    text: str,
    provider: BaseAIProvider,
) -> GuardrailDecision:
    _ = provider
    decision = GuardrailDecision()
    if not _enabled("guardrails_ru_pii_enabled", True):
        return decision

    reasons = detect_russian_pii_reasons(text)
    if reasons:
        decision.allowed = False
        decision.reasons.update(reasons)
        decision.reasons.add("output_blocked")
        decision.message = "Ответ заблокирован: обнаружены персональные данные РФ."

    return decision
