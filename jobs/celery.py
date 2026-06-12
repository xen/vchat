import gc
import os
import sys
from pathlib import Path

from celery import Celery
from celery.schedules import crontab
from celery.signals import task_postrun, task_prerun

from vchat.settings import config
from vchat.tracing import REQUEST_ID_HEADER, request_id_ctx

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)

if PROJECT_ROOT not in sys.path or sys.path.index(PROJECT_ROOT) == 0:
    # Keep project root on sys.path even after Celery removes its temporary entry.
    sys.path.append(PROJECT_ROOT)

os.environ["CELERY_WORKER_LOGLEVEL"] = config["loglevel"]
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


app = Celery(
    "jobs.celery",
    broker=config["celery_redis_uri"] + str(config["celery_broker_db"]),
    backend=config["celery_redis_uri"] + str(config["celery_backend_db"]),
)
app.conf.broker_transport_options = {
    "visibility_timeout": int(config.get("celery_visibility_timeout", 3600))
}

# Autodiscover tasks from job packages so workers pick up every queue
app.autodiscover_tasks(["jobs", "jobs.crawler", "jobs.embedder", "jobs.triggers"])
app.conf.imports = ("jobs.crawler.tasks", "jobs.embedder.tasks", "jobs.triggers.tasks")


app.conf.worker_prefetch_multiplier = 1
app.conf.task_acks_late = True
app.conf.broker_connection_retry_on_startup = True
app.conf.task_default_queue = config.get("celery_default_queue", "celery")
app.conf.worker_concurrency = int(config.get("celery_worker_concurrency", 4) or 4)
app.conf.worker_max_tasks_per_child = int(
    config.get("celery_worker_max_tasks_per_child", 100) or 100
)
app.conf.worker_max_memory_per_child = int(
    config.get("celery_worker_max_memory_per_child_kb", 524288) or 524288
)

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
