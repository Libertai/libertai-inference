"""Monthly reconcile: Revolut's ledger vs. the invoice archive.

Manual-run (the spec schedules it monthly). Enumerates completed non-refund orders from
the Revolut API directly — never local events, which would inherit the lost-webhook blind
spot this exists to catch — then checks three invariants: gap-free per-series-per-year
numbering, at most one invoice per LCLW (provider_subscription_id, cycle_id), and exactly
one invoice per enumerated order with matching gross. Prints a report and exits non-zero
on any violation.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

# SQLAlchemy configures every registered mapper on the first query, so any relationship's
# string reference must already be resolvable — import the full model set up front rather
# than relying on this script's own handful of direct imports. Mirrors alembic/env.py.
import src.models.anon_chat_usage
import src.models.api_key
import src.models.auth_code
import src.models.blocked_email_domain
import src.models.chat_request
import src.models.credit_transaction
import src.models.entitlement_window
import src.models.inference_call
import src.models.liberclaw_billing_details
import src.models.liberclaw_credit_grant
import src.models.liberclaw_user
import src.models.lifecycle_email_send
import src.models.magic_link
import src.models.oauth_connection
import src.models.plan_subscription_event
import src.models.session
import src.models.user
import src.models.user_billing_details
import src.models.wallet_connection  # noqa: F401
from src.models.base import AsyncSessionLocal
from src.models.invoice import Invoice
from src.models.plan_subscription import PlanSubscription
from src.services.invoice import SERIES_LCLW
from src.services.payments.manager import TOPUP_EXT_REF_PREFIX
from src.services.payments.registry import payment_registry
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


def classify_order(order: dict, own_subscription_ids: set[str]) -> str:
    """Label an enumerated order for the report.

    "own": inference's own order (a topup ext_ref, or a subscription id matching a local
    plan_subscriptions row). "liberclaw": a foreign subscription id corroborated by a
    "LiberClaw" description prefix. "unclassifiable": neither signal lines up — the caller
    logs these for manual triage.
    """
    ext_ref = order.get("merchant_order_ext_ref") or ""
    if ext_ref.startswith(TOPUP_EXT_REF_PREFIX):
        return "own"
    channel_data = order.get("channel_data") or {}
    sub_id = channel_data.get("subscription_id")
    if sub_id is not None and sub_id in own_subscription_ids:
        return "own"
    description = order.get("description") or ""
    if sub_id is not None and description.startswith("LiberClaw"):
        return "liberclaw"
    return "unclassifiable"


async def check_series_sequence_gapless(db: AsyncSession) -> list[str]:
    """Invariant 1: per (series, year), count(*) == max(seq) — a mismatch means a burnt
    or duplicated number."""
    rows = (
        await db.execute(
            select(Invoice.series, Invoice.year, func.count(), func.max(Invoice.seq)).group_by(
                Invoice.series, Invoice.year
            )
        )
    ).all()
    return [
        f"{series}-{year}: count={count} max_seq={max_seq}"
        for series, year, count, max_seq in rows
        if count != max_seq
    ]


async def check_lclw_cycle_uniqueness(db: AsyncSession) -> list[str]:
    """Invariant 2: at most one invoice per LCLW (provider_subscription_id, cycle_id)."""
    rows = (
        await db.execute(
            select(Invoice.provider_subscription_id, Invoice.cycle_id, func.count())
            .where(
                Invoice.series == SERIES_LCLW,
                Invoice.provider_subscription_id.isnot(None),
                Invoice.cycle_id.isnot(None),
            )
            .group_by(Invoice.provider_subscription_id, Invoice.cycle_id)
            .having(func.count() > 1)
        )
    ).all()
    return [f"{sub_id}/{cycle_id}: {count} invoices" for sub_id, cycle_id, count in rows]


async def check_orders_have_matching_invoice(db: AsyncSession, orders: list[dict]) -> list[str]:
    """Invariant 3: exactly one invoice across both series per enumerated order, with
    gross/currency matching the settled amount.

    Zero-amount orders are excluded: issuance deliberately skips them (issue_invoice
    no-ops on gross_minor <= 0), so no invoice is ever expected for one. An order
    missing "amount" can't be checked either way — it's logged and skipped rather
    than crashing the run.
    """
    violations = []
    for order in orders:
        order_id = order["id"]
        amount = order.get("amount")
        if amount is None:
            logger.error(f"Unclassifiable order in reconcile (missing amount): {order_id}")
            continue
        if amount == 0:
            continue
        ref = f"revolut:{order_id}"
        rows = (
            await db.execute(select(Invoice.gross_amount, Invoice.currency).where(Invoice.external_reference == ref))
        ).all()
        if len(rows) == 0:
            violations.append(f"{order_id}: no invoice found")
            continue
        if len(rows) > 1:
            violations.append(f"{order_id}: {len(rows)} invoices found")
            continue
        gross_amount, currency = rows[0]
        expected_gross = (Decimal(amount) / 100).quantize(Decimal("0.01"))
        if gross_amount != expected_gross or currency != order.get("currency"):
            violations.append(
                f"{order_id}: gross mismatch (invoice={gross_amount} {currency}, "
                f"order={expected_gross} {order.get('currency')})"
            )
    return violations


async def enumerate_completed_orders(provider) -> list[dict]:
    """Every completed, non-refund order on the merchant account.

    ``list_orders`` raises if its single page might be truncated (see its docstring) —
    that propagates here uncaught, since a maybe-partial order set must never be reconciled.
    """
    orders = await provider.list_orders()
    return [order for order in orders if order.get("state") == "completed" and order.get("type") != "refund"]


async def main() -> None:
    provider = payment_registry.get("revolut")
    async with AsyncSessionLocal() as db:
        own_subscription_ids = {
            sub_id
            for sub_id in (
                await db.execute(
                    select(PlanSubscription.provider_subscription_id).where(
                        PlanSubscription.provider_subscription_id.isnot(None)
                    )
                )
            ).scalars()
            if sub_id is not None
        }
        orders = await enumerate_completed_orders(provider)

        classifications: Counter[str] = Counter()
        for order in orders:
            label = classify_order(order, own_subscription_ids)
            classifications[label] += 1
            if label == "unclassifiable":
                logger.error(f"Unclassifiable order in reconcile: {order.get('id')}")

        violations = [f"sequence: {v}" for v in await check_series_sequence_gapless(db)]
        violations += [f"lclw_cycle: {v}" for v in await check_lclw_cycle_uniqueness(db)]
        violations += [f"order_match: {v}" for v in await check_orders_have_matching_invoice(db, orders)]

    report = {
        "orders_enumerated": len(orders),
        "classifications": dict(classifications),
        "violations": violations,
    }
    print(report)
    if violations:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
