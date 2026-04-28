"""replace source reindex period with cron expression

Revision ID: b8e4d2a1c9f0
Revises: 9a6c3f7b4d21
Create Date: 2026-04-29 10:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "b8e4d2a1c9f0"
down_revision: Union[str, Sequence[str], None] = "9a6c3f7b4d21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SOURCE_REINDEX_ENUM = postgresql.ENUM(
    "weekly",
    "monthly",
    "manual",
    name="sourcereindexperiod",
)


def upgrade() -> None:
    op.add_column(
        "source",
        sa.Column(
            "reindex_cron",
            sa.String(length=100),
            nullable=False,
            server_default=sa.text("'0 3 * * 1'"),
        ),
    )

    op.execute(
        """
        UPDATE source
        SET reindex_cron = CASE reindex_period
            WHEN 'monthly' THEN '0 3 1 * *'
            ELSE '0 3 * * 1'
        END
        """
    )

    op.drop_column("source", "reindex_period")

    bind = op.get_bind()
    _SOURCE_REINDEX_ENUM.drop(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    _SOURCE_REINDEX_ENUM.create(bind, checkfirst=True)

    op.add_column(
        "source",
        sa.Column(
            "reindex_period",
            sa.Enum(
                "weekly",
                "monthly",
                "manual",
                name="sourcereindexperiod",
                create_type=False,
            ),
            nullable=False,
            server_default=sa.text("'weekly'"),
        ),
    )

    op.execute(
        """
        UPDATE source
        SET reindex_period = CASE
            WHEN reindex_cron = '0 3 1 * *' THEN 'monthly'
            ELSE 'weekly'
        END
        """
    )

    op.drop_column("source", "reindex_cron")
