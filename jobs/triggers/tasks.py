from __future__ import annotations

import logging
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobs.celery import app
from jobs.db import create_sync_engine
from jobs.triggers.generation import generate_trigger_texts_for_page
from vchat.models import Page
from vchat.views.projects.page_status import PageStatus
from vchat.views.triggers.rules import build_page_trigger_items


def pages_for_trigger_generation(session: Session, limit: int = 20) -> list[Page]:
    return list(
        session.execute(
            select(Page)
            .where(Page.has_triggers.is_(True))
            .where(Page.uri.is_not(None))
            .where(Page.content.is_not(None))
            .where(Page.content != "")
            .where(Page.status == PageStatus.ready)
            .where(Page.status_error.is_(None))
            .where(sa.or_(Page.triggers.is_(None), Page.triggers == []))
            .order_by(Page.updated_at.desc().nullslast(), Page.id.desc())
            .limit(limit)
        ).scalars()
    )


@app.task(name="jobs.triggers.tasks.generate_missing_triggers_task", queue="celery")
def generate_missing_triggers_task(limit: int = 20) -> int:
    engine = create_sync_engine()
    created = 0
    try:
        with Session(bind=engine) as session:
            pages = pages_for_trigger_generation(session, limit=limit)
            for page in pages:
                texts = generate_trigger_texts_for_page(page)
                items = build_page_trigger_items(texts, source="generated")
                page.triggers = items
                page.updated_at = datetime.now(timezone.utc)
                created += len(items)
            session.commit()
    finally:
        engine.dispose()
    logging.info("Generated %s trigger items", created)
    return created
