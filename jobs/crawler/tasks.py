import json
import subprocess
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobs.celery import app
from jobs.db import create_sync_engine
from vchat.models.data import Project, Source

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@app.task(
    name="jobs.crawler.tasks.crawl_source_task",
    queue="crawler",
)
def crawl_source_task(source_id: int):
    print(f"Starting crawl for source {source_id}")

    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            stmt = (
                select(Source, Project.crawl_page_limit)
                .join(Project, Project.id == Source.project_id)
                .where(Source.id == source_id)
            )
            row = session.execute(stmt).one_or_none()

            if not row:
                print(f"Source {source_id} not found")
                return

            source, crawl_page_limit = row
            source_type = source.type
            url = source.uri
            source_title = source.title
            source_config = dict(source.config or {})
    finally:
        engine.dispose()

    # Route to appropriate crawler based on source type
    if source_type == "s3":
        # Use S3 crawler
        print(f"Using S3 crawler for source {source_id}")
        runner_cmd = [
            sys.executable,
            "-m",
            "jobs.crawler.s3_crawler",
            str(source_id),
        ]
    elif source_type == "google_drive":
        # Use Google Drive crawler
        print(f"Using Google Drive crawler for source {source_id}")
        runner_cmd = [
            sys.executable,
            "-m",
            "jobs.crawler.google_drive_crawler",
            str(source_id),
        ]
    else:
        # Use Scrapy crawler for site/custom sources
        print(
            f"Using Scrapy crawler for source [{source_id}]: '{source_title}' ({url})"
        )

        config_json = json.dumps(source_config)
        runner_cmd = [
            sys.executable,
            "-m",
            "jobs.crawler.crawler_runner",
            url,
            str(source_id),
            str(crawl_page_limit),
            source_type,
            config_json,
        ]
        print(f"Indexing source {source_id} complete")

    result = subprocess.run(
        runner_cmd,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )

    if result.returncode != 0:
        print(f"Crawler failed with exit code {result.returncode}")
    else:
        # Chain refresh_project_index
        from jobs.embedder.tasks import refresh_project_index

        print(f"Triggering refresh_project_index for project {source.project_id}")
        refresh_project_index.delay(source.project_id)

    print(f"Finished crawling source {source_id}")


@app.task(
    name="jobs.crawler.tasks.crawl_all_sources_task",
    queue="crawler",
)
def crawl_all_sources_task(project_id: int):
    """
    Crawl all sources for a given project.
    """
    print(f"Starting crawl for all sources in project {project_id}")

    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            stmt = select(Source).where(
                Source.project_id == project_id, Source.type != "upload"
            )
            sources = session.execute(stmt).scalars().all()

            if not sources:
                print(f"No sources found for project {project_id}")
                return

            source_ids = [source.id for source in sources]
    finally:
        engine.dispose()

    print(f"Found {len(source_ids)} sources to crawl")

    # Trigger crawl_source_task for each source
    for source_id in source_ids:
        crawl_source_task.delay(source_id)

    print(f"Finished queueing crawl tasks for project {project_id}")
