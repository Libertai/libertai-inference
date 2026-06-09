"""Provider-agnostic payment endpoints (top-ups, subscriptions, webhooks).

Routes never branch on a concrete provider: the provider id selects an
implementation from ``payment_registry`` and everything else flows through the
``PaymentManager`` / ``PaymentProvider`` abstraction.
"""

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select

from src.config import config
from src.interfaces.payments import (
    CancelResponse,
    CheckoutResponse,
    DowngradeRequest,
    DowngradeResponse,
    PaymentProviderResponse,
    SubscribeRequest,
    SubscriptionResponse,
    TierResponse,
    TopupRequest,
)
from src.models.base import AsyncSessionLocal
from src.models.plan_subscription import PlanSubscription
from src.models.user import User
from src.models.wallet_connection import WalletConnection
from src.routes.payments import router
from src.services.auth import get_current_user
from src.services.entitlement import get_allowance_state
from src.services.payments.base import PaymentProviderKind, UnsupportedCapability
from src.services.payments.manager import PaymentManager
from src.services.payments.registry import payment_registry
from src.subscription_tiers import DEFAULT_TIER, SUBSCRIPTION_TIERS
from src.utils.cron import scheduler
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


@scheduler.scheduled_job("interval", hours=1)
async def expire_subscriptions() -> int:
    """Downgrade subscriptions past their billing period (provider-agnostic, local-only)."""
    async with AsyncSessionLocal() as db:
        manager = PaymentManager(payment_registry.get("revolut"), db)
        count = await manager.check_expirations()
        await db.commit()
    return count


def _checkout_redirect() -> str:
    base = config.FRONTEND_URL.rstrip("/") if config.FRONTEND_URL else ""
    return f"{base}/billing"


def _require_provider(provider_id: str):
    try:
        provider = payment_registry.get(provider_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown provider: {provider_id}")
    if not provider.descriptor().enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Provider {provider_id} is not configured",
        )
    return provider


@router.get("/providers", description="Payment providers available to the authenticated user")  # type: ignore
async def list_providers(user: User = Depends(get_current_user)) -> list[PaymentProviderResponse]:
    async with AsyncSessionLocal() as db:
        chains = list(
            (
                await db.execute(
                    select(WalletConnection.chain).where(WalletConnection.user_id == user.id)
                )
            ).scalars().all()
        )
    return [
        PaymentProviderResponse(
            id=d.id,
            kind=d.kind.value,
            label=d.label,
            capabilities=[c.value for c in d.capabilities],
            currencies=d.currencies,
            chain=d.chain,
            contract_address=d.contract_address,
        )
        for d in payment_registry.available_for_chains(chains)
    ]


@router.get("/tiers", description="Subscription tiers and their pricing/allowances")  # type: ignore
async def list_tiers() -> list[TierResponse]:
    return [
        TierResponse(
            name=cfg.name,
            price_cents=cfg.price_cents,
            currency=cfg.currency,
            window_5h_credits=cfg.window_5h_credits,
            weekly_credits=cfg.weekly_credits,
            is_paid=cfg.is_paid,
        )
        for cfg in SUBSCRIPTION_TIERS.values()
    ]


@router.post("/topup", description="Open a checkout to buy prepaid credits")  # type: ignore
async def topup(body: TopupRequest, user: User = Depends(get_current_user)) -> CheckoutResponse:
    provider = _require_provider(body.provider)
    if provider.descriptor().kind != PaymentProviderKind.fiat:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Crypto top-ups settle on-chain; use the provider's contract address from /payments/providers",
        )
    async with AsyncSessionLocal() as db:
        manager = PaymentManager(provider, db)
        try:
            result = await manager.start_topup(user, amount=body.amount, redirect_url=_checkout_redirect())
        except (ValueError, UnsupportedCapability) as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        await db.commit()
    return CheckoutResponse(checkout_url=result.checkout_url)


@router.post("/subscribe", description="Open a checkout to subscribe to a paid tier")  # type: ignore
async def subscribe(body: SubscribeRequest, user: User = Depends(get_current_user)) -> CheckoutResponse:
    provider = _require_provider(body.provider)
    async with AsyncSessionLocal() as db:
        manager = PaymentManager(provider, db)
        try:
            result = await manager.start_checkout(user, tier=body.tier, redirect_url=_checkout_redirect())
        except (ValueError, UnsupportedCapability) as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        await db.commit()
    return CheckoutResponse(checkout_url=result.checkout_url)


@router.post("/upgrade", description="Upgrade to a higher paid tier")  # type: ignore
async def upgrade(body: SubscribeRequest, user: User = Depends(get_current_user)) -> CheckoutResponse:
    provider = _require_provider(body.provider)
    async with AsyncSessionLocal() as db:
        manager = PaymentManager(provider, db)
        try:
            result = await manager.upgrade(user, new_tier=body.tier, redirect_url=_checkout_redirect())
        except (ValueError, UnsupportedCapability) as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        await db.commit()
    return CheckoutResponse(checkout_url=result.checkout_url)


@router.post("/downgrade", description="Queue a downgrade for the end of the billing period")  # type: ignore
async def downgrade(body: DowngradeRequest, user: User = Depends(get_current_user)) -> DowngradeResponse:
    async with AsyncSessionLocal() as db:
        sub = (
            await db.execute(
                select(PlanSubscription).where(
                    PlanSubscription.user_id == user.id,
                    PlanSubscription.status.in_(["pending", "active", "overdue"]),
                )
            )
        ).scalar_one_or_none()
        provider = _require_provider(sub.provider) if sub else _require_provider("revolut")
        manager = PaymentManager(provider, db)
        try:
            result = await manager.request_downgrade(user, new_tier=body.tier)
        except (ValueError, UnsupportedCapability) as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        await db.commit()
    return DowngradeResponse(new_tier=result["new_tier"], effective_date=result["effective_date"])


@router.post("/cancel", description="Cancel the current subscription at period end")  # type: ignore
async def cancel(user: User = Depends(get_current_user)) -> CancelResponse:
    async with AsyncSessionLocal() as db:
        sub = (
            await db.execute(
                select(PlanSubscription).where(
                    PlanSubscription.user_id == user.id,
                    PlanSubscription.status.in_(["pending", "active", "overdue"]),
                )
            )
        ).scalar_one_or_none()
        if not sub:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No active subscription")
        manager = PaymentManager(_require_provider(sub.provider), db)
        result = await manager.cancel(user)
        await db.commit()
    return CancelResponse(message=result["message"], effective_date=result["effective_date"])


@router.get("/subscription", description="Current subscription state for the authenticated user")  # type: ignore
async def get_subscription(user: User = Depends(get_current_user)) -> SubscriptionResponse:
    async with AsyncSessionLocal() as db:
        sub = (
            await db.execute(
                select(PlanSubscription)
                .where(
                    PlanSubscription.user_id == user.id,
                    PlanSubscription.status.in_(["pending", "active", "overdue"]),
                )
                .order_by(PlanSubscription.created_at.desc())
            )
        ).scalars().first()
        allowance = await get_allowance_state(db, user.id)

    has_sub = sub is not None
    return SubscriptionResponse(
        tier=(sub.tier if sub and sub.status == "active" else DEFAULT_TIER),
        has_subscription=has_sub,
        status=sub.status if sub else None,
        provider=sub.provider if sub else None,
        current_period_end=sub.current_period_end if sub else None,
        cancel_at_period_end=sub.cancel_at_period_end if sub else False,
        pending_tier=sub.pending_tier if sub else None,
        is_trial=sub.is_trial if sub else False,
        allowed=allowance.allowed,
        source=allowance.source,
        window_5h_used=allowance.window_5h_used,
        window_5h_limit=allowance.window_5h_limit,
        window_5h_resets_at=allowance.window_5h_resets_at,
        weekly_used=allowance.weekly_used,
        weekly_limit=allowance.weekly_limit,
        weekly_resets_at=allowance.weekly_resets_at,
        prepaid_balance=allowance.prepaid_balance,
    )


@router.post("/webhook/{provider_id}", description="Provider payment webhook (signature-verified)")  # type: ignore
async def webhook(provider_id: str, request: Request) -> dict:
    try:
        provider = payment_registry.get(provider_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown provider: {provider_id}")

    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        event = provider.parse_webhook(headers, body)
    except ValueError as e:
        logger.warning(f"Rejected {provider_id} webhook: {e}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    except UnsupportedCapability:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider has no webhook")

    async with AsyncSessionLocal() as db:
        manager = PaymentManager(provider, db)
        await manager.handle_event(event)
        await db.commit()
    return {"status": "ok"}
