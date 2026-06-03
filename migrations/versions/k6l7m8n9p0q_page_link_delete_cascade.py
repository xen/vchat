"""switch page_link page fks to delete cascade

Revision ID: k6l7m8n9p0q
Revises: j5k6l7m8n9p
Create Date: 2026-06-03 18:45:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "k6l7m8n9p0q"
down_revision: Union[str, None] = "j5k6l7m8n9p"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "page_link_source_page_id_fkey",
        "page_link",
        type_="foreignkey",
    )
    op.drop_constraint(
        "page_link_target_page_id_fkey",
        "page_link",
        type_="foreignkey",
    )

    op.create_foreign_key(
        "page_link_source_page_id_fkey",
        "page_link",
        "page",
        ["source_page_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "page_link_target_page_id_fkey",
        "page_link",
        "page",
        ["target_page_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "page_link_source_page_id_fkey",
        "page_link",
        type_="foreignkey",
    )
    op.drop_constraint(
        "page_link_target_page_id_fkey",
        "page_link",
        type_="foreignkey",
    )

    op.create_foreign_key(
        "page_link_source_page_id_fkey",
        "page_link",
        "page",
        ["source_page_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "page_link_target_page_id_fkey",
        "page_link",
        "page",
        ["target_page_id"],
        ["id"],
        ondelete="SET NULL",
    )
