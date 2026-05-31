"""page status refactor: merge status+index_status, add status_error, drop is_ignored

Revision ID: b2c3d4e5f6a7
Revises: 7c8e9f1a2b3c
Create Date: 2026-05-31 12:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "7c8e9f1a2b3c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

new_status_enum = sa.Enum("crawler", "parsing", "ready", name="status")


def upgrade() -> None:
    # 1. Add status_error column
    op.add_column(
        "page",
        sa.Column("status_error", sa.String(64), nullable=True),
    )

    # 2. Backfill status_error from old status values
    op.execute("""
        UPDATE page SET status_error = 'http_4xx'
        WHERE status = 'error_4xx'
    """)
    op.execute("""
        UPDATE page SET status_error = 'http_5xx'
        WHERE status IN ('error_5xx', 'blocked')
    """)
    op.execute("""
        UPDATE page SET status_error = 'redirect'
        WHERE status = 'redirect'
    """)
    op.execute("""
        UPDATE page SET status_error = 'excluded_auth'
        WHERE status = 'excluded_auth'
    """)
    op.execute("""
        UPDATE page SET status_error = 'excluded_ignored'
        WHERE status = 'excluded_ignored' OR is_ignored = true
    """)
    op.execute("""
        UPDATE page SET status_error = 'no_content'
        WHERE status = 'no_content'
    """)
    op.execute("""
        UPDATE page SET status_error = 'low_content'
        WHERE status = 'low_content'
    """)
    op.execute("""
        UPDATE page SET status_error = 'excluded_robots'
        WHERE status = 'excluded_robots'
    """)
    op.execute("""
        UPDATE page SET status_error = 'excluded_rules'
        WHERE status = 'excluded_rules'
    """)

    # 3. Migrate status column: drop old ENUM, create new one
    # First set a temp varchar column
    op.add_column("page", sa.Column("status_new", sa.String(16), nullable=True))

    op.execute("""
        UPDATE page SET status_new = CASE
            WHEN status IN ('pending', 'added') THEN 'crawler'
            WHEN status IN ('error_4xx', 'error_5xx', 'blocked', 'redirect',
                            'excluded_auth', 'excluded_ignored', 'no_content',
                            'low_content', 'excluded_robots', 'excluded_rules') THEN 'crawler'
            WHEN status IN ('ok', 'unchanged', 'indexed') THEN 'parsing'
            ELSE 'crawler'
        END
    """)

    # Drop old status column (and its index)
    op.drop_index("ix_document_status", table_name="page")
    op.drop_column("page", "status")

    # Rename new column
    op.alter_column("page", "status_new", new_column_name="status", nullable=False,
                    server_default="crawler")

    # Drop old ENUM type, create new one, alter column type
    op.execute("ALTER TABLE page ALTER COLUMN status DROP DEFAULT")
    op.execute("DROP TYPE IF EXISTS status")
    op.execute("CREATE TYPE status AS ENUM ('crawler', 'parsing', 'ready')")
    op.execute("ALTER TABLE page ALTER COLUMN status TYPE status USING status::status")
    op.execute("ALTER TABLE page ALTER COLUMN status SET DEFAULT 'crawler'::status")

    # Recreate index
    op.create_index("ix_page_status", "page", ["status"])

    # 4. Drop index_status column and its index
    op.drop_index("ix_page_index_status", table_name="page")
    op.drop_column("page", "index_status")

    # 5. Drop is_ignored column
    op.drop_column("page", "is_ignored")

    # 6. Add index on status_error
    op.create_index("ix_page_status_error", "page", ["status_error"])


def downgrade() -> None:
    # Not supported — no backward compatibility
    raise NotImplementedError("Downgrade not supported for page status refactor")
