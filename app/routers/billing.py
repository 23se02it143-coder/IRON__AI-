"""Subscriptions: checkout, test-mode payment page, cancel/resume, Stripe webhook."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..database import get_db, utcnow
from ..errors import ApiError
from ..models import CheckoutSession, Invoice, Plan, User
from ..ratelimit import rate_limit
from ..schemas import CheckoutIn, MockCompleteIn
from ..security import current_user
from ..services import billing
from ..services.payments import build_provider, verify_razorpay_signature, verify_stripe_signature

log = logging.getLogger("iron.billing")
router = APIRouter(prefix="/api/billing", tags=["billing"])


def get_provider(settings: Settings = Depends(get_settings)):
    """Dependency so tests can swap in a provider with a mocked HTTP transport."""
    return build_provider(settings)


@router.get("/subscription")
def get_subscription(user: User = Depends(current_user), db: Session = Depends(get_db)):
    sub = billing.current_subscription(db, user)
    invoices = db.scalars(select(Invoice).where(Invoice.user_id == user.id).order_by(Invoice.id.desc()).limit(24))
    return {
        "subscription": billing.serialize_subscription(db, sub),
        "invoices": [
            {"id": i.id, "description": i.description, "amount_cents": i.amount_cents, "currency": i.currency, "status": i.status, "created_at": i.created_at.isoformat()}
            for i in invoices
        ],
    }


@router.post("/checkout", dependencies=[Depends(rate_limit("checkout", 20, 3600))])
def start_checkout(
    data: CheckoutIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    provider=Depends(get_provider),
):
    plan = db.scalars(select(Plan).where(Plan.slug == data.plan_slug)).first()
    if plan is None:
        raise ApiError(404, "plan_not_found", "That plan does not exist.")
    checkout = billing.open_checkout(db, settings, user, plan, data.interval, provider.name)
    customer_id = None
    live = billing.current_subscription(db, user)
    if live and live.provider == provider.name:
        customer_id = live.provider_customer_id
    result = provider.create_checkout(checkout=checkout, user=user, plan=plan, customer_id=customer_id)
    checkout.provider_session_id = result.provider_session_id
    db.commit()
    return {"checkout_id": checkout.id, "url": result.url}


def _own_mock_checkout(db: Session, user: User, settings: Settings, checkout_id: str) -> CheckoutSession:
    if settings.payments_provider != "mock":
        raise ApiError(404, "not_found", "Not found.")
    checkout = db.get(CheckoutSession, checkout_id)
    if checkout is None or checkout.user_id != user.id or checkout.provider != "mock":
        raise ApiError(404, "checkout_not_found", "We could not find that checkout.")
    return checkout


@router.get("/mock/{checkout_id}")
def mock_checkout_details(checkout_id: str, user: User = Depends(current_user), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    checkout = _own_mock_checkout(db, user, settings, checkout_id)
    return {
        "id": checkout.id,
        "status": "expired" if checkout.status == "open" and checkout.expires_at <= utcnow() else checkout.status,
        "plan": {"slug": checkout.plan.slug, "name": checkout.plan.name},
        "interval": checkout.interval,
        "amount_cents": checkout.amount_cents,
        "currency": checkout.currency,
    }


@router.post("/mock/{checkout_id}/complete")
def mock_checkout_complete(
    checkout_id: str,
    data: MockCompleteIn,
    background: BackgroundTasks,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    checkout = _own_mock_checkout(db, user, settings, checkout_id)
    if data.outcome == "cancel":
        if checkout.status == "open":
            checkout.status = "canceled"
            db.commit()
        return {"status": "canceled"}
    billing.complete_mock_checkout(db, settings, background, user, checkout)
    return {"status": "complete"}


@router.post("/cancel")
def cancel_subscription(user: User = Depends(current_user), db: Session = Depends(get_db), provider=Depends(get_provider)):
    sub = billing.current_subscription(db, user)
    if sub is None:
        raise ApiError(400, "no_subscription", "You do not have a paid subscription to cancel.")
    if not sub.cancel_at_period_end:
        if sub.provider == provider.name:
            provider.set_cancel_at_period_end(sub, True)
        sub.cancel_at_period_end = True
        db.commit()
    return {"subscription": billing.serialize_subscription(db, sub)}


@router.post("/resume")
def resume_subscription(user: User = Depends(current_user), db: Session = Depends(get_db), provider=Depends(get_provider)):
    sub = billing.current_subscription(db, user)
    if sub is None:
        raise ApiError(400, "no_subscription", "There is no active subscription to resume. Choose a plan to start a new one.")
    if sub.cancel_at_period_end:
        if sub.provider == provider.name:
            provider.set_cancel_at_period_end(sub, False)
        sub.cancel_at_period_end = False
        db.commit()
    return {"subscription": billing.serialize_subscription(db, sub)}


@router.post("/webhook/stripe", include_in_schema=False)
async def stripe_webhook(
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    provider=Depends(get_provider),
):
    if settings.payments_provider != "stripe" or not settings.stripe_webhook_secret:
        raise ApiError(404, "not_found", "Not found.")
    payload = await request.body()
    if not verify_stripe_signature(payload, request.headers.get("stripe-signature"), settings.stripe_webhook_secret):
        raise ApiError(400, "invalid_signature", "Signature verification failed.")
    try:
        event = json.loads(payload)
    except ValueError:
        raise ApiError(400, "invalid_payload", "The payload is not valid JSON.")
    outcome = billing.handle_stripe_event(db, settings, provider, background, event)
    log.info("Stripe webhook %s (%s): %s", event.get("id"), event.get("type"), outcome)
    return {"received": True, "outcome": outcome}


@router.post("/webhook/razorpay", include_in_schema=False)
async def razorpay_webhook(
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if settings.payments_provider != "razorpay" or not settings.razorpay_webhook_secret:
        raise ApiError(404, "not_found", "Not found.")
    payload = await request.body()
    if not verify_razorpay_signature(payload, request.headers.get("x-razorpay-signature"), settings.razorpay_webhook_secret):
        raise ApiError(400, "invalid_signature", "Signature verification failed.")
    try:
        event = json.loads(payload)
    except ValueError:
        raise ApiError(400, "invalid_payload", "The payload is not valid JSON.")
    outcome = billing.handle_razorpay_event(db, settings, background, event)
    log.info("Razorpay webhook %s: %s", event.get("event"), outcome)
    return {"received": True, "outcome": outcome}
