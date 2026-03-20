import logging
from decimal import Decimal
from urllib.parse import urlencode

import aiohttp_jinja2
import stripe
from aiohttp import web
from sqlalchemy import select, desc
from itsdangerous import URLSafeSerializer, BadSignature

from vchat.models.billing import Payment, PaymentStatus, StripeWebhookLog
from vchat.models.data import Project
from vchat.models.plan import plan_from_identifier
from vchat.settings import config

logger = logging.getLogger(__name__)

stripe.api_key = config.get("stripe", {}).get("api_key")
stripe.enable_telemetry = False

PLAN_LIST = [
    {
        "id": "personal_monthly",
        "name": "Personal Monthly",
        "price": 15,
        "interval": "month",
        "tokens": 500000,
        "price_id": config.get("stripe", {}).get(
            "price_personal_monthly", "price_personal_monthly"
        ),
    },
    {
        "id": "personal_yearly",
        "name": "Personal Yearly",
        "price": 149,
        "interval": "year",
        "tokens": 500000,
        "price_id": config.get("stripe", {}).get(
            "price_personal_yearly", "price_personal_yearly"
        ),
    },
    {
        "id": "pro_monthly",
        "name": "Pro Monthly",
        "price": 29,
        "interval": "month",
        "tokens": 1500000,
        "price_id": config.get("stripe", {}).get(
            "price_pro_monthly", "price_pro_monthly"
        ),
    },
    {
        "id": "pro_yearly",
        "name": "Pro Yearly",
        "price": 299,
        "interval": "year",
        "tokens": 1500000,
        "price_id": config.get("stripe", {}).get(
            "price_pro_yearly", "price_pro_yearly"
        ),
    },
    {
        "id": "startup_monthly",
        "name": "Startup Monthly",
        "price": 99,
        "interval": "month",
        "tokens": 3000000,
        "price_id": config.get("stripe", {}).get(
            "price_startup_monthly", "price_startup_monthly"
        ),
    },
    {
        "id": "startup_yearly",
        "name": "Startup Yearly",
        "price": 999,
        "interval": "year",
        "tokens": 3000000,
        "price_id": config.get("stripe", {}).get(
            "price_startup_yearly", "price_startup_yearly"
        ),
    },
]

routes = web.RouteTableDef()


@routes.get("/project/{short_id}/billing")
@aiohttp_jinja2.template("projects/billing.html")
async def billing_page(request):
    short_id = request.match_info["short_id"]
    db = request["db"]

    stmt = select(Project).where(Project.short_id == short_id)
    result = await db.execute(stmt)
    project = result.scalar_one_or_none()

    if not project:
        raise web.HTTPNotFound()

    # Check access (simplified)
    # user = request["user"]
    # if project.user_id != user.id: ...

    user_data = project

    stmt_pay = (
        select(Payment)
        .where(Payment.project_id == project.id)
        .order_by(desc(Payment.created_at))
    )
    res_pay = await db.execute(stmt_pay)
    payments = res_pay.scalars().all()

    has_active_payment = False
    active_payment = None
    for payment in payments:
        if payment.status == PaymentStatus.SUCCEEDED and not payment.end_at:
            has_active_payment = True
            active_payment = payment
            break

    return {
        "project": project,
        "user_data": user_data,
        "payments": payments,
        "plans": PLAN_LIST,
        "has_active_payment": has_active_payment,
        "active_payment": active_payment,
    }


@routes.post("/project/{short_id}/billing/upgrade")
async def upgrade_plan(request):
    short_id = request.match_info["short_id"]
    db = request["db"]
    data = await request.post()
    plan_id = data.get("plan_id")

    stmt = select(Project).where(Project.short_id == short_id)
    result = await db.execute(stmt)
    project = result.scalar_one_or_none()

    if not project:
        raise web.HTTPNotFound()

    selected_plan = next((p for p in PLAN_LIST if p["id"] == plan_id), None)
    if not selected_plan:
        raise web.HTTPBadRequest(text="Invalid plan")

    if not project.stripe_customer_id:
        customer = stripe.Customer.create(
            email=f"project_{project.short_id}@example.com",  # Should use owner email
            name=project.title,
            metadata={"project_id": str(project.id)},
        )
        project.stripe_customer_id = customer.id
        await db.commit()

    # Create Payment
    payment = Payment(
        project_id=project.id,
        plan_id=plan_id,
        amount=Decimal(selected_plan["price"]),
        currency="usd",
        description=f"Upgrade to {selected_plan['name']}",
    )
    db.add(payment)
    await db.commit()

    token_serializer = URLSafeSerializer(config["secret_key"])
    success_token = token_serializer.dumps(
        {"payment_id": payment.id, "purpose": "success"},
        salt="subscription-upgrade",
    )
    cancel_token = token_serializer.dumps(
        {"payment_id": payment.id, "purpose": "cancel"},
        salt="subscription-upgrade",
    )

    checkout_url = str(request.app.router["billing_result"].url_for(short_id=short_id))
    success_url = f"{checkout_url}?{urlencode({'token': success_token})}"
    cancel_url = f"{checkout_url}?{urlencode({'cancel_token': cancel_token, 'canceled': 'true'})}"

    session = stripe.checkout.Session.create(
        success_url=request.url.scheme + "://" + request.host + success_url,
        cancel_url=request.url.scheme + "://" + request.host + cancel_url,
        mode="subscription",
        customer=project.stripe_customer_id,
        line_items=[
            {
                "price": selected_plan["price_id"],
                "quantity": 1,
            }
        ],
        metadata={"project_id": str(project.id), "plan_id": plan_id},
        subscription_data={"billing_mode": "charge_automatically"},
    )

    payment.stripe_checkout_session_id = session.id
    await db.commit()

    return web.HTTPFound(session.url)


@routes.post("/project/{short_id}/billing/downgrade")
async def downgrade_plan(request):
    short_id = request.match_info["short_id"]
    db = request["db"]
    data = await request.post()
    payment_id = data.get("payment_id")

    stmt = select(Payment).where(Payment.id == int(payment_id))
    result = await db.execute(stmt)
    payment = result.scalar_one_or_none()

    if not payment or not payment.stripe_subscription_id:
        raise web.HTTPBadRequest(text="Invalid payment")

    try:
        stripe.Subscription.modify(
            payment.stripe_subscription_id,
            cancel_at_period_end=True,
        )
        # We don't update end_at immediately, webhook will handle it or we check periodically
    except Exception as e:
        logger.error(f"Error canceling subscription: {e}")
        raise web.HTTPInternalServerError()

    return web.HTTPFound(request.app.router["billing_page"].url_for(short_id=short_id))


@routes.get("/project/{short_id}/billing/result", name="billing_result")
async def billing_result(request):
    short_id = request.match_info["short_id"]
    token = request.query.get("token")
    cancel_token = request.query.get("cancel_token")
    canceled_flag = request.query.get("canceled") == "true"
    db = request["db_session"]

    serializer = URLSafeSerializer(config["secret_key"])

    try:
        if canceled_flag:
            data = serializer.loads(cancel_token, salt="subscription-upgrade")
        else:
            data = serializer.loads(token, salt="subscription-upgrade")
    except BadSignature:
        raise web.HTTPBadRequest(text="Invalid token")

    payment_id = data.get("payment_id")
    stmt = select(Payment).where(Payment.id == payment_id)
    result = await db.execute(stmt)
    payment = result.scalar_one_or_none()

    if not payment:
        raise web.HTTPNotFound()

    if canceled_flag:
        payment.status = PaymentStatus.CANCELED
    else:
        # Verify with Stripe
        try:
            session = stripe.checkout.Session.retrieve(
                payment.stripe_checkout_session_id
            )
            if session.status == "complete":
                payment.status = PaymentStatus.SUCCEEDED
                payment.stripe_subscription_id = session.subscription

                stmt_proj = select(Project).where(Project.id == payment.project_id)
                res_proj = await db.execute(stmt_proj)
                project = res_proj.scalar_one_or_none()

                if project:
                    project.plan = plan_from_identifier(payment.plan_id)
                    project.apply_tariff()

        except Exception as e:
            logger.error(f"Error checking session: {e}")

    await db.commit()
    return web.HTTPFound(request.app.router["billing_page"].url_for(short_id=short_id))


@routes.post("/stripe/webhook")
async def stripe_webhook(request):
    payload = await request.read()
    sig_header = request.headers.get("Stripe-Signature")
    db = request["db_session"]

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, config.get("stripe", {}).get("webhook_secret", "")
        )
    except ValueError as e:
        logger.warning("Invalid payload in Stripe webhook: %s", e)
        return web.Response(status=400)
    except stripe.SignatureVerificationError as e:
        logger.warning("Invalid signature in Stripe webhook: %s", e)
        return web.Response(status=400)

    # Log webhook
    log = StripeWebhookLog(
        event_id=event.get("id"),
        event_type=event.get("type"),
        payload=event,
        raw_body=payload.decode("utf-8"),
    )
    db.add(log)
    await db.commit()

    # Handle event
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        stmt = select(Payment).where(
            Payment.stripe_checkout_session_id == session["id"]
        )
        res = await db.execute(stmt)
        payment = res.scalar_one_or_none()

        if payment:
            payment.status = PaymentStatus.SUCCEEDED
            payment.stripe_subscription_id = session.get("subscription")

            stmt_proj = select(Project).where(Project.id == payment.project_id)
            res_proj = await db.execute(stmt_proj)
            project = res_proj.scalar_one_or_none()

            if project:
                project.plan = plan_from_identifier(payment.plan_id)
                project.apply_tariff()

            await db.commit()

    return web.Response(status=200)
