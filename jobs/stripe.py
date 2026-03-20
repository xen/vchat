import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
import stripe
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobs.celery import app
from jobs.db import create_sync_engine
from vchat.models.billing import Payment, PaymentStatus
from vchat.models.data import Chat, ChatMsg, Project
from vchat.models.plan import Plan, plan_from_identifier
from vchat.settings import config

logger = logging.getLogger(__name__)
stripe.api_key = config.get("stripe_api_key")


@contextmanager
def session_scope():
    engine = create_sync_engine()
    try:
        with Session(bind=engine) as session:
            yield session
    finally:
        engine.dispose()


@app.task(name="jobs.stripe.stripe_stale_payments")
def stripe_stale_payments():
    """
    Reconcile stale payments.
    This task should be run every 5 minutes.
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=15)

    stmt = (
        select(Payment)
        .where(
            Payment.status == PaymentStatus.PENDING,
            Payment.created_at < cutoff,
            Payment.stripe_checkout_session_id.is_not(None),
        )
        .limit(100)
    )

    with session_scope() as session:
        stale_payments = session.execute(stmt).scalars().all()

        for payment in stale_payments:
            try:
                checkout_session = stripe.checkout.Session.retrieve(
                    payment.stripe_checkout_session_id
                )
            except Exception:
                logger.exception(
                    "Exception while retrieving Stripe session for payment %s",
                    payment.id,
                )
                continue

            status = getattr(checkout_session, "status", None)
            payment_status = getattr(checkout_session, "payment_status", None)
            subscription_id = getattr(checkout_session, "subscription", None)

            if status == "complete" and payment_status in (
                "paid",
                "no_payment_required",
            ):
                payment.status = PaymentStatus.SUCCEEDED

                if (
                    hasattr(checkout_session, "payment_intent")
                    and not payment.stripe_payment_intent_id
                ):
                    payment.stripe_payment_intent_id = checkout_session.payment_intent

                if subscription_id and not payment.stripe_subscription_id:
                    payment.stripe_subscription_id = subscription_id

                project = session.get(Project, payment.project_id)
                if not project:
                    continue

                project.plan = plan_from_identifier(payment.plan_id)
                project.apply_tariff()
                session.commit()

            elif status == "expired":
                payment.status = PaymentStatus.CANCELED
                session.commit()


@app.task(name="jobs.stripe.check_expired_subscriptions")
def check_expired_subscriptions():
    """
    Check for expired subscriptions and downgrade to free plan.
    """
    stmt = select(Project).where(Project.plan != Plan.FREE)
    now = datetime.now(tz=timezone.utc)

    with session_scope() as session:
        projects_with_plan = session.execute(stmt).scalars().all()
        downgraded_count = 0

        for project in projects_with_plan:
            stmt_pay = select(Payment).where(
                Payment.project_id == project.id,
                Payment.status == PaymentStatus.SUCCEEDED,
                sa.or_(Payment.end_at.is_(None), Payment.end_at > now),
            )
            has_active = session.execute(stmt_pay).first() is not None

            if not has_active:
                logger.info(
                    "Downgrading project %s to free plan (expired).",
                    project.id,
                )
                project.plan = Plan.FREE
                project.apply_tariff()
                downgraded_count += 1

        session.commit()
        logger.info(
            "Checked expired subscriptions. Downgraded %s projects.", downgraded_count
        )


@app.task(name="jobs.stripe.calculate_daily_token_usage")
def calculate_daily_token_usage():
    """
    Calculate token usage for projects with active plans.
    """
    stmt = select(Project).where(Project.plan != Plan.FREE)

    with session_scope() as session:
        projects = session.execute(stmt).scalars().all()

        for project in projects:
            stmt_pay = (
                select(Payment)
                .where(
                    Payment.project_id == project.id,
                    Payment.status == PaymentStatus.SUCCEEDED,
                    Payment.stripe_subscription_id.is_not(None),
                )
                .order_by(Payment.created_at.desc())
                .limit(1)
            )

            payment = session.execute(stmt_pay).scalar_one_or_none()
            if not payment:
                continue

            try:
                subscription = stripe.Subscription.retrieve(
                    payment.stripe_subscription_id
                )
                current_period_start = datetime.fromtimestamp(
                    subscription.current_period_start,
                    tz=timezone.utc,
                )
                current_period_end = datetime.fromtimestamp(
                    subscription.current_period_end,
                    tz=timezone.utc,
                )
            except Exception:
                logger.error(
                    "Failed to retrieve subscription %s",
                    payment.stripe_subscription_id,
                )
                continue

            stmt_tokens = (
                select(func.sum(ChatMsg.tokens))
                .join(Chat)
                .where(
                    Chat.project_id == project.id,
                    ChatMsg.created_at >= current_period_start,
                    ChatMsg.created_at <= current_period_end,
                )
            )

            total_tokens = session.execute(stmt_tokens).scalar() or 0
            project.tokens_used = total_tokens

        session.commit()
