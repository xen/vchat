import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, Created, Updated


class PaymentStatus(str):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


payment_status_enum = ENUM(
    "pending",
    "succeeded",
    "failed",
    "canceled",
    name="paymentstatus",
    create_type=False,
)


class Payment(Base, Created, Updated):
    __tablename__ = "payment"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        sa.Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    plan_id: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    amount: Mapped[float] = mapped_column(sa.Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(10), nullable=False, default="usd")

    stripe_checkout_session_id: Mapped[Optional[str]] = mapped_column(
        sa.String(255), unique=True, nullable=True
    )
    stripe_payment_intent_id: Mapped[Optional[str]] = mapped_column(
        sa.String(255), nullable=True
    )

    status: Mapped[str] = mapped_column(
        payment_status_enum,
        nullable=False,
        default=PaymentStatus.PENDING,
    )

    description: Mapped[str] = mapped_column(sa.String(255), nullable=False, default="")
    end_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    stripe_subscription_id: Mapped[Optional[str]] = mapped_column(
        sa.String(255), nullable=True
    )


class StripeWebhookLog(Base, Created):
    __tablename__ = "stripe_webhook_log"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(sa.String(255), nullable=False, default="")
    event_type: Mapped[str] = mapped_column(sa.String(255), nullable=False, default="")
    payload: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    raw_body: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    processed: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, index=True
    )
    error_message: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
