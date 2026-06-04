from jobs.celery import app as celery_app


def test_boilerplate_rebuild_task_is_registered_for_embedder_queue() -> None:
    celery_app.loader.import_default_modules()

    task = celery_app.tasks["jobs.embedder.tasks.rebuild_boilerplate_index"]

    assert task.name == "jobs.embedder.tasks.rebuild_boilerplate_index"
    assert task.queue == "embeddings"
