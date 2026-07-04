"""Provider-agnostic payment endpoints (top-ups, subscriptions, webhooks).

Routes never branch on a concrete provider: the provider id selects an
implementation from ``payment_registry`` and everything else flows through the
``PaymentManager`` / ``PaymentProvider`` abstraction.
"""

import httpx
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select

from src.interfaces.payments import (
    CancelResponse,
    CheckoutResponse,
    DowngradeRequest,
    DowngradeResponse,
    PaymentProviderResponse,
    RegionResponse,
    ResumeResponse,
    SubscribeRequest,
    SubscriptionResponse,
    TierResponse,
    TopupPackResponse,
    TopupRequest,
)
from src.models.base import AsyncSessionLocal
from src.models.plan_subscription import PlanSubscription
from src.models.user import User
from src.models.wallet_connection import WalletConnection
from src.routes.payments import router
from src.services.auth import get_current_user
from src.services.entitlement import get_allowance_state
from src.services.geo import resolve_currency, vat_rate_for_currency
from src.services.payments.base import PaymentProviderKind, UnsupportedCapability
from src.services.payments.credit_subscription import CreditSubscriptionService
from src.services.payments.manager import PaymentManager
from src.services.payments.registry import payment_registry
from src.services.payments.team_seat_subscription import TEAM_CREDITS_PROVIDER
from src.subscription_tiers import DEFAULT_TIER, SUBSCRIPTION_TIERS
from src.topup_packs import TOPUP_PACKS, get_pack
from src.utils.cron import scheduler
from src.utils.frontend import resolve_frontend_base
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


@scheduler.scheduled_job("interval", hours=1)
async def renew_credit_subscriptions() -> int:
    async with AsyncSessionLocal() as db:
        count = await CreditSubscriptionService.process_renewals(db)
        await db.commit()
    return count


def _checkout_redirect(redirect_base: str | None) -> str:
    """Post-checkout return URL on the app the user paid from (chat vs console)."""
    return f"{resolve_frontend_base(redirect_base)}/payment/callback"


async def _user_wallet_chains(db, user_id) -> list[str]:
    """Chains of the user's connected wallets; empty for email/OAuth-only accounts."""
    return list(
        (
            await db.execute(select(WalletConnection.chain).where(WalletConnection.user_id == user_id))
        ).scalars().all()
    )


# Rails are split by account type: wallet users pay on-chain, email users pay by card.
_WALLET_MUST_PAY_ONCHAIN = "Wallet accounts pay on-chain — use the credits provider"
_CREDITS_REQUIRE_WALLET = "Credits subscriptions require a connected wallet"


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
        chains = await _user_wallet_chains(db, user.id)
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


@router.get("/region", description="Caller's payment region (currency + display VAT rate), resolved from client IP")  # type: ignore
async def region(request: Request) -> RegionResponse:
    currency = resolve_currency(request)
    return RegionResponse(currency=currency, vat_rate=vat_rate_for_currency(currency))


@router.get("/topup-packs", description="Fixed EUR top-up packs (gross EUR charge -> USD credits)")  # type: ignore
async def topup_packs() -> list[TopupPackResponse]:
    return [
        TopupPackResponse(id=p.id, usd_credits=p.usd_credits, eur_charge=p.eur_charge)
        for p in TOPUP_PACKS.values()
    ]


@router.post("/topup", description="Open a checkout to buy prepaid credits")  # type: ignore
async def topup(body: TopupRequest, request: Request, user: User = Depends(get_current_user)) -> CheckoutResponse:
    provider = _require_provider(body.provider)
    if provider.descriptor().kind != PaymentProviderKind.fiat:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Crypto top-ups settle on-chain; use the provider's contract address from /payments/providers",
        )
    currency = resolve_currency(request)
    if currency == "EUR":
        # EU users buy fixed gross-EUR packs (VAT-inclusive) for a fixed USD credit.
        if not body.pack_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="EU top-ups use fixed packs; provide pack_id",
            )
        try:
            pack = get_pack(body.pack_id)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        usd_credits, charge_amount, charge_currency = pack.usd_credits, pack.eur_charge, "EUR"
    else:
        # Non-EU: arbitrary USD amount, credited 1:1.
        if body.pack_id is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="pack_id is not used for USD top-ups; send amount only",
            )
        if body.amount is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="amount is required")
        usd_credits, charge_amount, charge_currency = body.amount, body.amount, "USD"
    async with AsyncSessionLocal() as db:
        if await _user_wallet_chains(db, user.id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Wallet accounts top up on-chain — use your connected wallet",
            )
        manager = PaymentManager(provider, db)
        try:
            result = await manager.start_topup(
                user,
                redirect_url=_checkout_redirect(body.redirect_base),
                usd_credits=usd_credits,
                charge_amount=charge_amount,
                charge_currency=charge_currency,
            )
        except (ValueError, UnsupportedCapability) as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        except httpx.HTTPError as e:
            # Provider API failure (bad credentials, wrong environment, outage) — surface a
            # clean, user-displayable error instead of an opaque 500.
            logger.error(f"Payment provider API error: {e}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Payment provider error — please try again later",
            )
        await db.commit()
    return CheckoutResponse(checkout_url=result.checkout_url)


@router.post("/subscribe", description="Open a checkout to subscribe to a paid tier")  # type: ignore
async def subscribe(
    body: SubscribeRequest, request: Request, user: User = Depends(get_current_user)
) -> CheckoutResponse:
    if body.provider == "credits":
        async with AsyncSessionLocal() as db:
            if not await _user_wallet_chains(db, user.id):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_CREDITS_REQUIRE_WALLET)
            try:
                await CreditSubscriptionService.subscribe(db, user, body.tier)
            except ValueError as e:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
            await db.commit()
        return CheckoutResponse(checkout_url=None)
    provider = _require_provider(body.provider)
    currency = resolve_currency(request)
    async with AsyncSessionLocal() as db:
        if await _user_wallet_chains(db, user.id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_WALLET_MUST_PAY_ONCHAIN)
        manager = PaymentManager(provider, db)
        try:
            result = await manager.start_checkout(
                user, tier=body.tier, redirect_url=_checkout_redirect(body.redirect_base), currency=currency
            )
        except (ValueError, UnsupportedCapability) as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        except httpx.HTTPError as e:
            # Provider API failure (bad credentials, wrong environment, outage) — surface a
            # clean, user-displayable error instead of an opaque 500.
            logger.error(f"Payment provider API error: {e}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Payment provider error — please try again later",
            )
        await db.commit()
    return CheckoutResponse(checkout_url=result.checkout_url)


@router.post("/upgrade", description="Upgrade to a higher paid tier")  # type: ignore
async def upgrade(
    body: SubscribeRequest, request: Request, user: User = Depends(get_current_user)
) -> CheckoutResponse:
    if body.provider == "credits":
        async with AsyncSessionLocal() as db:
            if not await _user_wallet_chains(db, user.id):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_CREDITS_REQUIRE_WALLET)
            try:
                await CreditSubscriptionService.upgrade(db, user, body.tier)
            except ValueError as e:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
            await db.commit()
        return CheckoutResponse(checkout_url=None)
    provider = _require_provider(body.provider)
    currency = resolve_currency(request)
    async with AsyncSessionLocal() as db:
        if await _user_wallet_chains(db, user.id):
            # A wallet user may still hold a fiat subscription opened before they
            # connected a wallet — let them upgrade it on its original provider.
            sub = (
                await db.execute(
                    select(PlanSubscription).where(
                        PlanSubscription.user_id == user.id,
                        PlanSubscription.status.in_(["pending", "active", "overdue"]),
                    )
                )
            ).scalar_one_or_none()
            if sub is None or sub.provider != provider.id:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_WALLET_MUST_PAY_ONCHAIN)
        manager = PaymentManager(provider, db)
        try:
            result = await manager.upgrade(
                user, new_tier=body.tier, redirect_url=_checkout_redirect(body.redirect_base), currency=currency
            )
        except (ValueError, UnsupportedCapability) as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        except httpx.HTTPError as e:
            # Provider API failure (bad credentials, wrong environment, outage) — surface a
            # clean, user-displayable error instead of an opaque 500.
            logger.error(f"Payment provider API error: {e}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Payment provider error — please try again later",
            )
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
        if sub is not None and sub.provider == TEAM_CREDITS_PROVIDER:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This subscription is managed by your team",
            )
        if sub and sub.provider == "credits":
            try:
                res = await CreditSubscriptionService.request_downgrade(db, user, body.tier)
            except ValueError as e:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
            await db.commit()
            return DowngradeResponse(new_tier=res["new_tier"], effective_date=res["effective_date"])
        provider = _require_provider(sub.provider) if sub else _require_provider("revolut")
        manager = PaymentManager(provider, db)
        try:
            result = await manager.request_downgrade(user, new_tier=body.tier)
        except (ValueError, UnsupportedCapability) as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        except httpx.HTTPError as e:
            # Provider API failure (bad credentials, wrong environment, outage) — surface a
            # clean, user-displayable error instead of an opaque 500.
            logger.error(f"Payment provider API error: {e}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Payment provider error — please try again later",
            )
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
        if sub is not None and sub.provider == TEAM_CREDITS_PROVIDER:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This subscription is managed by your team",
            )
        if sub.provider == "credits":
            try:
                res = await CreditSubscriptionService.cancel(db, user)
            except ValueError as e:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
            await db.commit()
            return CancelResponse(message=res["message"], effective_date=res["effective_date"])
        manager = PaymentManager(_require_provider(sub.provider), db)
        result = await manager.cancel(user)
        await db.commit()
    return CancelResponse(message=result["message"], effective_date=result["effective_date"])


@router.post("/resume", description="Undo a scheduled cancellation or downgrade before it takes effect")  # type: ignore
async def resume(user: User = Depends(get_current_user)) -> ResumeResponse:
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
        if sub is not None and sub.provider == TEAM_CREDITS_PROVIDER:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This subscription is managed by your team",
            )
        if sub.provider == "credits":
            try:
                res = await CreditSubscriptionService.resume(db, user)
            except ValueError as e:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
            await db.commit()
            return ResumeResponse(message=res["message"], tier=res["tier"])
        manager = PaymentManager(_require_provider(sub.provider), db)
        try:
            result = await manager.resume(user)
        except (ValueError, UnsupportedCapability) as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        except httpx.HTTPError as e:
            logger.error(f"Payment provider API error: {e}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Payment provider error — please try again later",
            )
        await db.commit()
    return ResumeResponse(message=result["message"], tier=result["tier"])


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
        is_team_seat=(sub.provider == TEAM_CREDITS_PROVIDER) if sub else False,
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
