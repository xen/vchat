from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from guardrails import GuardrailsAsyncOpenAI
from presidio_analyzer import (
    AnalyzerEngine,
    Pattern,
    PatternRecognizer,
    RecognizerRegistry,
)
from presidio_analyzer.context_aware_enhancers import ContextAwareEnhancer
from presidio_analyzer.nlp_engine import NlpArtifacts, NlpEngine
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

from vchat.views.chat.ai import BaseAIProvider
from vchat.settings import config

logger = logging.getLogger("vchat.views.chat.guardrails")

_cached_client: Any | None = None
_cached_key: tuple[str, str] | None = None
_presidio_analyzer: Any | None = None
_presidio_anonymizer: Any | None = None

_OPENAI_GUARDRAILS_PIPELINE: dict[str, Any] = {
    "version": 1,
    "pre_flight": {
        "version": 1,
        "guardrails": [
            {
                "name": "Contains PII",
                "config": {
                    "entities": [
                        "CREDIT_CARD",
                        "CVV",
                        "CRYPTO",
                        "EMAIL_ADDRESS",
                        "IBAN_CODE",
                        "BIC_SWIFT",
                        "IP_ADDRESS",
                        "MEDICAL_LICENSE",
                        "NRP",
                        "PHONE_NUMBER",
                    ]
                },
            },
            {
                "name": "Moderation",
                "config": {
                    "categories": [
                        "sexual",
                        "sexual/minors",
                        "hate",
                        "hate/threatening",
                        "harassment",
                        "harassment/threatening",
                        "self-harm",
                        "self-harm/intent",
                        "self-harm/instructions",
                        "violence",
                        "violence/graphic",
                        "illicit",
                        "illicit/violent",
                    ]
                },
            },
            {
                "name": "Prompt Injection Detection",
                "config": {
                    "confidence_threshold": 0.7,
                    "model": "gpt-4.1-mini",
                    "include_reasoning": False,
                },
            },
        ],
    },
    "input": {
        "version": 1,
        "guardrails": [
            {
                "name": "Jailbreak",
                "config": {
                    "confidence_threshold": 0.7,
                    "model": "gpt-4.1-mini",
                    "include_reasoning": False,
                },
            },
            {
                "name": "Custom Prompt Check",
                "config": {
                    "confidence_threshold": 0.7,
                    "model": "gpt-4.1-mini",
                    "system_prompt_details": (
                        "You are a customer support assistant. Raise the "
                        "guardrail if questions aren't focused on customer "
                        "inquiries, product support, and service-related questions."
                    ),
                    "include_reasoning": False,
                },
            },
        ],
    },
    "output": {
        "version": 1,
        "guardrails": [
            {
                "name": "NSFW Text",
                "config": {
                    "confidence_threshold": 0.7,
                    "model": "gpt-4.1-mini",
                    "include_reasoning": False,
                },
            },
            {
                "name": "Prompt Injection Detection",
                "config": {
                    "confidence_threshold": 0.7,
                    "model": "gpt-4.1-mini",
                    "include_reasoning": False,
                },
            },
        ],
    },
}


@dataclass
class GuardrailDecision:
    allowed: bool = True
    reasons: set[str] = field(default_factory=set)
    message: str | None = None


def _enabled(key: str, default: bool = True) -> bool:
    return bool(config.get(key, default))


def get_guardrails_client(*, api_key: str, base_url: str) -> Any | None:
    if not _enabled("openai_guardrails_enabled", True):
        return None

    global _cached_client, _cached_key
    key = (api_key, base_url)
    if _cached_client is not None and _cached_key == key:
        return _cached_client

    try:
        _cached_client = GuardrailsAsyncOpenAI(
            config=_OPENAI_GUARDRAILS_PIPELINE,
            raise_guardrail_errors=False,
            api_key=api_key,
            base_url=base_url,
        )
    except Exception as exc:
        logger.warning("Failed to initialize GuardrailsAsyncOpenAI: %s", exc)
        return None

    _cached_key = key
    return _cached_client


def extract_tripwire_details(exc: Any) -> tuple[str, str]:
    info: dict[str, Any] = (
        getattr(getattr(exc, "guardrail_result", None), "info", {}) or {}
    )

    stage = str(info.get("stage_name", "input")).strip().lower()
    if stage == "pre_flight":
        stage = "input"
    if stage not in {"input", "output"}:
        stage = "input"

    name = str(info.get("guardrail_name", "guardrail_tripwire")).strip().lower()
    name = re.sub(r"[^a-z0-9_]+", "_", name).strip("_")
    if not name:
        name = "guardrail_tripwire"

    return stage, name


_RU_PHONE_RE = re.compile(r"(?:\+7|8)\s*\(?\d{3}\)?[\s-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}")
_RU_PASSPORT_RE = re.compile(r"\b\d{2}\s+\d{2}\s+\d{6}\b")
_RU_INN_RE = re.compile(r"\b(?:\d{10}|\d{12})\b")
_RU_SNILS_RE = re.compile(r"\b\d{3}-?\d{3}-?\d{3}\s?\d{2}\b")
_RU_OMS_RE = re.compile(r"\b\d{16}\b")

_PRESIDIO_LANGUAGE = "ru"
_PRESIDIO_SCORE = 0.9
_RU_PII_ENTITY_TO_REASON: dict[str, str] = {
    "PHONE_NUMBER_RU": "phone_number_ru",
    "PASSPORT_RU": "passport_ru",
    "INN_RU": "inn_ru",
    "SNILS_RU": "snils_ru",
    "OMS_RU": "oms_ru",
}
_RU_PII_REASON_TO_PATTERN: dict[str, re.Pattern[str]] = {
    "phone_number_ru": _RU_PHONE_RE,
    "passport_ru": _RU_PASSPORT_RE,
    "inn_ru": _RU_INN_RE,
    "snils_ru": _RU_SNILS_RE,
    "oms_ru": _RU_OMS_RE,
}


class _NoopNlpEngine(NlpEngine):
    def load(self) -> None:
        return None

    def is_loaded(self) -> bool:
        return True

    def process_text(self, text: str, language: str) -> NlpArtifacts:
        tokens: Any = []
        return NlpArtifacts(
            entities=[],
            tokens=tokens,
            tokens_indices=[],
            lemmas=[],
            nlp_engine=self,
            language=language,
        )

    def process_batch(
        self,
        texts,
        language: str,
        batch_size: int = 1,
        n_process: int = 1,
        **kwargs,
    ):
        for text in texts:
            yield text, self.process_text(text, language)

    def is_stopword(self, word: str, language: str) -> bool:
        return False

    def is_punct(self, word: str, language: str) -> bool:
        return False

    def get_supported_entities(self) -> list[str]:
        return []

    def get_supported_languages(self) -> list[str]:
        return [_PRESIDIO_LANGUAGE]


class _NoopContextAwareEnhancer(ContextAwareEnhancer):
    def __init__(self) -> None:
        super().__init__(
            context_similarity_factor=0.0,
            min_score_with_context_similarity=0.0,
            context_prefix_count=0,
            context_suffix_count=0,
        )

    def enhance_using_context(
        self,
        text: str,
        raw_results: list[Any],
        nlp_artifacts: Any,
        recognizers: list[Any],
        context: list[str] | None = None,
    ) -> list[Any]:
        return raw_results


def _build_presidio_analyzer() -> AnalyzerEngine:
    registry = RecognizerRegistry(supported_languages=[_PRESIDIO_LANGUAGE])
    for entity, reason in _RU_PII_ENTITY_TO_REASON.items():
        pattern = _RU_PII_REASON_TO_PATTERN[reason]
        registry.add_recognizer(
            PatternRecognizer(
                name=f"{reason}_recognizer",
                supported_entity=entity,
                supported_language=_PRESIDIO_LANGUAGE,
                patterns=[
                    Pattern(
                        name=reason,
                        regex=pattern.pattern,
                        score=_PRESIDIO_SCORE,
                    )
                ],
            )
        )

    return AnalyzerEngine(
        registry=registry,
        nlp_engine=_NoopNlpEngine(),
        supported_languages=[_PRESIDIO_LANGUAGE],
        context_aware_enhancer=_NoopContextAwareEnhancer(),
    )


def _get_presidio_engines() -> tuple[AnalyzerEngine, AnonymizerEngine]:
    global _presidio_analyzer, _presidio_anonymizer
    if _presidio_analyzer is not None and _presidio_anonymizer is not None:
        return _presidio_analyzer, _presidio_anonymizer

    _presidio_analyzer = _build_presidio_analyzer()
    _presidio_anonymizer = AnonymizerEngine()
    return _presidio_analyzer, _presidio_anonymizer


def _analyze_russian_pii_with_presidio(text: str) -> list[Any]:
    if not text:
        return []

    analyzer, _anonymizer = _get_presidio_engines()
    return analyzer.analyze(text=text, language=_PRESIDIO_LANGUAGE)


def _detect_russian_pii_reasons(text: str) -> set[str]:
    presidio_results = _analyze_russian_pii_with_presidio(text)
    reasons = {
        _RU_PII_ENTITY_TO_REASON[result.entity_type]
        for result in presidio_results
        if result.entity_type in _RU_PII_ENTITY_TO_REASON
    }
    if reasons:
        reasons.add("russian_pii")
    return reasons


def detect_russian_pii_reasons(text: str) -> set[str]:
    reasons = _detect_russian_pii_reasons(text)
    regex_map = {
        "phone_number_ru": _RU_PHONE_RE,
        "passport_ru": _RU_PASSPORT_RE,
        "inn_ru": _RU_INN_RE,
        "snils_ru": _RU_SNILS_RE,
        "oms_ru": _RU_OMS_RE,
    }
    for reason, pattern in regex_map.items():
        if pattern.search(text):
            reasons.add(reason)
    if reasons:
        reasons.add("russian_pii")
    return reasons


def mask_russian_pii(text: str) -> tuple[str, bool]:
    if not text:
        return text, False

    analyzer_results = _analyze_russian_pii_with_presidio(text)
    if not analyzer_results:
        return text, False

    _, anonymizer = _get_presidio_engines()
    masked = anonymizer.anonymize(
        text=text,
        analyzer_results=analyzer_results,
        operators={"DEFAULT": OperatorConfig("replace", {"new_value": "***"})},
    ).text
    return masked, True


async def check_input_guardrails(
    *,
    text: str,
    provider: BaseAIProvider,
) -> GuardrailDecision:
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
