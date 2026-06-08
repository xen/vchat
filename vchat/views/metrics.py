from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable

import redis as redis_lib
from aiohttp import web
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    REGISTRY,
    generate_latest,
    multiprocess,
)
from prometheus_client.core import GaugeMetricFamily
from vchat.settings import config

logger = logging.getLogger("vchat.views.metrics")

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

CRAWLER_PAGES_TOTAL = Counter(
    "vchat_crawler_pages_total",
    "Total pages processed by the crawler, by result type.",
    ("source_id", "result"),
)

CRAWLER_RUN_DURATION_SECONDS = Histogram(
    "vchat_crawler_run_duration_seconds",
    "Duration of a single crawl run in seconds.",
    ("source_id",),
    buckets=(30, 60, 120, 300, 600, 1200, 1800, 3600),
)

CRAWLER_RATE_LIMITED_TOTAL = Counter(
    "vchat_crawler_rate_limited_total",
    "Number of crawl runs that encountered rate limiting.",
    ("source_id",),
)

CRAWLER_LAST_CRAWL_TIMESTAMP = Gauge(
    "vchat_crawler_last_crawl_timestamp_seconds",
    "Unix timestamp of the last completed crawl run for a source.",
    ("source_id",),
    multiprocess_mode="livemax",
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
        _normalize_guardrail_reason(reason) for reason in (guardrail_reasons or [])
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


def record_crawl_run(
    *,
    source_id: int,
    pages_new: int,
    pages_changed: int,
    pages_crawled: int,
    pages_errors: int,
    pages_excluded: int,
    duration_seconds: float | None,
    was_rate_limited: bool,
) -> None:
    sid = str(source_id)
    n_new = int(pages_new or 0)
    n_changed = int(pages_changed or 0)
    n_crawled = int(pages_crawled or 0)
    n_errors = int(pages_errors or 0)
    n_excluded = int(pages_excluded or 0)
    if n_new:
        CRAWLER_PAGES_TOTAL.labels(source_id=sid, result="new").inc(n_new)
    if n_changed:
        CRAWLER_PAGES_TOTAL.labels(source_id=sid, result="changed").inc(n_changed)
    if n_crawled:
        CRAWLER_PAGES_TOTAL.labels(source_id=sid, result="unchanged").inc(n_crawled)
    if n_errors:
        CRAWLER_PAGES_TOTAL.labels(source_id=sid, result="error").inc(n_errors)
    if n_excluded:
        CRAWLER_PAGES_TOTAL.labels(source_id=sid, result="excluded").inc(n_excluded)
    if duration_seconds is not None and float(duration_seconds) >= 0:
        CRAWLER_RUN_DURATION_SECONDS.labels(source_id=sid).observe(
            float(duration_seconds)
        )
    if was_rate_limited:
        CRAWLER_RATE_LIMITED_TOTAL.labels(source_id=sid).inc()
    CRAWLER_LAST_CRAWL_TIMESTAMP.labels(source_id=sid).set(time.time())


class CrawlerQueueCollector:
    """Reads Celery queue lengths from Redis at scrape time."""

    def describe(self):
        return []

    def collect(self):
        celery_metric = GaugeMetricFamily(
            "vchat_celery_queue_size",
            "Current number of tasks in the default Celery queue.",
        )
        embedder_metric = GaugeMetricFamily(
            "vchat_embedder_queue_size",
            "Current number of tasks in the embedder Celery queue.",
        )
        try:
            broker_url = f"{config['celery_redis_uri']}{config['celery_broker_db']}"
            r = redis_lib.Redis.from_url(broker_url, decode_responses=False)
            celery_metric.add_metric([], float(r.llen("celery")))
            embedder_metric.add_metric([], float(r.llen("embeddings")))
            r.close()
        except Exception as exc:
            logger.debug("CrawlerQueueCollector: Redis error: %s", exc)
        yield celery_metric
        yield embedder_metric


REGISTRY.register(CrawlerQueueCollector())


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
