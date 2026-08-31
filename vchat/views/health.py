from __future__ import annotations

import asyncio
from typing import Any

import sqlalchemy as sa
from aiohttp import web
from redis.asyncio import from_url as redis_from_url

from jobs.celery import app as celery_app
from jobs.embedder.client import ready as embedding_service_ready
from vchat.settings import REDIS_KEY, cfg
from vchat.views.chat import ctx as chat_ctx
from vchat.views.chat.ai import resolve_ai_settings

CheckResult = dict[str, Any]
QueueSnapshot = dict[str, int]
WorkerQueues = dict[str, list[str]]

CRAWLER_QUEUE = "crawler"
EMBEDDER_QUEUE = "embeddings"


async def live(request: web.Request) -> web.Response:
    del request
    return web.json_response({"status": "ok"})


def _ok(**details: Any) -> CheckResult:
    return {"status": "ok", **details}


def _failed(exc: BaseException | str, **details: Any) -> CheckResult:
    reason = exc if isinstance(exc, str) else exc.__class__.__name__
    return {"status": "failed", "detail": str(reason), **details}


def _queue_names() -> tuple[str, str, str]:
    return cfg.celery_default_queue, CRAWLER_QUEUE, EMBEDDER_QUEUE


async def _check_database(request: web.Request) -> CheckResult:
    try:
        await request["db"].execute(sa.text("select 1"))
    except Exception as exc:
        return _failed(exc)
    return _ok()


async def _check_app_redis(request: web.Request) -> CheckResult:
    try:
        await request.app[REDIS_KEY].ping()
    except Exception as exc:
        return _failed(exc)
    return _ok()


async def _check_celery_broker() -> CheckResult:
    broker_url = f"{cfg.celery_redis_uri}{cfg.celery_broker_db}"
    redis = redis_from_url(broker_url, decode_responses=False)
    try:
        await redis.ping()
        queues = {
            queue_name: int(await redis.llen(queue_name))
            for queue_name in _queue_names()
        }
    except Exception as exc:
        return _failed(exc)
    finally:
        await redis.aclose()
    return _ok(queues=queues)


def _inspect_celery_workers(timeout_seconds: float) -> WorkerQueues:
    inspector = celery_app.control.inspect(timeout=timeout_seconds)
    active_queues = inspector.active_queues() or {}
    worker_queues: WorkerQueues = {}
    for worker_name, queues in active_queues.items():
        queue_names = []
        for queue in queues or []:
            name = queue.get("name") if isinstance(queue, dict) else None
            if name:
                queue_names.append(str(name))
        worker_queues[str(worker_name)] = queue_names
    return worker_queues


async def _check_celery_workers() -> CheckResult:
    default_queue = _queue_names()[0]
    try:
        worker_queues = await asyncio.to_thread(
            _inspect_celery_workers,
            cfg.readiness_celery_timeout_seconds,
        )
    except Exception as exc:
        return _failed(exc)

    if not worker_queues:
        return _failed("no workers", workers=worker_queues)

    default_workers = [
        worker for worker, queues in worker_queues.items() if default_queue in queues
    ]
    if not default_workers:
        return _failed(
            f"no workers listen to {default_queue}",
            workers=worker_queues,
            required_queue=default_queue,
        )

    return _ok(workers=worker_queues, default_workers=default_workers)


async def _check_embedding_service() -> CheckResult:
    try:
        payload = await asyncio.to_thread(embedding_service_ready)
    except Exception as exc:
        return _failed(exc, service_url=cfg.embedding_service_url)
    return _ok(service_url=cfg.embedding_service_url, queues=payload.get("queues", {}))


def _is_model_loaded(value: Any) -> bool:
    return value is not None and value is not False


async def _check_reranker_model() -> CheckResult:
    loaded = _is_model_loaded(getattr(chat_ctx, "_rerank_model", None))
    if not loaded:
        return _failed("reranker model not warmed up")
    return _ok(loaded=True)


async def _check_llm_config() -> CheckResult:
    provider_id = cfg.chat_provider.strip()
    model_id = cfg.chat_model.strip()
    try:
        provider, model = resolve_ai_settings(provider_id, model_id)
    except Exception as exc:
        return _failed(exc, provider=provider_id, model=model_id)

    if not provider.supports_chat:
        return _failed(
            "provider does not support chat",
            provider=provider.id,
            model=model.id,
        )

    if not provider.api_key:
        return _failed("missing api key", provider=provider.id, model=model.id)

    return _ok(provider=provider.id, model=model.id)


async def ready(request: web.Request) -> web.Response:
    checks = {
        "database": await _check_database(request),
        "redis": await _check_app_redis(request),
        "celery_broker": await _check_celery_broker(),
        "celery_workers": await _check_celery_workers(),
        "embedder": await _check_embedding_service(),
        "reranker_model": await _check_reranker_model(),
        "llm": await _check_llm_config(),
    }
    ready_status = (
        "ok"
        if all(check["status"] == "ok" for check in checks.values())
        else "degraded"
    )
    http_status = 200 if ready_status == "ok" else 503
    return web.json_response(
        {"status": ready_status, "checks": checks},
        status=http_status,
    )
