"""Require TOTP for every local administrator, revoking pre-TOTP sessions."""

import sqlalchemy as sa
from alembic import op

revision = "u5v6w7x8y9z0"
down_revision = "t4u5v6w7x8y9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_totp",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("secret", sa.String()),
        sa.Column("pending_secret", sa.String()),
        sa.Column("token_hash", sa.String(64), unique=True),
        sa.Column("password_digest", sa.String()),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("failure_window_at", sa.DateTime(timezone=True)),
        sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_counter", sa.BigInteger()),
    )
    op.add_column(
        "user_session",
        sa.Column(
            "totp_verified", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.execute("""UPDATE user_session SET revoked_at=now(), revoked_reason='mandatory_totp', updated_at=now()
                  WHERE revoked_at IS NULL AND user_id IN (SELECT id FROM users WHERE NOT is_ldap)""")


def downgrade():
    raise RuntimeError(
        "Mandatory TOTP cannot be downgraded to password-only authentication"
    )
