"""add billing models

Revision ID: 5f74f0854dde
Revises: 3a8951a7d487
Create Date: 2025-11-30 20:09:21.846406

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "5f74f0854dde"
down_revision = "3a8951a7d487"
branch_labels = None
depends_on = None

PLAN_VALUES = ("free", "personal", "pro", "startup")
PAYMENT_STATUS_VALUES = ("pending", "succeeded", "failed", "canceled")


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    if bind is None:
        return False
    inspector = sa.inspect(bind)
    return inspector.has_table(table_name)


def _drop_table_if_exists(table_name: str) -> None:
    if _table_exists(table_name):
        op.drop_table(table_name)


def upgrade():
    plan_type = postgresql.ENUM(*PLAN_VALUES, name="plan")
    plan_type.create(op.get_bind(), checkfirst=True)
    payment_status_type = postgresql.ENUM(*PAYMENT_STATUS_VALUES, name="paymentstatus")
    payment_status_type.create(op.get_bind(), checkfirst=True)

    _drop_table_if_exists("payment")
    _drop_table_if_exists("subscription")
    _drop_table_if_exists("payment_method")

    op.create_table(
        "stripe_webhook_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=255), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("raw_body", sa.Text(), nullable=False),
        sa.Column("processed", sa.Boolean(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_stripe_webhook_log_processed"),
        "stripe_webhook_log",
        ["processed"],
        unique=False,
    )

    plan_column = postgresql.ENUM(*PLAN_VALUES, name="plan", create_type=False)
    op.add_column(
        "project",
        sa.Column(
            "plan",
            plan_column,
            nullable=False,
            server_default=sa.text("'free'"),
        ),
    )
    op.add_column(
        "project",
        sa.Column(
            "token_limit",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="Monthly token limit",
        ),
    )
    op.add_column(
        "project",
        sa.Column(
            "tokens_used",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="Tokens used in current month",
        ),
    )
    op.add_column(
        "project",
        sa.Column(
            "stripe_customer_id",
            sa.String(length=128),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )

    if _table_exists("user_data"):
        op.execute(
            sa.text(
                """
                UPDATE project
                SET plan = ud.plan,
                    token_limit = ud.token_limit,
                    tokens_used = ud.tokens_used,
                    stripe_customer_id = ud.stripe_customer_id
                FROM user_data ud
                WHERE project.id = ud.project_id
                """
            )
        )
        op.drop_index(
            op.f("ix_user_data_project_id"),
            table_name="user_data",
        )
        op.drop_table("user_data")

    payment_status_column = postgresql.ENUM(
        *PAYMENT_STATUS_VALUES,
        name="paymentstatus",
        create_type=False,
    )
    op.create_table(
        "payment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.String(length=50), nullable=False),
        sa.Column(
            "amount",
            sa.Numeric(precision=10, scale=2),
            nullable=False,
        ),
        sa.Column(
            "currency",
            sa.String(length=10),
            nullable=False,
            server_default=sa.text("'usd'"),
        ),
        sa.Column(
            "stripe_checkout_session_id",
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column(
            "stripe_payment_intent_id",
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column(
            "status",
            payment_status_column,
            nullable=False,
        ),
        sa.Column(
            "description",
            sa.String(length=255),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "end_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "stripe_subscription_id",
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_payment_project_id"),
        "payment",
        ["project_id"],
        unique=False,
    )
    op.create_unique_constraint(
        None,
        "payment",
        ["stripe_checkout_session_id"],
    )


def downgrade():
    _drop_table_if_exists("payment")

    plan_type = postgresql.ENUM(*PLAN_VALUES, name="plan")
    plan_type.create(op.get_bind(), checkfirst=True)

    if not _table_exists("user_data"):
        op.create_table(
            "user_data",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project_id", sa.Integer(), nullable=False),
            sa.Column(
                "token_limit",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
                comment="Monthly token limit",
            ),
            sa.Column(
                "tokens_used",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
                comment="Tokens used in current month",
            ),
            sa.Column(
                "plan",
                postgresql.ENUM(*PLAN_VALUES, name="plan"),
                nullable=False,
                server_default=sa.text("'free'"),
            ),
            sa.Column(
                "stripe_customer_id",
                sa.String(length=128),
                nullable=False,
                server_default=sa.text("''"),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            op.f("ix_user_data_project_id"),
            "user_data",
            ["project_id"],
            unique=True,
        )

    if _table_exists("project"):
        op.execute(
            sa.text(
                """
                INSERT INTO user_data (
                    project_id,
                    token_limit,
                    tokens_used,
                    plan,
                    stripe_customer_id,
                    created_at,
                    updated_at
                )
                SELECT
                    id,
                    token_limit,
                    tokens_used,
                    plan,
                    stripe_customer_id,
                    CURRENT_TIMESTAMP,
                    updated_at
                FROM project
                """
            )
        )

    op.drop_column("project", "stripe_customer_id")
    op.drop_column("project", "tokens_used")
    op.drop_column("project", "token_limit")
    op.drop_column("project", "plan")

    if _table_exists("stripe_webhook_log"):
        op.drop_index(
            op.f("ix_stripe_webhook_log_processed"),
            table_name="stripe_webhook_log",
        )
        op.drop_table("stripe_webhook_log")

    op.create_table(
        "subscription",
        sa.Column("id", sa.INTEGER(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.INTEGER(), autoincrement=False, nullable=False),
        sa.Column("plan_type", sa.VARCHAR(), autoincrement=False, nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "active",
                "canceled",
                "ended",
                "payment_failed",
                name="subscriptionstatus",
            ),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column(
            "external_subscription_id", sa.VARCHAR(), autoincrement=False, nullable=True
        ),
        sa.Column("is_auto_renew", sa.BOOLEAN(), autoincrement=False, nullable=False),
        sa.Column(
            "start_date", postgresql.TIMESTAMP(), autoincrement=False, nullable=False
        ),
        sa.Column(
            "end_date", postgresql.TIMESTAMP(), autoincrement=False, nullable=False
        ),
        sa.Column(
            "canceled_at", postgresql.TIMESTAMP(), autoincrement=False, nullable=True
        ),
        sa.Column(
            "payment_method_id", sa.INTEGER(), autoincrement=False, nullable=True
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            autoincrement=False,
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["payment_method_id"],
            ["payment_method.id"],
            name="subscription_payment_method_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="subscription_user_id_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="subscription_pkey"),
    )
    op.create_table(
        "payment_method",
        sa.Column("id", sa.INTEGER(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.INTEGER(), autoincrement=False, nullable=False),
        sa.Column(
            "provider",
            postgresql.ENUM(
                "stripe",
                "paypal",
                "wayforpay",
                name="paymentprovider",
            ),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column("provider_token", sa.VARCHAR(), autoincrement=False, nullable=False),
        sa.Column(
            "created_at", postgresql.TIMESTAMP(), autoincrement=False, nullable=False
        ),
        sa.Column("is_active", sa.BOOLEAN(), autoincrement=False, nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="payment_method_user_id_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="payment_method_pkey"),
    )
    op.create_table(
        "payment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column(
            "provider",
            postgresql.ENUM(
                "stripe",
                "paypal",
                "wayforpay",
                name="paymentprovider",
            ),
            nullable=False,
        ),
        sa.Column("external_transaction_id", sa.String(), nullable=False),
        sa.Column("amount_coin", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("paid_at", sa.DateTime(), nullable=False),
        sa.Column("is_successful", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscription.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_payment_external_transaction_id"),
        "payment",
        ["external_transaction_id"],
        unique=False,
    )
