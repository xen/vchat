"""Add sitemap/list/google_drive/upload source types.

Revision ID: cf4c5734b329
Revises: a5420397d268
Create Date: 2025-12-07 04:30:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "cf4c5734b329"
down_revision = "a5420397d268"
branch_labels = None
depends_on = None


def _add_value(value: str) -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_enum e
                JOIN pg_type t ON t.oid = e.enumtypid
                WHERE t.typname = 'sourcetype' AND e.enumlabel = '{value}'
            ) THEN
                ALTER TYPE sourcetype ADD VALUE '{value}';
            END IF;
        END$$;
        """
    )


def upgrade():
    for value in ("sitemap", "list", "google_drive", "upload"):
        _add_value(value)


def downgrade():
    print(
        "Downgrading sourcetype enum values is not supported. "
        "Manual intervention is required if necessary."
    )
