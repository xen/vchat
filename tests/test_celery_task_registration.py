from jobs.celery import app as celery_app


def test_boilerplate_rebuild_task_is_registered_for_crawler_queue() -> None:
    celery_app.loader.import_default_modules()

    task = celery_app.tasks["jobs.crawler.tasks.rebuild_boilerplate_index"]

    assert task.name == "jobs.crawler.tasks.rebuild_boilerplate_index"
    assert task.queue == "celery"
    assert "jobs.embedder.tasks.rebuild_boilerplate_index" not in celery_app.tasks
