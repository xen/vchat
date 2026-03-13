import os
import sys
from pathlib import Path

import sentry_sdk
from celery import Celery, signals
from celery.signals import task_postrun
from sentry_sdk.integrations.aiohttp import AioHttpIntegration
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.redis import RedisIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

from core.settings import config

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
app.autodiscover_tasks(["jobs", "jobs.crawler", "jobs.embedder", "jobs.stripe"])


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
}


@signals.celeryd_init.connect
def init_sentry(**_kwargs):
    sconf = config.get("sentry", {})
    sentry_sdk.init(
        dsn=sconf.get("sdk_celery"),
        integrations=[
            AioHttpIntegration(),
            SqlalchemyIntegration(),
            RedisIntegration(),
            CeleryIntegration(monitor_beat_tasks=True),
        ],
        traces_sample_rate=sconf.get("trace_rate", 0.0),
        profiles_sample_rate=sconf.get("profile_rate", 0.0),
    )


@task_postrun.connect
def run_gc(*_, **__):
    import gc

    gc.collect()


# Import tasks to register them with Celery when autodiscovery is limited
try:
    import jobs  # noqa: F401
    import jobs.content  # noqa: F401
    import jobs.sitemap  # noqa: F401
    import jobs.suggestions  # noqa: F401
except ImportError:
    pass  # jobs package might not be available in minimal environments
