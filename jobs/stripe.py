import logging

from jobs.celery import app

logger = logging.getLogger(__name__)


@app.task(name="jobs.stripe.stripe_stale_payments")
def stripe_stale_payments():
    logger.info("stripe_stale_payments skipped: billing is disabled in single-project mode")


@app.task(name="jobs.stripe.check_expired_subscriptions")
def check_expired_subscriptions():
    logger.info("check_expired_subscriptions skipped: billing is disabled in single-project mode")


@app.task(name="jobs.stripe.calculate_daily_token_usage")
def calculate_daily_token_usage():
    logger.info("calculate_daily_token_usage skipped: billing is disabled in single-project mode")
