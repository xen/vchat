from __future__ import annotations

import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

from jobs.db import create_sync_engine
from vchat.models import Base


EXCLUDED_TABLES = {
    "celery_taskmeta",
    "celery_tasksetmeta",
}


def _single_alembic_head() -> str:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    assert len(heads) == 1
    return heads[0]


def test_database_is_at_alembic_head() -> None:
    engine = create_sync_engine()
    with engine.connect() as conn:
        current_revision = conn.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one()

    assert current_revision == _single_alembic_head()


def test_database_contains_all_model_columns() -> None:
    engine = create_sync_engine()
    inspector = sa.inspect(engine)

    missing: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name in EXCLUDED_TABLES or table.info.get("is_view", False):
            continue
        database_columns = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name not in database_columns:
                missing.append(f"{table.name}.{column.name}")

    assert missing == []
