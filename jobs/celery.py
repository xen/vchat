import gc
import os
import sys
from pathlib import Path

from celery import Celery
from celery.schedules import crontab
from celery.signals import task_postrun

from vchat.settings import config

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)

if PROJECT_ROOT not in sys.path or sys.path.index(PROJECT_ROOT) == 0:
    # Keep project root on sys.path even after Celery removes its temporary entry.
    sys.path.append(PROJECT_ROOT)

os.environ["CELERY_WORKER_LOGLEVEL"] = config["loglevel"]


app = Celery(
    "jobs.celery",
    broker=config["celery_redis_uri"] + str(config["celery_broker_db"]),
    backend=config["celery_redis_uri"] + str(config["celery_backend_db"]),
)
app.conf.broker_transport_options = {
    "visibility_timeout": int(config.get("celery_visibility_timeout", 3600))
}

# Autodiscover tasks from job packages so workers pick up every queue
app.autodiscover_tasks(["jobs", "jobs.crawler", "jobs.embedder"])


app.conf.worker_prefetch_multiplier = 1
app.conf.task_acks_late = True
app.conf.broker_connection_retry_on_startup = True

app.conf.beat_schedule = {
    "sitemap_generate": {
        "task": "seo.sitemap_generate",
        "schedule": 86400.0,  # 24 hours
    },
    "process_pending_chunks": {
        "task": "jobs.embedder.tasks.pending_chunks",
        "schedule": 300.0,
        "options": {"queue": "embeddings"},
    },
    "schedule_source_reindex": {
        "task": "jobs.crawler.tasks.schedule_reindex_sources_task",
        "schedule": crontab(),
        "options": {"queue": "crawler"},
    },
}


@task_postrun.connect
def run_gc(*_, **__):
    gc.collect()


# Import tasks to register them with Celery when autodiscovery is limited
try:
    import jobs  # noqa: F401
    import jobs.content  # noqa: F401
    import jobs.sitemap  # noqa: F401
    import jobs.suggestions  # noqa: F401
except ImportError:
    pass  # jobs package might not be available in minimal environments
