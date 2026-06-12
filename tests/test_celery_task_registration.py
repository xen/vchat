from types import SimpleNamespace

from jobs.celery import app as celery_app
from jobs import celery
from vchat.tracing import get_request_id


def test_boilerplate_rebuild_task_is_registered_for_crawler_queue() -> None:
    celery_app.loader.import_default_modules()

    task = celery_app.tasks["jobs.crawler.tasks.rebuild_boilerplate_index"]

    assert task.name == "jobs.crawler.tasks.rebuild_boilerplate_index"
    assert task.queue == "celery"
    assert "jobs.embedder.tasks.rebuild_boilerplate_index" not in celery_app.tasks


def test_celery_request_id_binding_uses_signal_task_id() -> None:
    task = SimpleNamespace(
        request=SimpleNamespace(headers={"x-request-id": "req-worker-1"})
    )

    celery.bind_request_id(task_id="task-1", task=task)
    try:
        assert get_request_id() == "req-worker-1"
    finally:
        celery.unbind_request_id(task_id="task-1")

    assert get_request_id() is None
