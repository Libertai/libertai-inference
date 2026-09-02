"""Centralized LCLW invoice issuance for the /liberclaw channel.

Called by LiberClaw's webhook hook (``order_id``) and hourly sweep (``provider_subscription_id``,
optionally targeting a past ``cycle_id``). The Revolut merchant account is shared with inference's
own LTAI product, so every order is screened for foreign ownership before an invoice is issued
against it — see ``_is_foreign_order``.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.interfaces.liberclaw import IssueResult, LiberclawInvoiceIssueRequest
from src.liberclaw_tiers import LIBERCLAW_TIERS
from src.models.credit_transaction import CreditTransaction
from src.models.invoice import Invoice
from src.models.liberclaw_user import LiberclawUser
from src.models.plan_subscription import PlanSubscription
from src.models.plan_subscription_event import PlanSubscriptionEvent
from src.services.invoice import SERIES_LCLW, issue_invoice
from src.services.payments.base import PaymentProvider
from src.services.payments.manager import TOPUP_EXT_REF_PREFIX, order_invoice_fields
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


async def account_is_known(db: AsyncSession, liberclaw_account_id: uuid.UUID) -> bool:
    """Has the identity bridge (``liberclaw_users.liberclaw_account_id``) ever seen this id?

    Callers error-log (not reject) when this is False: a token-authed call for an id nothing
    here recognizes is either a bridge race (api-key call hasn't landed yet) or enumeration.
    """
    return (
        await db.execute(
            select(LiberclawUser.id).where(LiberclawUser.liberclaw_account_id == liberclaw_account_id).limit(1)
        )
    ).scalar_one_or_none() is not None


async def _is_foreign_order(db: AsyncSession, provider_id: str, order: dict, order_id: str) -> bool:
    """True if ``order`` is inference's own (LTAI top-up or subscription) — never LiberClaw's."""
    ext_ref = order.get("merchant_order_ext_ref") or ""
    if ext_ref.startswith(TOPUP_EXT_REF_PREFIX):
        return True

    sub_id = (order.get("channel_data") or {}).get("subscription_id")
    # Guard: only probe when non-null. A NULL sub_id must never match plan_subscriptions rows
    # that also carry NULL (never-paid checkouts) — that would reject every such order.
    # limit(1): a retried charge can log more than one row for the same subscription/order id,
    # and scalar_one_or_none() raises MultipleResultsFound (-> 500) on more than one match.
    if sub_id is not None:
        owned_sub = (
            await db.execute(
                select(PlanSubscription.id).where(PlanSubscription.provider_subscription_id == sub_id).limit(1)
            )
        ).scalar_one_or_none()
        if owned_sub is not None:
            return True

    # Plain string match: credit_transactions carries no metadata column to key on instead.
    # external_reference is unique, so no limit(1) needed here.
    owned_topup = (
        await db.execute(
            select(CreditTransaction.id).where(CreditTransaction.external_reference == f"{provider_id}:{order_id}")
        )
    ).scalar_one_or_none()
    if owned_topup is not None:
        return True

    owned_event = (
        await db.execute(
            select(PlanSubscriptionEvent.id)
            .where(PlanSubscriptionEvent.metadata_json["order_id"].as_string() == order_id)
            .limit(1)
        )
    ).scalar_one_or_none()
    return owned_event is not None


def _audited(status: str, liberclaw_account_id: uuid.UUID, order_id: str | None, **fields) -> IssueResult:
    logger.info(f"LiberClaw invoice issuance: account={liberclaw_account_id} order={order_id} outcome={status}")
    return IssueResult(status=status, **fields)


async def issue_for_liberclaw(
    db: AsyncSession, provider: PaymentProvider, payload: LiberclawInvoiceIssueRequest
) -> IssueResult:
    if payload.tier not in LIBERCLAW_TIERS:
        raise ValueError(f"Unknown LiberClaw tier: {payload.tier!r}")
    if not await account_is_known(db, payload.liberclaw_account_id):
        logger.error(
            f"LiberClaw invoice issuance for account never seen by the identity bridge: {payload.liberclaw_account_id}"
        )

    # 1. Resolve the order id: given directly, or via the subscription's (current or a named
    # past) cycle — the sweep/backfill path.
    cycle_id = payload.cycle_id
    order_id: str | None
    if payload.order_id is not None:
        order_id = payload.order_id
    else:
        sub_id = payload.provider_subscription_id
        assert sub_id is not None  # enforced by the request's exactly-one-of validator
        try:
            if cycle_id is not None:
                cycle = await provider.get_cycle(sub_id, cycle_id)
            else:
                cycle = await provider.get_current_cycle(sub_id)
                cycle_id = cycle.get("id")
            order_id = cycle.get("order_id")
        except Exception:
            logger.warning(f"Could not resolve a cycle/order for LiberClaw sub {sub_id}", exc_info=True)
            order_id = None
        if not order_id:
            return _audited("unresolvable", payload.liberclaw_account_id, None)

    # 2. Lock-free duplicate pre-check: a plain indexed SELECT, before any provider I/O for
    # this order and before issue_invoice's advisory lock — the sweep re-submits blindly.
    # Scoped to series == LCLW: external_reference is unique *globally*, so an order already
    # invoiced on the LTAI side would otherwise surface (and leak) an LTAI number over this
    # channel. A hit on a foreign series falls through to the ownership check below instead,
    # which will (re-)identify it as rejected_foreign.
    external_reference = f"{provider.id}:{order_id}"
    existing = (
        await db.execute(
            select(Invoice.number, Invoice.series).where(Invoice.external_reference == external_reference).limit(1)
        )
    ).first()
    if existing is not None and existing.series == SERIES_LCLW:
        return _audited("duplicate", payload.liberclaw_account_id, order_id, number=existing.number)

    # 3. Ownership checks (need the order payload, hence after the pre-check above).
    try:
        order = await provider.get_order(order_id)
    except Exception:
        logger.warning(f"Could not fetch order {order_id} for a LiberClaw invoice", exc_info=True)
        return _audited("unresolvable", payload.liberclaw_account_id, order_id)

    channel = order.get("channel_data") or {}
    order_sub_id = channel.get("subscription_id")
    if cycle_id is None:
        # order_id-given path: the sweep/backfill path already resolved this from the cycle.
        cycle_id = channel.get("subscription_cycle_id")
    if payload.provider_subscription_id is not None and order_sub_id != payload.provider_subscription_id:
        logger.error(
            f"LiberClaw invoice rejected: account={payload.liberclaw_account_id} order={order_id} claimed "
            f"subscription {payload.provider_subscription_id} but the order belongs to {order_sub_id!r}"
        )
        return _audited("rejected_foreign", payload.liberclaw_account_id, order_id)

    if await _is_foreign_order(db, provider.id, order, order_id):
        logger.error(f"LiberClaw invoice rejected as foreign: account={payload.liberclaw_account_id} order={order_id}")
        return _audited("rejected_foreign", payload.liberclaw_account_id, order_id)

    # 4. Settlement / refund / zero re-checks, on the server's own read of the order (never
    # the caller's say-so). A cycle names its order at creation, before it settles — an
    # unsettled order must retry later (unresolvable), never mint a numbered invoice for
    # money not yet received. Mirrors missed_activation_event's own state check.
    if order.get("state") != "completed":
        return _audited("unresolvable", payload.liberclaw_account_id, order_id)
    if order.get("type") == "refund":
        return _audited("skipped_refund", payload.liberclaw_account_id, order_id)
    try:
        gross, currency, tax, payment_date = order_invoice_fields(order, order_id)
    except ValueError:
        # An unexpected payload shape is an inference-side resolution failure, not a caller
        # error: keep it inside the {status: ...} envelope instead of a bare 422 detail string.
        logger.warning(f"Order {order_id} has an unexpected shape for a LiberClaw invoice", exc_info=True)
        return _audited("unresolvable", payload.liberclaw_account_id, order_id)
    if gross <= 0:
        return _audited("skipped_zero", payload.liberclaw_account_id, order_id)

    # 5. Issue. Tier label is derived server-side, never caller text.
    label = f"LiberClaw {payload.tier.capitalize()} subscription"
    invoice = await issue_invoice(
        db,
        series=SERIES_LCLW,
        liberclaw_account_id=payload.liberclaw_account_id,
        user_email=payload.email,
        external_reference=external_reference,
        gross_minor=gross,
        currency=currency,
        tax_minor=tax,
        payment_date=payment_date,
        line_label=label,
        period_start=payload.period_start,
        period_end=payload.period_end,
        provider_subscription_id=order_sub_id or payload.provider_subscription_id,
        cycle_id=cycle_id,
    )
    if invoice is None:
        # Our lock-free pre-check missed a race that issue_invoice's lock-held check caught.
        # A collision on an ext_ref we did not just create is never routine.
        logger.error(f"issue_invoice reported a duplicate for {external_reference} it did not just create")
        existing_number = (
            await db.execute(select(Invoice.number).where(Invoice.external_reference == external_reference))
        ).scalar_one()
        return _audited("duplicate", payload.liberclaw_account_id, order_id, number=existing_number)

    return _audited("issued", payload.liberclaw_account_id, order_id, invoice_id=invoice.id, number=invoice.number)
