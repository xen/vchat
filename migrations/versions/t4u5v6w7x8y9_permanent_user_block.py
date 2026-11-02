"""Retain users permanently and distinguish irreversible blocking."""

import sqlalchemy as sa
from alembic import op

revision = "t4u5v6w7x8y9"
down_revision = "s3t4u5v6w7x8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("blocked_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "ck_users_blocked_inactive", "users", "blocked_at IS NULL OR NOT is_active"
    )
    op.execute("""
        CREATE FUNCTION enforce_user_retention() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
                RAISE EXCEPTION 'Physical user deletion is forbidden';
            END IF;
            IF OLD.blocked_at IS NOT NULL AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'Permanently blocked users are immutable';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER users_retention BEFORE UPDATE OR DELETE ON users
        FOR EACH ROW EXECUTE FUNCTION enforce_user_retention()
    """)

    op.execute("""
        CREATE TRIGGER users_no_truncate BEFORE TRUNCATE ON users
        FOR EACH STATEMENT EXECUTE FUNCTION enforce_user_retention()
    """)


def downgrade() -> None:
    # Dropping this status would restore blocked accounts. Never erase it.
    raise RuntimeError("Permanent user blocking cannot be downgraded")
