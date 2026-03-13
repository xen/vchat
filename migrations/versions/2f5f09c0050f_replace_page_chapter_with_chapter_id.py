"""replace_page_chapter_with_chapter_id

Revision ID: 2f5f09c0050f
Revises: b30757544984
Create Date: 2025-12-14 22:25:19.913529

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "2f5f09c0050f"
down_revision = "b30757544984"
branch_labels = None
depends_on = None


def upgrade():
    # 1. Add chapter_id column as nullable first
    op.add_column("support_pages", sa.Column("chapter_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_support_pages_chapter_id",
        "support_pages",
        "support_chapters",
        ["chapter_id"],
        ["id"],
    )

    # 2. Migrate data
    op.execute("""
        UPDATE support_pages
        SET chapter_id = (
            SELECT chapter_id
            FROM support_page_chapters
            WHERE support_page_chapters.page_id = support_pages.id
            ORDER BY "order" ASC
            LIMIT 1
        )
    """)

    # 3. Clean up orphans and enforce not null
    op.execute("DELETE FROM support_pages WHERE chapter_id IS NULL")
    op.alter_column("support_pages", "chapter_id", nullable=False)

    # 4. Drop old table
    op.drop_table("support_page_chapters")


def downgrade():
    # 1. Recreate table
    op.create_table(
        "support_page_chapters",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("page_id", sa.Integer(), nullable=False),
        sa.Column("chapter_id", sa.Integer(), nullable=False),
        sa.Column("lang", sa.String(length=10), nullable=True),
        sa.Column("order", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["chapter_id"],
            ["support_chapters.id"],
        ),
        sa.ForeignKeyConstraint(
            ["page_id"],
            ["support_pages.id"],
        ),
    )

    # 2. Restore data
    op.execute("""
        INSERT INTO support_page_chapters (page_id, chapter_id, lang, "order")
        SELECT id, chapter_id, lang, 0 FROM support_pages
    """)

    # 3. Remove column
    op.drop_constraint(
        "fk_support_pages_chapter_id", "support_pages", type_="foreignkey"
    )
    op.drop_column("support_pages", "chapter_id")
