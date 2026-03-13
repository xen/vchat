"""Initial db setup

Revision ID: f5890fb79af1
Revises:
Create Date: 2025-01-17 23:57:51.313523

"""

from alembic import op

from pathlib import Path
import sqlparse


# revision identifiers, used by Alembic.
revision = "46f2418a40c2"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    statements = sqlparse.split(
        Path(__file__).with_name("_schema.sql").read_text(encoding="utf-8")
    )
    for statement in statements:
        if command := statement.strip():
            op.execute(command)

    statements = sqlparse.split(
        Path(__file__).with_name("_sqids.sql").read_text(encoding="utf-8")
    )
    for statement in statements:
        if command := statement.strip():
            op.execute(command)

    # shufled alphabet from https://sqids.org/playground
    op.execute(
        """
    CREATE OR REPLACE FUNCTION public.trigger_set_short_id()
        RETURNS TRIGGER AS $$
    BEGIN
        IF NEW.short_id IS NULL THEN
            NEW.short_id = sqids.encode(array[NEW.id], 'xMXvWAEb78OqjrtGKwzhT51aFeLmuQ9PJsNSi24BgYdfn6HVoZ0pkUy3RDCclI', 10);
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;"""
    )
    op.execute(
        """
    CREATE TRIGGER set_short_id
    BEFORE INSERT OR UPDATE
    ON public.post
    FOR EACH ROW
    EXECUTE FUNCTION public.trigger_set_short_id();
    """
    )
    op.execute("""set search_path to public;""")


def downgrade():
    pass
