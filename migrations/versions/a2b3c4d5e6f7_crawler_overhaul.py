"""crawler_overhaul

Revision ID: a2b3c4d5e6f7
Revises: c1d2e3f4a5b6
Create Date: 2026-05-31

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "a2b3c4d5e6f7"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()

    # 1. Truncate chunk and document tables
    bind.execute(sa.text("TRUNCATE TABLE chunk CASCADE"))
    bind.execute(sa.text("TRUNCATE TABLE document CASCADE"))

    # 2. Add new status enum values
    new_status_values = [
        "pending",
        "unchanged",
        "ok",
        "error_4xx",
        "error_5xx",
        "blocked",
        "redirect",
        "no_content",
        "excluded_robots",
        "excluded_rules",
        "excluded_auth",
        "excluded_ignored",
    ]
    for val in new_status_values:
        bind.execute(
            sa.text(f"ALTER TYPE status ADD VALUE IF NOT EXISTS '{val}'")
        )

    # 3. Add new columns to document
    op.add_column("document", sa.Column("http_status", sa.Integer(), nullable=True))
    op.add_column(
        "document",
        sa.Column(
            "last_crawled_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )
    op.add_column("document", sa.Column("last_etag", sa.Text(), nullable=True))
    op.add_column(
        "document",
        sa.Column(
            "last_modified_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "document",
        sa.Column(
            "check_interval_days",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("7"),
        ),
    )
    op.add_column(
        "document",
        sa.Column(
            "stable_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "document",
        sa.Column(
            "error_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "document",
        sa.Column(
            "is_hub_page",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "document",
        sa.Column("content_value", sa.Float(), nullable=True),
    )
    op.add_column(
        "document",
        sa.Column(
            "inlink_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    # 4. Rename document -> page
    op.rename_table("document", "page")

    # 5. Rename chunk.document_id -> chunk.page_id and update FK
    op.drop_constraint("chunk_document_id_fkey", "chunk", type_="foreignkey")
    op.alter_column("chunk", "document_id", new_column_name="page_id")
    op.create_foreign_key(
        "chunk_page_id_fkey",
        "chunk",
        "page",
        ["page_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # 6. Create sitemap table
    op.create_table(
        "sitemap",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("source.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column(
            "is_excluded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "discovered_via",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'manual'"),
        ),
        sa.Column(
            "first_seen_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("last_fetched_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_etag", sa.Text(), nullable=True),
        sa.Column("last_content_hash", sa.Text(), nullable=True),
        sa.Column("url_count", sa.Integer(), nullable=True),
    )

    # 7. Migrate data from source.sitemaps to sitemap table
    bind.execute(
        sa.text(
            """
            INSERT INTO sitemap (source_id, url, discovered_via)
            SELECT id, unnest(sitemaps), 'manual'
            FROM source
            WHERE sitemaps IS NOT NULL AND array_length(sitemaps, 1) > 0
            """
        )
    )

    # 8. Drop source.sitemaps column
    op.drop_column("source", "sitemaps")

    # 9. Create page_link table
    op.create_table(
        "page_link",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("target_uri", sa.Text(), nullable=False),
        sa.Column(
            "source_page_id",
            sa.Integer(),
            sa.ForeignKey("page.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "target_page_id",
            sa.Integer(),
            sa.ForeignKey("page.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("target_status", sa.String(32), nullable=True),
        sa.Column(
            "found_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    # 10. Create crawl_run table
    op.create_table(
        "crawl_run",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("source.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "pages_crawled",
            sa.Integer(),
            nullable=True,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "pages_new",
            sa.Integer(),
            nullable=True,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "pages_changed",
            sa.Integer(),
            nullable=True,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "pages_errors",
            sa.Integer(),
            nullable=True,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "pages_excluded",
            sa.Integer(),
            nullable=True,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "was_rate_limited",
            sa.Boolean(),
            nullable=True,
            server_default=sa.text("false"),
        ),
        sa.Column("exit_reason", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
    )

    # 11. Add robots_cache to source
    op.add_column(
        "source",
        sa.Column(
            "robots_cache",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade():
    # NOTE: downgrade reverses schema changes but does NOT restore truncated data.
    # NOTE: PostgreSQL does not support removing enum values, so the new status
    # values added in upgrade() cannot be removed. A full recreate of the enum
    # type would be required, which is complex and risky in production.

    bind = op.get_bind()

    # Remove robots_cache from source
    op.drop_column("source", "robots_cache")

    # Drop crawl_run
    op.drop_table("crawl_run")

    # Drop page_link
    op.drop_table("page_link")

    # Re-add sitemaps to source
    op.add_column(
        "source",
        sa.Column(
            "sitemaps",
            sa.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    # We cannot restore data from sitemap -> source.sitemaps (data was truncated)
    op.drop_table("sitemap")

    # Rename page -> document
    op.rename_table("page", "document")

    # Rename chunk.page_id -> chunk.document_id
    op.drop_constraint("chunk_page_id_fkey", "chunk", type_="foreignkey")
    op.alter_column("chunk", "page_id", new_column_name="document_id")
    op.create_foreign_key(
        "chunk_document_id_fkey",
        "chunk",
        "document",
        ["document_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Drop columns added to document (now renamed back)
    op.drop_column("document", "inlink_count")
    op.drop_column("document", "content_value")
    op.drop_column("document", "is_hub_page")
    op.drop_column("document", "error_count")
    op.drop_column("document", "stable_count")
    op.drop_column("document", "check_interval_days")
    op.drop_column("document", "last_modified_at")
    op.drop_column("document", "last_etag")
    op.drop_column("document", "last_crawled_at")
    op.drop_column("document", "http_status")
