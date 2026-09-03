import asyncio
import uuid

import httpx
from fastapi import Depends, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from src.interfaces.invoices import (
    BillingDetailsResponse,
    BillingDetailsUpdate,
    InvoiceListResponse,
    InvoiceResponse,
)
from src.interfaces.liberclaw import (
    LiberclawApiKeyDeactivateResponse,
    LiberclawApiKeyRequest,
    LiberclawApiKeyResponse,
    LiberclawExtraCreditsGrant,
    LiberclawExtraCreditsResponse,
    LiberclawInvoiceIssueRequest,
    LiberclawTierUpdate,
    LiberclawUserResponse,
    SubscriptionCycle,
    SubscriptionCyclesResponse,
)
from src.models.base import AsyncSessionLocal
from src.models.invoice import Invoice
from src.models.liberclaw_billing_details import LiberclawBillingDetails
from src.routes.liberclaw import router
from src.services.auth import verify_liberclaw_token
from src.services.invoice import SERIES_LCLW
from src.services.invoice_pdf import get_or_render_pdf
from src.services.liberclaw import LiberclawService
from src.services.liberclaw_invoices import account_is_known, issue_for_liberclaw
from src.services.payments.registry import payment_registry
from src.utils.logger import setup_logger

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
