from __future__ import annotations

import logging
import os
from collections.abc import Iterable

from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, REGISTRY, generate_latest, multiprocess

logger = logging.getLogger("vchat.metrics")

CHAT_REQUESTS_TOTAL = Counter(
    "vchat_chat_requests_total",
    "Total number of chat requests handled by websocket endpoint.",
    ("provider", "model", "status", "guardrail"),
)

CHAT_TOKENS_TOTAL = Counter(
    "vchat_chat_tokens_total",
    "Total number of LLM tokens consumed by chat requests.",
    ("provider", "model"),
)

CHAT_GUARDRAIL_EVENTS_TOTAL = Counter(
    "vchat_chat_guardrail_events_total",
    "Total number of guardrail remarks detected while processing chat requests.",
    ("provider", "model", "reason"),
)


def _safe_label(value: str | None, fallback: str) -> str:
    value = (value or "").strip()
    if not value:
        return fallback
    return value[:128]


def _normalize_guardrail_reason(reason: str | None) -> str:
    normalized = (reason or "unknown").strip().lower()
    if not normalized:
        normalized = "unknown"

    allowlist = {
        "content_filter",
        "refusal",
        "safety",
        "policy_violation",
        "provider_block",
        "input_blocked",
        "output_blocked",
        "tool_blocked",
        "guardrail_tripwire",
        "contains_pii",
        "moderation",
        "prompt_injection_detection",
        "jailbreak",
        "custom_prompt_check",
        "url_filter",
        "nsfw_text",
        "russian_pii",
        "passport_ru",
        "inn_ru",
        "snils_ru",
        "oms_ru",
        "phone_number_ru",
        "unknown",
    }
    if normalized in allowlist:
        return normalized
    return "unknown"


def record_chat_request(
    *,
    provider: str | None,
    model: str | None,
    tokens: int | None,
    status: str,
    guardrail_reasons: Iterable[str] | None,
) -> None:
    provider_label = _safe_label(provider, "unknown")
    model_label = _safe_label(model, "unknown")
    status_label = _safe_label(status, "unknown")

    reasons = {
        _normalize_guardrail_reason(reason)
        for reason in (guardrail_reasons or [])
    }

    guardrail_label = "true" if reasons else "false"
    CHAT_REQUESTS_TOTAL.labels(
        provider=provider_label,
        model=model_label,
        status=status_label,
        guardrail=guardrail_label,
    ).inc()

    token_count = max(int(tokens or 0), 0)
    if token_count:
        CHAT_TOKENS_TOTAL.labels(
            provider=provider_label,
            model=model_label,
        ).inc(token_count)

    for reason in reasons:
        CHAT_GUARDRAIL_EVENTS_TOTAL.labels(
            provider=provider_label,
            model=model_label,
            reason=reason,
        ).inc()


def _is_multiprocess_enabled() -> bool:
    return bool(os.getenv("PROMETHEUS_MULTIPROC_DIR"))


def _build_registry() -> CollectorRegistry:
    if not _is_multiprocess_enabled():
        return REGISTRY

    registry = CollectorRegistry(support_collectors_without_names=True)
    multiprocess.MultiProcessCollector(registry)
    return registry


async def metrics_handler(_: web.Request) -> web.Response:
    data = generate_latest(_build_registry())
    return web.Response(body=data, headers={"Content-Type": CONTENT_TYPE_LATEST})


def validate_multiprocess_setup() -> None:
    metrics_dir = os.getenv("PROMETHEUS_MULTIPROC_DIR")
    if not metrics_dir:
        return

    if not os.path.isdir(metrics_dir):
        logger.warning(
            "PROMETHEUS_MULTIPROC_DIR='%s' is not a directory; multiprocess metrics may fail",
            metrics_dir,
        )
