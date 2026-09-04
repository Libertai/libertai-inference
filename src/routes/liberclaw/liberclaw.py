import asyncio
import uuid
from datetime import datetime, timedelta

import httpx
from fastapi import Depends, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import config
from src.interfaces.invoices import (
    BillingDetailsResponse,
    BillingDetailsUpdate,
    InvoiceListResponse,
    InvoiceResponse,
)
from src.interfaces.liberclaw import (
    LiberclawAccountRequest,
    LiberclawAdminExtendRequest,
    LiberclawAdminGrantTrialRequest,
    LiberclawApiKeyDeactivateResponse,
    LiberclawApiKeyRequest,
    LiberclawApiKeyResponse,
    LiberclawCheckoutRequest,
    LiberclawCheckoutResponse,
    LiberclawExtendResponse,
    LiberclawExtraCreditsGrant,
    LiberclawExtraCreditsResponse,
    LiberclawInvoiceIssueRequest,
    LiberclawTierRequest,
    LiberclawTierUpdate,
    LiberclawTrialEligibilityResponse,
    LiberclawTrialRequest,
    LiberclawUpgradeRequest,
    LiberclawUserResponse,
    SubscriptionCycle,
    SubscriptionCyclesResponse,
)
from src.models.base import AsyncSessionLocal
from src.models.invoice import Invoice
from src.models.liberclaw_billing_details import LiberclawBillingDetails
from src.models.liberclaw_user import LiberclawUser
from src.models.plan_subscription import PlanSubscription
from src.routes.liberclaw import router
from src.services.auth import verify_liberclaw_token
from src.services.invoice import SERIES_LCLW
from src.services.invoice_pdf import get_or_render_pdf
from src.services.liberclaw import LiberclawService
from src.services.liberclaw_invoices import account_is_known, issue_for_liberclaw
from src.services.payments.base import CheckoutResult, UnsupportedCapability
from src.services.payments.manager import PaymentManager, ProviderCancelFailed, SupersedeFailed
from src.services.payments.owner import Owner
from src.services.payments.registry import payment_registry
from src.services.payments.tier_push import build_snapshot, utc_iso
from src.utils.logger import setup_logger

# Checkout/upgrade currency for LiberClaw is always EUR — the LCLW tier registry sells no
# other currency, and this must never be resolved from the caller's request/IP.
LCLW_CURRENCY = "EUR"

_PROVIDER_ERROR_DETAIL = "Payment provider error — please try again later"

logger = setup_logger(__name__)

MAX_PAGE_SIZE = 100

# Hops walked back via previous_cycle_id before giving up — bounds one request's cost
# against a malformed or unexpectedly long chain.
MAX_CYCLES = 36

# Sleep between successive get_cycle hops — caps this walk at ~10 provider calls/s
# regardless of the caller's own pacing (the backfill script's 2 req/s is separate).
CYCLE_WALK_DELAY_SECONDS = 0.1

# HTTP status per issuance outcome; anything else (issued, duplicate, skipped_*) is 200.
_ISSUE_STATUS_CODES = {
    "rejected_foreign": status.HTTP_409_CONFLICT,
    "unresolvable": status.HTTP_422_UNPROCESSABLE_ENTITY,
}


@router.post("/api-key", dependencies=[Depends(verify_liberclaw_token)])  # type: ignore
async def get_or_create_api_key(request: LiberclawApiKeyRequest) -> LiberclawApiKeyResponse:
    """Get or create an API key for a Liberclaw user."""
    try:
        return await LiberclawService.get_or_create_api_key(
            user_id=request.user_id,
            user_type=request.user_type,
            liberclaw_account_id=request.liberclaw_account_id,
        )
    except Exception as e:
        logger.error(f"Error in get_or_create_api_key: {e!s}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/api-key/deactivate", dependencies=[Depends(verify_liberclaw_token)])  # type: ignore
async def deactivate_api_key(request: LiberclawApiKeyRequest) -> LiberclawApiKeyDeactivateResponse:
    """Deactivate a Liberclaw user's API key once they have no running agent."""
    try:
        deactivated = await LiberclawService.deactivate_api_key(user_id=request.user_id, user_type=request.user_type)
        return LiberclawApiKeyDeactivateResponse(deactivated=deactivated)
    except Exception as e:
        logger.error(f"Error in deactivate_api_key: {e!s}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.put("/tier", dependencies=[Depends(verify_liberclaw_token)])  # type: ignore
async def update_tier(request: LiberclawTierUpdate) -> None:
    """Update a Liberclaw user's tier."""
    try:
        await LiberclawService.update_tier(user_id=request.user_id, user_type=request.user_type, tier=request.tier)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Error in update_tier: {e!s}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/extra-credits", dependencies=[Depends(verify_liberclaw_token)])  # type: ignore
async def grant_extra_credits(request: LiberclawExtraCreditsGrant) -> LiberclawExtraCreditsResponse:
    """Grant extra usage credits to a Liberclaw user (idempotent on external_reference)."""
    try:
        amount = await LiberclawService.grant_extra_credits(
            user_id=request.user_id,
            user_type=request.user_type,
            from_tier=request.from_tier,
            unused_fraction=request.unused_fraction,
            external_reference=request.external_reference,
        )
        return LiberclawExtraCreditsResponse(amount=amount)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Error in grant_extra_credits: {e!s}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/user", dependencies=[Depends(verify_liberclaw_token)])  # type: ignore
async def get_user(user_id: str, user_type: str) -> LiberclawUserResponse:
    """Get Liberclaw user info with usage stats."""
    try:
        return await LiberclawService.get_user(user_id=user_id, user_type=user_type)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Error in get_user: {e!s}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/invoices", dependencies=[Depends(verify_liberclaw_token)])  # type: ignore
async def issue_invoice(body: LiberclawInvoiceIssueRequest) -> Response:
    """Issue (or no-op on) an LCLW invoice for a settled Revolut order.

    Always 200 with the outcome envelope, except the two outcomes that are themselves an
    error: rejected_foreign (409) and unresolvable (422).
    """
    provider = payment_registry.get("revolut")
    async with AsyncSessionLocal() as db:
        result = await issue_for_liberclaw(db, provider, body)
        await db.commit()
    return JSONResponse(
        status_code=_ISSUE_STATUS_CODES.get(result.status, status.HTTP_200_OK),
        content=result.model_dump(mode="json"),
    )


@router.get("/invoices", dependencies=[Depends(verify_liberclaw_token)])  # type: ignore
async def list_invoices(
    liberclaw_account_id: uuid.UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1),
) -> InvoiceListResponse:
    page_size = min(page_size, MAX_PAGE_SIZE)
    async with AsyncSessionLocal() as db:
        if not await account_is_known(db, liberclaw_account_id):
            # info, not error: the bridge only fills via sync_key_entitlement, so most
            # accounts are unseen here at flag flip — this fires on every such read.
            logger.info(
                f"LiberClaw invoice list read for account never seen by the identity bridge: {liberclaw_account_id}"
            )
        rows = (
            await db.execute(
                select(Invoice)
                .where(Invoice.liberclaw_account_id == liberclaw_account_id, Invoice.series == SERIES_LCLW)
                .order_by(Invoice.issued_at.desc(), Invoice.seq.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars()
        items = [InvoiceResponse.model_validate(row, from_attributes=True) for row in rows]
        total = (
            await db.execute(
                select(func.count())
                .select_from(Invoice)
                .where(Invoice.liberclaw_account_id == liberclaw_account_id, Invoice.series == SERIES_LCLW)
            )
        ).scalar_one()
    if total == 0:
        # info, not error: every routine no-invoice Settings view hits this.
        logger.info(f"No LiberClaw invoices found for account {liberclaw_account_id}")
    return InvoiceListResponse(items=items, total=total)


@router.get("/subscription-cycles", dependencies=[Depends(verify_liberclaw_token)])  # type: ignore
async def list_subscription_cycles(provider_subscription_id: str) -> SubscriptionCyclesResponse:
    """Walk a subscription's cycle chain for the LiberClaw backfill script.

    Newest first, via ``get_current_cycle`` then ``previous_cycle_id`` (capped at
    MAX_CYCLES hops). Entries may carry a null ``order_id`` — callers skip them.
    """
    provider = payment_registry.get("revolut")
    cycles: list[SubscriptionCycle] = []
    try:
        cycle = await provider.get_current_cycle(provider_subscription_id)
        cycle_id = cycle.get("id")
        while cycle_id is not None and len(cycles) < MAX_CYCLES:
            cycles.append(
                SubscriptionCycle(
                    cycle_id=cycle_id,
                    order_id=cycle.get("order_id"),
                    start_date=cycle.get("start_date"),
                    end_date=cycle.get("end_date"),
                )
            )
            previous_cycle_id = cycle.get("previous_cycle_id")
            if previous_cycle_id is None:
                break
            await asyncio.sleep(CYCLE_WALK_DELAY_SECONDS)
            cycle = await provider.get_cycle(provider_subscription_id, previous_cycle_id)
            cycle_id = previous_cycle_id
    except (ValueError, httpx.HTTPError) as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    return SubscriptionCyclesResponse(cycles=cycles)


@router.get("/invoices/{invoice_id}/pdf", dependencies=[Depends(verify_liberclaw_token)])  # type: ignore
async def download_invoice_pdf(invoice_id: uuid.UUID, liberclaw_account_id: uuid.UUID) -> Response:
    async with AsyncSessionLocal() as db:
        invoice = (
            await db.execute(
                select(Invoice).where(
                    Invoice.id == invoice_id,
                    Invoice.liberclaw_account_id == liberclaw_account_id,
                    Invoice.series == SERIES_LCLW,
                )
            )
        ).scalar_one_or_none()
        if invoice is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
        pdf_bytes = await get_or_render_pdf(db, invoice)
        await db.commit()
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{invoice.number}.pdf"',
                "Cache-Control": "no-store",
            },
        )


@router.get("/billing-details", dependencies=[Depends(verify_liberclaw_token)])  # type: ignore
async def get_billing_details(liberclaw_account_id: uuid.UUID) -> BillingDetailsResponse:
    async with AsyncSessionLocal() as db:
        if not await account_is_known(db, liberclaw_account_id):
            # info, not error: the bridge only fills via sync_key_entitlement, so most
            # accounts are unseen here at flag flip — this fires on every such read.
            logger.info(
                f"LiberClaw billing-details read for account never seen by the identity bridge: {liberclaw_account_id}"
            )
        details = await db.get(LiberclawBillingDetails, liberclaw_account_id)
        if details is None:
            # info, not error: every routine no-details Settings view hits this.
            logger.info(f"No LiberClaw billing details found for account {liberclaw_account_id}")
            return BillingDetailsResponse()
        return BillingDetailsResponse(**details.as_snapshot())


@router.put(  # type: ignore
    "/billing-details",
    dependencies=[Depends(verify_liberclaw_token)],
    description="Full replace: omitted fields are cleared, so always send the complete object",
)
async def update_billing_details(
    liberclaw_account_id: uuid.UUID, body: BillingDetailsUpdate
) -> BillingDetailsResponse:
    async with AsyncSessionLocal() as db:
        details = await db.get(LiberclawBillingDetails, liberclaw_account_id)
        if details is None:
            details = LiberclawBillingDetails(liberclaw_account_id=liberclaw_account_id)
            db.add(details)
        for field, value in body.model_dump().items():
            setattr(details, field, value)
        await db.commit()
        await db.refresh(details)
        return BillingDetailsResponse(**details.as_snapshot())


@router.delete("/billing-details", dependencies=[Depends(verify_liberclaw_token)])  # type: ignore
async def delete_billing_details(liberclaw_account_id: uuid.UUID) -> None:
    """Erases the editable profile only — invoice buyer snapshots are retained (legal basis)."""
    async with AsyncSessionLocal() as db:
        details = await db.get(LiberclawBillingDetails, liberclaw_account_id)
        if details is not None:
            await db.delete(details)
            await db.commit()


# --------------------------------------------------------------------- subscription endpoints


def require_billing_enabled() -> None:
    """Guard for every subscription-mutating route: while the flag is off, LiberClaw still owns
    billing and the webhook 200-skips liberclaw events, so a checkout taken here would charge a
    customer for a subscription nothing would ever activate. Read paths stay open."""
    if not config.LIBERCLAW_BILLING_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="LiberClaw billing is not enabled on this backend"
        )


_SUBSCRIPTION_DEPS = [Depends(verify_liberclaw_token), Depends(require_billing_enabled)]

_BRIDGE_CONFLICT_DETAIL = (
    "This email is already linked to a different LiberClaw account. Sign in with the account "
    "that owns it, or contact support to have the two merged."
)

_UNBRIDGED_ACCOUNT_DETAIL = (
    "Unknown LiberClaw account: no identity bridge row exists for it yet. Have the user sign in "
    "(or complete a checkout) so the account is bridged, then retry."
)


async def _upsert_bridge(db: AsyncSession, account_id: uuid.UUID, email: str) -> None:
    """Record/refresh the identity-bridge row for ``account_id`` (never allocates an API key)
    so a webhook — which carries only the account id — can resolve its email later.

    Mirrors ``LiberclawService.get_or_create_api_key``'s resolution order: account id first,
    then the legacy ``(email, 'email')`` row the dedupe migration can leave with no account
    id — a blind INSERT there collides on ``unique_liberclaw_user`` instead of backfilling
    it, and a blind email assignment collides the same way when another account already
    holds the target email (routed through ``_refresh_email_if_safe`` instead, which logs
    and skips rather than raising).

    Raises 409 when the account has no row of its own AND the email's row belongs to another
    account: nothing here can bridge that account, so every later activation webhook — which
    carries only the account id — would fail to resolve an email and raise. It must fail before
    the provider call, not after the customer has paid.
    """
    lc_user = await LiberclawService.resolve_by_account_id(db, account_id)
    if lc_user is None:
        lc_user = (
            (
                await db.execute(
                    select(LiberclawUser).where(LiberclawUser.user_id == email, LiberclawUser.user_type == "email")
                )
            )
            .scalars()
            .first()
        )
        if lc_user is not None and lc_user.liberclaw_account_id not in (None, account_id):
            logger.error(
                f"LiberClaw identity conflict: account {account_id} has no bridge row, and bridge "
                f"row {lc_user.id} holding its email is bound to account {lc_user.liberclaw_account_id}"
            )
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_BRIDGE_CONFLICT_DETAIL)
    if lc_user is None:
        db.add(LiberclawUser(user_id=email, user_type="email", liberclaw_account_id=account_id))
    else:
        if lc_user.liberclaw_account_id is None:
            lc_user.liberclaw_account_id = account_id
        await LiberclawService._refresh_email_if_safe(db, lc_user, email, "email")
    await db.flush()


def _require_provider_subscription_id(result: CheckoutResult, account_id: uuid.UUID) -> None:
    """A subscription checkout with no provider id cannot be located again — neither by the
    caller's requery nor by any webhook. The Revolut-side subscription already exists by the
    time this is reached, so the link is orphaned and named in the log for manual cleanup."""
    if result.provider_subscription_id is None:
        logger.error(
            f"Payment provider returned no subscription id for liberclaw account {account_id}; "
            f"orphaned checkout link: {result.checkout_url}"
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=_PROVIDER_ERROR_DETAIL)


async def _require_bridged_account(db: AsyncSession, account_id: uuid.UUID) -> None:
    """Admin tier writes are refused for an unbridged account: without a bridge row the
    ``lc_users.tier`` write is a logged no-op, leaving a subscription snapshot advertising a
    tier that enforces nothing."""
    if await LiberclawService.resolve_by_account_id(db, account_id) is None:
        logger.error(f"Admin subscription write refused: liberclaw account {account_id} has no identity bridge row")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_UNBRIDGED_ACCOUNT_DETAIL)


async def _owner_with_bridge_email(db: AsyncSession, account_id: uuid.UUID) -> Owner:
    """Owner for a body carrying no email of its own, pulled from the identity bridge if known."""
    lc_user = await LiberclawService.resolve_by_account_id(db, account_id)
    email = lc_user.user_id if lc_user is not None and lc_user.user_type == "email" else None
    return Owner.for_liberclaw(account_id, email=email)


@router.post("/checkout", dependencies=_SUBSCRIPTION_DEPS)  # type: ignore
async def liberclaw_checkout(body: LiberclawCheckoutRequest) -> LiberclawCheckoutResponse:
    async with AsyncSessionLocal() as db:
        await _upsert_bridge(db, body.liberclaw_account_id, body.email)
        owner = Owner.for_liberclaw(body.liberclaw_account_id, email=body.email)
        manager = PaymentManager(payment_registry.get("revolut"), db)
        try:
            result = await manager.start_checkout(
                owner, tier=body.tier, redirect_url=body.redirect_url, currency=LCLW_CURRENCY
            )
        except (ValueError, UnsupportedCapability) as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        except httpx.HTTPError as e:
            logger.error(f"Payment provider API error: {e}")
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=_PROVIDER_ERROR_DETAIL)
        _require_provider_subscription_id(result, body.liberclaw_account_id)
        sub_id = (
            await db.execute(
                select(PlanSubscription.id).where(
                    PlanSubscription.provider_subscription_id == result.provider_subscription_id
                )
            )
        ).scalar_one()
        await db.commit()
    return LiberclawCheckoutResponse(url=result.checkout_url, subscription_id=str(sub_id))


@router.post("/subscription/upgrade", dependencies=_SUBSCRIPTION_DEPS)  # type: ignore
async def liberclaw_upgrade(body: LiberclawUpgradeRequest) -> LiberclawCheckoutResponse:
    async with AsyncSessionLocal() as db:
        owner = await _owner_with_bridge_email(db, body.liberclaw_account_id)
        manager = PaymentManager(payment_registry.get("revolut"), db)
        try:
            result = await manager.upgrade(
                owner, new_tier=body.tier, redirect_url=body.redirect_url, currency=LCLW_CURRENCY
            )
        except (ValueError, UnsupportedCapability) as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        except httpx.HTTPError as e:
            logger.error(f"Payment provider API error: {e}")
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=_PROVIDER_ERROR_DETAIL)
        _require_provider_subscription_id(result, body.liberclaw_account_id)
        sub_id = (
            await db.execute(
                select(PlanSubscription.id).where(
                    PlanSubscription.provider_subscription_id == result.provider_subscription_id
                )
            )
        ).scalar_one()
        await db.commit()
    return LiberclawCheckoutResponse(url=result.checkout_url, subscription_id=str(sub_id))


@router.post("/subscription/cancel", dependencies=_SUBSCRIPTION_DEPS)  # type: ignore
async def liberclaw_cancel(body: LiberclawAccountRequest) -> dict:
    owner = Owner.for_liberclaw(body.liberclaw_account_id)
    async with AsyncSessionLocal() as db:
        manager = PaymentManager(payment_registry.get("revolut"), db)
        try:
            result = await manager.cancel(owner)
        except (ValueError, UnsupportedCapability) as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        await db.commit()
    return result


@router.post("/subscription/resume", dependencies=_SUBSCRIPTION_DEPS)  # type: ignore
async def liberclaw_resume(body: LiberclawAccountRequest) -> dict:
    owner = Owner.for_liberclaw(body.liberclaw_account_id)
    async with AsyncSessionLocal() as db:
        manager = PaymentManager(payment_registry.get("revolut"), db)
        try:
            result = await manager.resume(owner)
        except (ValueError, UnsupportedCapability) as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        await db.commit()
    return result


@router.post("/subscription/downgrade", dependencies=_SUBSCRIPTION_DEPS)  # type: ignore
async def liberclaw_downgrade(body: LiberclawTierRequest) -> dict:
    owner = Owner.for_liberclaw(body.liberclaw_account_id)
    async with AsyncSessionLocal() as db:
        manager = PaymentManager(payment_registry.get("revolut"), db)
        try:
            result = await manager.request_downgrade(owner, new_tier=body.tier)
        except (ValueError, UnsupportedCapability) as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        await db.commit()
    return result


@router.post("/subscription/trial", dependencies=_SUBSCRIPTION_DEPS)  # type: ignore
async def liberclaw_start_trial(body: LiberclawTrialRequest) -> dict:
    async with AsyncSessionLocal() as db:
        await _upsert_bridge(db, body.liberclaw_account_id, body.email)
        owner = Owner.for_liberclaw(body.liberclaw_account_id, email=body.email)
        manager = PaymentManager(payment_registry.get("revolut"), db)
        try:
            sub = await manager.start_self_serve_trial(owner, body.days)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        await db.commit()
        return build_snapshot(sub)


@router.get("/subscription/trial-eligibility", dependencies=[Depends(verify_liberclaw_token)])  # type: ignore
async def liberclaw_trial_eligibility(liberclaw_account_id: uuid.UUID) -> LiberclawTrialEligibilityResponse:
    async with AsyncSessionLocal() as db:
        owner = await _owner_with_bridge_email(db, liberclaw_account_id)
        manager = PaymentManager(payment_registry.get("revolut"), db)
        eligible, reason = await manager.check_trial_eligibility(owner)
    return LiberclawTrialEligibilityResponse(eligible=eligible, reason=reason)


@router.get("/subscription-state", dependencies=[Depends(verify_liberclaw_token)])  # type: ignore
async def liberclaw_subscription_state(liberclaw_account_id: uuid.UUID) -> dict:
    """The account's live-status row (pending/active/overdue) — the raw state feed used by
    pull-reconcile and checkout recording, unlike LC's own filtered /current."""
    owner = Owner.for_liberclaw(liberclaw_account_id)
    async with AsyncSessionLocal() as db:
        manager = PaymentManager(payment_registry.get("revolut"), db)
        sub = await manager._active_subscription(owner, lock=False)
        if sub is None:
            # info, not error: routine for an account with no subscription yet.
            logger.info(f"LiberClaw subscription-state read for account with no live row: {liberclaw_account_id}")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No subscription found")
        return build_snapshot(sub)


@router.post("/subscription/admin/grant-trial", dependencies=_SUBSCRIPTION_DEPS)  # type: ignore
async def liberclaw_admin_grant_trial(body: LiberclawAdminGrantTrialRequest) -> dict:
    owner = Owner.for_liberclaw(body.liberclaw_account_id)
    async with AsyncSessionLocal() as db:
        await _require_bridged_account(db, body.liberclaw_account_id)
        manager = PaymentManager(payment_registry.get("revolut"), db)
        try:
            sub = await manager.grant_trial(owner, body.tier, body.days, body.granted_by)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        await db.commit()
        return build_snapshot(sub)


@router.post("/subscription/admin/override-tier", dependencies=_SUBSCRIPTION_DEPS)  # type: ignore
async def liberclaw_admin_override_tier(body: LiberclawTierRequest) -> dict:
    owner = Owner.for_liberclaw(body.liberclaw_account_id)
    async with AsyncSessionLocal() as db:
        await _require_bridged_account(db, body.liberclaw_account_id)
        manager = PaymentManager(payment_registry.get("revolut"), db)
        try:
            await manager.override_tier(owner, body.tier)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        except SupersedeFailed as e:
            logger.error(f"Override-tier supersede failed: {e}")
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=_PROVIDER_ERROR_DETAIL)
        sub = await manager._active_subscription(owner, lock=False)
        assert sub is not None  # override_tier just activated one
        await db.commit()
        return build_snapshot(sub)


@router.post("/subscription/admin/force-cancel", dependencies=_SUBSCRIPTION_DEPS)  # type: ignore
async def liberclaw_admin_force_cancel(body: LiberclawAccountRequest) -> dict:
    """Immediate terminal cancel, bypassing the deferred wind-down."""
    owner = Owner.for_liberclaw(body.liberclaw_account_id)
    async with AsyncSessionLocal() as db:
        manager = PaymentManager(payment_registry.get("revolut"), db)
        try:
            sub = await manager.force_cancel(owner)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        except ProviderCancelFailed as e:
            logger.error(f"Force-cancel failed at the provider: {e}")
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=_PROVIDER_ERROR_DETAIL)
        await db.commit()
        await db.refresh(sub)  # updated_at is server-computed (onupdate): reload before reading it
        return build_snapshot(sub)


@router.post("/subscription/admin/extend", dependencies=_SUBSCRIPTION_DEPS)  # type: ignore
async def liberclaw_admin_extend(body: LiberclawAdminExtendRequest) -> LiberclawExtendResponse:
    if body.days < 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="days must be positive")
    owner = Owner.for_liberclaw(body.liberclaw_account_id)
    async with AsyncSessionLocal() as db:
        manager = PaymentManager(payment_registry.get("revolut"), db)
        sub = await manager._active_subscription(owner, lock=True)
        if sub is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No active subscription")
        base = sub.current_period_end or datetime.now()
        sub.current_period_end = base + timedelta(days=body.days)
        await manager._log_event(sub, "admin_extended", metadata={"days": body.days})
        await db.commit()
        return LiberclawExtendResponse(new_period_end=utc_iso(sub.current_period_end))
