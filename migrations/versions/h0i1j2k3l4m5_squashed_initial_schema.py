"""squashed initial schema

Revision ID: h0i1j2k3l4m5
Revises:
Create Date: 2026-06-14 00:00:00.000000
"""

from pathlib import Path

import sqlparse
from alembic import op


revision = "h0i1j2k3l4m5"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql_file = Path(__file__).with_name("h0i1j2k3l4m5_squashed_initial_schema.sql")
    bind = op.get_bind()
    for statement in sqlparse.split(sql_file.read_text()):
        bind.exec_driver_sql(statement)


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP SCHEMA public CASCADE")
    bind.exec_driver_sql("CREATE SCHEMA public")
