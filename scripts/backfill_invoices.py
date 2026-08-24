"""Regularization backfill: issue invoices for past Revolut payments.

Issues into the CURRENT-year sequence with issued_at = now and the original
payment date shown — late issuance, never retro-dated numbering.
Idempotent on external_reference; safe to re-run.
"""

import argparse
import asyncio
import uuid
from typing import NamedTuple

from sqlalchemy import select

# SQLAlchemy configures every registered mapper on the first query, so any relationship's string
# reference (e.g. ApiKey -> ChatRequest) must already be resolvable — import the full model set
# up front rather than relying on this script's own handful of direct imports. Mirrors the list
# in alembic/env.py and tests/conftest.py.
import src.models.anon_chat_usage
import src.models.api_key
import src.models.auth_code
import src.models.blocked_email_domain
import src.models.chat_request
import src.models.entitlement_window
import src.models.inference_call
import src.models.liberclaw_credit_grant
import src.models.liberclaw_user
import src.models.lifecycle_email_send
import src.models.magic_link
import src.models.oauth_connection
import src.models.session
import src.models.wallet_connection  # noqa: F401
from src.interfaces.credits import CreditTransactionStatus
from src.models.base import AsyncSessionLocal
from src.models.credit_transaction import CreditTransaction
from src.models.plan_subscription import PlanSubscription
from src.models.plan_subscription_event import PlanSubscriptionEvent
from src.models.user import User
from src.services.invoice import issue_invoice
from src.services.payments.manager import order_invoice_fields
from src.services.payments.registry import payment_registry


class Candidate(NamedTuple):
    user_id: uuid.UUID
    tier: str | None = None  # subscription tier, when the ref came from a subscription event


async def candidate_order_refs(db) -> dict[str, Candidate]:
    """order external_reference ("revolut:<id>") -> Candidate, deduped across sources.

    A credit-transaction ref wins over a subscription-event ref for the same order
    (checked first, kept via ``setdefault`` on the second pass).
    """
    refs: dict[str, Candidate] = {}
    txs = await db.execute(
        select(CreditTransaction.external_reference, CreditTransaction.user_id).where(
            CreditTransaction.external_reference.like("revolut:%"),
            CreditTransaction.status == CreditTransactionStatus.completed,
        )
    )
    for ref, user_id in txs:
        refs[ref] = Candidate(user_id)
    events = await db.execute(
        select(PlanSubscriptionEvent.metadata_json, PlanSubscription.user_id, PlanSubscription.tier)
        .join(PlanSubscription, PlanSubscription.id == PlanSubscriptionEvent.subscription_id)
        .where(
            PlanSubscriptionEvent.event_type.in_(("activated", "renewed")),
            PlanSubscription.provider == "revolut",
        )
    )
    for metadata, user_id, tier in events:
        order_id = (metadata or {}).get("order_id")
        if order_id:
            refs.setdefault(f"revolut:{order_id}", Candidate(user_id, tier))
    return refs


def _line_label(order: dict, candidate: Candidate) -> str:
    if candidate.tier:
        return f"{candidate.tier.capitalize()} subscription"
    line_items = order.get("line_items") or []
    name = line_items[0].get("name") if line_items else None
    return name or "LibertAI usage credits"


async def main(dry_run: bool) -> None:
    provider = payment_registry.get("revolut")
    report = {"issued": 0, "duplicate": 0, "refund": 0, "zero": 0, "unresolvable": []}
    async with AsyncSessionLocal() as db:
        refs = await candidate_order_refs(db)
        users = {
            u.id: u
            for u in (await db.execute(select(User).where(User.id.in_({c.user_id for c in refs.values()})))).scalars()
        }
    # Sorted by order ref for deterministic re-runs; issued numbers deliberately do NOT track
    # payment chronology (this is late issuance into the current sequence, never retro-numbering).
    for ref, candidate in sorted(refs.items()):
        order_id = ref.split(":", 1)[1]
        try:
            order = await provider.get_order(order_id)
        except Exception as e:
            report["unresolvable"].append((ref, str(e)))
            continue
        if (order or {}).get("type") == "refund":
            report["refund"] += 1
            continue
        try:
            gross, currency, tax, paid_at = order_invoice_fields(order, order_id)
        except ValueError as e:
            report["unresolvable"].append((ref, str(e)))
            continue
        if gross == 0:
            report["zero"] += 1
            continue
        line_label = _line_label(order, candidate)
        if dry_run:
            report["issued"] += 1
            print(f"DRY {ref}: {gross / 100} {currency} paid {paid_at} — {line_label}")
            continue
        try:
            async with AsyncSessionLocal() as db:
                invoice = await issue_invoice(
                    db,
                    user_id=candidate.user_id,
                    user_email=users[candidate.user_id].email,
                    external_reference=ref,
                    gross_minor=gross,
                    currency=currency,
                    tax_minor=tax,
                    payment_date=paid_at,
                    line_label=line_label,
                )
                await db.commit()
        except Exception as e:
            # The session context manager rolls back on the way out, so a mid-write failure
            # (lock contention, dropped connection, IntegrityError) never leaves a half-issued
            # invoice — the ref stays a clean re-run candidate rather than aborting the batch.
            report["unresolvable"].append((ref, str(e)))
            continue
        report["issued" if invoice else "duplicate"] += 1
    print(report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    asyncio.run(main(parser.parse_args().dry_run))
