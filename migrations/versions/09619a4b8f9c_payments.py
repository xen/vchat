"""payments

Revision ID: 09619a4b8f9c
Revises: 9153faf3cc33
Create Date: 2025-04-26 22:57:18.210925

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "09619a4b8f9c"
down_revision = "46f2418a40c2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "payment_method",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "provider",
            sa.Enum("stripe", "paypal", "wayforpay", name="paymentprovider"),
            nullable=False,
        ),
        sa.Column("provider_token", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "subscription",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("plan_type", sa.String(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "canceled",
                "ended",
                "payment_failed",
                name="subscriptionstatus",
            ),
            nullable=False,
        ),
        sa.Column("external_subscription_id", sa.String(), nullable=True),
        sa.Column("is_auto_renew", sa.Boolean(), nullable=False),
        sa.Column("start_date", sa.DateTime(), nullable=False),
        sa.Column("end_date", sa.DateTime(), nullable=False),
        sa.Column("canceled_at", sa.DateTime(), nullable=True),
        sa.Column("payment_method_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["payment_method_id"],
            ["payment_method.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "payment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column(
            "provider",
            sa.Enum("stripe", "paypal", "wayforpay", name="paymentprovider"),
            nullable=False,
        ),
        sa.Column("external_transaction_id", sa.String(), nullable=False),
        sa.Column("amount_coin", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("paid_at", sa.DateTime(), nullable=False),
        sa.Column("is_successful", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscription.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_payment_external_transaction_id"),
        "payment",
        ["external_transaction_id"],
        unique=False,
    )


def downgrade():
    op.drop_index(op.f("ix_payment_external_transaction_id"), table_name="payment")
    op.drop_table("payment")
    op.drop_table("subscription")
    op.drop_table("payment_method")
