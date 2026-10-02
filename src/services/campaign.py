"""Credit campaigns: event codes that grant LibertAI + Aleph Cloud credits once per person.

Flow: a claimer signs in (email code or GitHub), claims a code during one of its windows, and gets a
LibertAI voucher on the spot. The Aleph Cloud half needs a wallet: it's signed and stored with the
claim, submitted right away, and retried by ``process_pending_aleph_grants`` until the network
processes or rejects it.
"""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import config
from src.interfaces.campaigns import (
    AlephGrantStatus,
    CampaignClaimResponse,
    CampaignEvent,
    CampaignPublicResponse,
)
from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.models.base import AsyncSessionLocal
from src.models.credit_campaign import CreditCampaign, CreditCampaignClaim, CreditCampaignWindow
from src.models.credit_transaction import CreditTransaction
from src.models.user import User
from src.services import aleph_credits
from src.services.disposable_email import is_blocked_signup_domain
from src.utils.email_canonical import canonical_email
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

# The retry job runs every 2 minutes: ~2 hours of retries before a human has to look.
MAX_ALEPH_ATTEMPTS = 60


def utcnow() -> datetime:
    """Naive UTC, matching the schema's TIMESTAMP columns whatever the host's TZ."""
    return datetime.now(UTC).replace(tzinfo=None)


async def _get_campaign(db: AsyncSession, code: str) -> CreditCampaign:
    campaign = (
        (await db.execute(select(CreditCampaign).where(CreditCampaign.code == code.strip().lower()))).scalars().first()
    )
    if campaign is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown claim code")
    return campaign


async def _windows(db: AsyncSession, campaign_id: uuid.UUID) -> list[CreditCampaignWindow]:
    result = await db.execute(
        select(CreditCampaignWindow)
        .where(CreditCampaignWindow.campaign_id == campaign_id)
        .order_by(CreditCampaignWindow.starts_at)
    )
    return list(result.scalars().all())


async def _claims_in_window(db: AsyncSession, window_id: uuid.UUID) -> int:
    count = await db.scalar(
        select(func.count()).select_from(CreditCampaignClaim).where(CreditCampaignClaim.window_id == window_id)
    )
    return int(count or 0)


async def _campaign_state(
    db: AsyncSession, campaign: CreditCampaign, now: datetime
) -> tuple[str, CreditCampaignWindow | None]:
    """(status, the window it refers to). Only "open" accepts claims, and only into that window."""
    windows = await _windows(db, campaign.id)
    current = [w for w in windows if w.starts_at <= now < w.ends_at]
    upcoming = [w for w in windows if w.starts_at > now]
    past = [w for w in windows if w.ends_at <= now]

    if not campaign.is_active:
        return "closed", current[0] if current else (past[-1] if past else None)
    if current:
        # Overlapping windows: the first one with room takes the claim.
        for window in sorted(current, key=lambda w: w.ends_at):
            if await _claims_in_window(db, window.id) < window.max_claims:
                return "open", window
        return "full", current[0]
    if upcoming:
        return "upcoming", upcoming[0]
    return "closed", past[-1] if past else None


def _event(window: CreditCampaignWindow | None) -> CampaignEvent | None:
    if window is None:
        return None
    return CampaignEvent(name=window.event_name, starts_at=window.starts_at, ends_at=window.ends_at)


async def get_public_campaign(code: str) -> CampaignPublicResponse:
    async with AsyncSessionLocal() as db:
        campaign = await _get_campaign(db, code)
        state, window = await _campaign_state(db, campaign, utcnow())
        return CampaignPublicResponse(
            code=campaign.code,
            libertai_amount=campaign.libertai_amount,
            aleph_amount=campaign.aleph_amount,
            credit_validity_days=campaign.credit_validity_days,
            status=state,  # type: ignore[arg-type]
            event=_event(window),
        )


async def _to_response(
    db: AsyncSession, claim: CreditCampaignClaim, campaign: CreditCampaign
) -> CampaignClaimResponse:
    window = await db.get(CreditCampaignWindow, claim.window_id)
    return CampaignClaimResponse(
        code=campaign.code,
        event_name=window.event_name if window else "",
        created_at=claim.created_at,
        libertai_amount=claim.libertai_amount,
        libertai_expires_at=claim.expires_at,
        aleph_amount=claim.aleph_amount,
        aleph_address=claim.aleph_address,
        aleph_status=claim.aleph_status,
        aleph_item_hash=claim.aleph_item_hash,
        aleph_expires_at=claim.expires_at if claim.aleph_address else None,
    )


async def _find_claim(db: AsyncSession, campaign_id: uuid.UUID, user_id: uuid.UUID) -> CreditCampaignClaim | None:
    result = await db.execute(
        select(CreditCampaignClaim).where(
            CreditCampaignClaim.campaign_id == campaign_id, CreditCampaignClaim.user_id == user_id
        )
    )
    return result.scalars().first()


async def get_claim(code: str, user: User) -> CampaignClaimResponse:
    async with AsyncSessionLocal() as db:
        campaign = await _get_campaign(db, code)
        claim = await _find_claim(db, campaign.id, user.id)
        if claim is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No claim yet")
        return await _to_response(db, claim, campaign)


def _validated_aleph_address(address: str | None) -> str | None:
    if address is None or not address.strip():
        return None
    normalized = aleph_credits.normalize_aleph_address(address)
    if normalized is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That isn't a valid Ethereum/Base (0x…) or Solana wallet address.",
        )
    return normalized


def _prepare_aleph_grant(claim: CreditCampaignClaim, campaign: CreditCampaign, address: str) -> None:
    """Attach the wallet and sign the transfer, in the caller's transaction. Submission comes after
    commit, so a crash in between leaves a stored message for the retry job, never a lost grant."""
    claim.aleph_address = address
    if claim.aleph_amount <= 0:
        claim.aleph_status = AlephGrantStatus.sent
        return
    claim.aleph_status = AlephGrantStatus.pending
    if not config.ALEPH_PROMO_PRIVATE_KEY:
        # Recorded, and sent by the retry job once the promo wallet is configured.
        logger.error("ALEPH_PROMO_PRIVATE_KEY is not set: Aleph campaign grants are queued, not sent")
        return
    message = aleph_credits.build_transfer_message(
        recipient=address,
        amount_usd=claim.aleph_amount,
        expires_at=claim.expires_at,
        tags=["origin_libertai_campaign", f"campaign_{campaign.code}"],
    )
    claim.aleph_message = message
    claim.aleph_item_hash = message["item_hash"]


async def _send_aleph_grant(claim_id: uuid.UUID) -> None:
    """One delivery attempt for a pending grant. Never raises: failures are left to the retry job."""
    async with AsyncSessionLocal() as db:
        claim = (
            (
                await db.execute(
                    select(CreditCampaignClaim)
                    .where(CreditCampaignClaim.id == claim_id)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .first()
        )
        if claim is None or claim.aleph_status != AlephGrantStatus.pending:
            return
        if claim.aleph_message is None:
            if not config.ALEPH_PROMO_PRIVATE_KEY:
                return
            campaign = await db.get(CreditCampaign, claim.campaign_id)
            if campaign is None or claim.aleph_address is None:
                return
            _prepare_aleph_grant(claim, campaign, claim.aleph_address)
            await db.commit()
            if claim.aleph_message is None:
                return

        claim.aleph_attempts += 1
        try:
            # A message submitted before may already be processed (or in flight): ask first, so a
            # retry after a lost response never resubmits needlessly.
            state: aleph_credits.AlephMessageStatus = "unknown"
            if claim.aleph_attempts > 1 and claim.aleph_item_hash:
                state = await aleph_credits.get_message_status(claim.aleph_item_hash)
            if state == "unknown":
                state = await aleph_credits.submit_message(claim.aleph_message)
        except Exception as e:
            logger.warning(f"Aleph grant {claim.id} attempt {claim.aleph_attempts} failed: {e!s}")
            claim.aleph_error = str(e)[:500]
            state = "unknown"

        if state == "processed":
            claim.aleph_status = AlephGrantStatus.sent
            claim.aleph_error = None
        elif state == "rejected":
            # Most likely the promo pool is empty. Needs a human: refund the pool, then reset to pending.
            claim.aleph_status = AlephGrantStatus.failed
            claim.aleph_error = "Rejected by the Aleph network (promo wallet balance?)"
            logger.error(f"Aleph grant {claim.id} ({claim.aleph_item_hash}) was rejected")
        elif claim.aleph_attempts >= MAX_ALEPH_ATTEMPTS:
            claim.aleph_status = AlephGrantStatus.failed
            logger.error(f"Aleph grant {claim.id} gave up after {claim.aleph_attempts} attempts")
        await db.commit()


async def claim_campaign(code: str, user: User, aleph_address: str | None) -> CampaignClaimResponse:
    if not user.email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sign in with email or GitHub to claim: wallet-only accounts can't claim event credits.",
        )
    address = _validated_aleph_address(aleph_address)
    email = canonical_email(user.email)

    async with AsyncSessionLocal() as db:
        campaign = await _get_campaign(db, code)
        if await _find_claim(db, campaign.id, user.id) is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="You already claimed this code.")
        # Email signups are already gated at account creation; OAuth ones are not.
        if await is_blocked_signup_domain(db, user.email):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Event credits can't be claimed with a disposable email address.",
            )

        now = utcnow()
        state, window = await _campaign_state(db, campaign, now)
        if state != "open" or window is None:
            detail = {
                "upcoming": "Claims for this code haven't opened yet.",
                "full": "All the credits for this event have been claimed.",
            }.get(state, "This code isn't accepting claims right now.")
            raise HTTPException(status_code=status.HTTP_410_GONE, detail=detail)

        # Serialize claims per window so the cap holds under concurrent requests.
        await db.execute(select(CreditCampaignWindow).where(CreditCampaignWindow.id == window.id).with_for_update())
        if await _claims_in_window(db, window.id) >= window.max_claims:
            raise HTTPException(
                status_code=status.HTTP_410_GONE, detail="All the credits for this event have been claimed."
            )

        expires_at = now + timedelta(days=campaign.credit_validity_days)
        transaction = CreditTransaction(
            user_id=user.id,
            amount=campaign.libertai_amount,
            amount_left=campaign.libertai_amount,
            provider=CreditTransactionProvider.voucher,
            external_reference=f"campaign:{campaign.id}:{user.id}",
            expired_at=expires_at,
            is_active=True,
            status=CreditTransactionStatus.completed,
        )
        db.add(transaction)
        await db.flush()

        claim = CreditCampaignClaim(
            campaign_id=campaign.id,
            window_id=window.id,
            user_id=user.id,
            email_canonical=email,
            libertai_amount=campaign.libertai_amount,
            aleph_amount=campaign.aleph_amount,
            expires_at=expires_at,
        )
        claim.created_at = now
        claim.credit_transaction_id = transaction.id
        if address is not None:
            _prepare_aleph_grant(claim, campaign, address)
        db.add(claim)
        try:
            await db.commit()
        except IntegrityError as e:
            await db.rollback()
            constraint = str(e.orig)
            if "uq_credit_campaign_claims_aleph_address" in constraint:
                detail = "That wallet already received credits from this code."
            elif "uq_credit_campaign_claims_email" in constraint:
                detail = "This email address already claimed this code."
            else:
                detail = "You already claimed this code."
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail) from None
        logger.info(f"Campaign {campaign.code}: {user.id} claimed in window {window.event_name!r}")
        claim_id = claim.id

    if address is not None:
        await _send_aleph_grant(claim_id)
    return await get_claim(code, user)


async def attach_aleph_address(code: str, user: User, aleph_address: str) -> CampaignClaimResponse:
    """Add the wallet later. Allowed until the claim's credits expire, even after the event: it
    can't create a new claim, so the event window doesn't apply."""
    address = _validated_aleph_address(aleph_address)
    if address is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A wallet address is required.")

    async with AsyncSessionLocal() as db:
        campaign = await _get_campaign(db, code)
        claim = await _find_claim(db, campaign.id, user.id)
        if claim is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No claim yet")
        if claim.aleph_status != AlephGrantStatus.none:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This claim already has a wallet.")
        if claim.expires_at <= utcnow():
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="These credits have expired.")
        _prepare_aleph_grant(claim, campaign, address)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="That wallet already received credits from this code."
            ) from None
        claim_id = claim.id

    await _send_aleph_grant(claim_id)
    return await get_claim(code, user)


async def process_pending_aleph_grants() -> int:
    """Retry job: one more attempt for every pending Aleph grant. Returns how many were tried."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(CreditCampaignClaim.id).where(CreditCampaignClaim.aleph_status == AlephGrantStatus.pending)
        )
        claim_ids = list(result.scalars().all())
    for claim_id in claim_ids:
        await _send_aleph_grant(claim_id)
    return len(claim_ids)
