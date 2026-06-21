from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from aiohttp import web
from redis.asyncio import from_url as redis_from_url

from jobs.celery import app as celery_app
from jobs.embedder.model import resolve_embedding_device
from vchat.settings import REDIS_KEY, config
from vchat.views.chat import ctx as chat_ctx
from vchat.views.chat.ai import resolve_ai_settings

CheckResult = dict[str, Any]
QueueSnapshot = dict[str, int]
WorkerQueues = dict[str, list[str]]

EMBEDDER_QUEUE = "embeddings"
CRAWLER_QUEUE = "crawler"


async def live(request: web.Request) -> web.Response:
    del request
    return web.json_response({"status": "ok"})


def _ok(**details: Any) -> CheckResult:
    return {"status": "ok", **details}


def _failed(exc: BaseException | str, **details: Any) -> CheckResult:
    reason = exc if isinstance(exc, str) else exc.__class__.__name__
    return {"status": "failed", "detail": str(reason), **details}


def _queue_names() -> tuple[str, str, str]:
    default_queue = str(config.get("celery_default_queue", "celery") or "celery")
    return default_queue, CRAWLER_QUEUE, EMBEDDER_QUEUE


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
    broker_url = f"{config['celery_redis_uri']}{config['celery_broker_db']}"
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
    timeout_seconds = float(config.get("readiness_celery_timeout_seconds", 1.0))
    try:
        worker_queues = await asyncio.to_thread(
            _inspect_celery_workers,
            timeout_seconds,
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


async def _check_embedder_workers() -> CheckResult:
    timeout_seconds = float(config.get("readiness_celery_timeout_seconds", 1.0))
    try:
        worker_queues = await asyncio.to_thread(
            _inspect_celery_workers,
            timeout_seconds,
        )
    except Exception as exc:
        return _failed(exc)

    embedder_workers = [
        worker for worker, queues in worker_queues.items() if EMBEDDER_QUEUE in queues
    ]
    if not embedder_workers:
        return _failed(
            f"no workers listen to {EMBEDDER_QUEUE}",
            workers=worker_queues,
            required_queue=EMBEDDER_QUEUE,
        )
    return _ok(workers=embedder_workers)


def _is_model_loaded(value: Any) -> bool:
    return value is not None and value is not False


async def _check_embedding_model() -> CheckResult:
    model_dir = Path(str(config["embedding_model_dir"]))
    try:
        device = resolve_embedding_device()
    except Exception as exc:
        return _failed(exc, model_dir=str(model_dir))

    if not model_dir.exists():
        return _failed("model directory missing", model_dir=str(model_dir), device=device)

    loaded = {
        "embedding": _is_model_loaded(getattr(chat_ctx, "_embed_model", None)),
        "reranker": _is_model_loaded(getattr(chat_ctx, "_rerank_model", None)),
    }
    if not all(loaded.values()):
        return _failed(
            "models not warmed up",
            model_dir=str(model_dir),
            device=device,
            loaded=loaded,
        )

    return _ok(model_dir=str(model_dir), device=device, loaded=loaded)


async def _check_llm_config() -> CheckResult:
    provider_id = str(config.get("chat_provider") or "").strip()
    model_id = str(config.get("chat_model") or "").strip()
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
        "embedder": await _check_embedder_workers(),
        "embedding_model": await _check_embedding_model(),
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
