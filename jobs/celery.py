import gc
import os
import sys
from pathlib import Path

from celery import Celery
from celery.schedules import crontab
from celery.signals import task_postrun, task_prerun

from vchat.settings import cfg
from vchat.tracing import REQUEST_ID_HEADER, request_id_ctx

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)

if PROJECT_ROOT not in sys.path or sys.path.index(PROJECT_ROOT) == 0:
    # Keep project root on sys.path even after Celery removes its temporary entry.
    sys.path.append(PROJECT_ROOT)

os.environ["CELERY_WORKER_LOGLEVEL"] = cfg.loglevel
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


app = Celery(
    "jobs.celery",
    broker=cfg.celery_redis_uri + str(cfg.celery_broker_db),
    backend=cfg.celery_redis_uri + str(cfg.celery_backend_db),
)
app.conf.broker_transport_options = {
    "visibility_timeout": cfg.celery_visibility_timeout
}

# The default worker only consumes the `celery` queue.  Do not import
# `jobs.embedder.tasks` here: importing its ML stack in every default worker
# needlessly duplicates PyTorch/SentenceTransformers memory.  The dedicated
# embedder worker includes that module explicitly in its command line.
app.conf.imports = ("jobs.crawler.tasks", "jobs.triggers.tasks")


app.conf.worker_prefetch_multiplier = 1
app.conf.task_acks_late = True
app.conf.broker_connection_retry_on_startup = True
app.conf.task_default_queue = cfg.celery_default_queue
app.conf.worker_concurrency = cfg.celery_worker_concurrency
app.conf.worker_max_tasks_per_child = cfg.celery_worker_max_tasks_per_child
app.conf.worker_max_memory_per_child = cfg.celery_worker_max_memory_per_child_kb

app.conf.beat_schedule = {
    "process_pending_chunks": {
        "task": "jobs.crawler.tasks.schedule_pending_chunks",
        "schedule": 300.0,
        "options": {"queue": app.conf.task_default_queue},
    },
    "schedule_source_reindex": {
        "task": "jobs.crawler.tasks.schedule_reindex_sources_task",
        "schedule": crontab(minute=0),
        "options": {"queue": app.conf.task_default_queue},
    },
    "schedule_sitemap_sync": {
        "task": "jobs.crawler.tasks.schedule_sitemap_sync_task",
        "schedule": crontab(minute=0, hour=3),
        "options": {"queue": app.conf.task_default_queue},
    },
}

_request_id_tokens = {}


@task_prerun.connect
def bind_request_id(*_, task_id=None, task=None, **__):
    if task is None:
        return
    headers = getattr(getattr(task, "request", None), "headers", None) or {}
    request_id = headers.get(REQUEST_ID_HEADER) or headers.get(
        REQUEST_ID_HEADER.lower()
    )
    if request_id:
        _request_id_tokens[task_id] = request_id_ctx.set(str(request_id))


@task_postrun.connect
def unbind_request_id(*_, task_id=None, **__):
    token = _request_id_tokens.pop(task_id, None)
    if token is not None:
        request_id_ctx.reset(token)


@task_postrun.connect
def run_gc(*_, **__):
    gc.collect()


# Task modules are loaded through Celery's loader via app.conf.imports.
