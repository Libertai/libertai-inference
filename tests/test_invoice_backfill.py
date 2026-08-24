"""Candidate selection for the past-payments backfill: which orders need a regularized invoice."""

import pytest

from scripts.backfill_invoices import Candidate, _line_label, candidate_order_refs
from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.models.credit_transaction import CreditTransaction
from src.models.plan_subscription import PlanSubscription
from src.models.plan_subscription_event import PlanSubscriptionEvent
from tests.test_payment_manager import _make_user


async def _subscription(db, user_id, *, provider="revolut", tier="plus", status="active"):
    sub = PlanSubscription(user_id=user_id, tier=tier, status=status, provider=provider)
    db.add(sub)
    await db.flush()
    return sub


async def _event(db, subscription_id, *, event_type="activated", order_id=None):
    db.add(
        PlanSubscriptionEvent(
            subscription_id=subscription_id,
            event_type=event_type,
            metadata_json={"order_id": order_id} if order_id else None,
        )
    )
    await db.flush()


@pytest.mark.asyncio
async def test_candidate_order_refs_selection(db):
    tx_user = await _make_user(db)
    db.add(
        CreditTransaction(
            user_id=tx_user.id,
            amount=20.0,
            amount_left=20.0,
            provider=CreditTransactionProvider.revolut,
            external_reference="revolut:ord_1",
            status=CreditTransactionStatus.completed,
        )
    )
    # Not a payment order — belongs to the upgrade-remainder flow, not "revolut:<order id>".
    db.add(
        CreditTransaction(
            user_id=tx_user.id,
            amount=5.0,
            amount_left=5.0,
            provider=CreditTransactionProvider.revolut,
            external_reference="upgrade_remainder:xyz",
            status=CreditTransactionStatus.completed,
        )
    )
    db.add(
        CreditTransaction(
            user_id=tx_user.id,
            amount=10.0,
            amount_left=10.0,
            provider=CreditTransactionProvider.revolut,
            external_reference="revolut:ord_pending",
            status=CreditTransactionStatus.pending,
        )
    )
    await db.flush()

    sub_user = await _make_user(db)
    sub = await _subscription(db, sub_user.id, tier="plus")
    await _event(db, sub.id, order_id="ord_2")

    credits_user = await _make_user(db)
    credits_sub = await _subscription(db, credits_user.id, provider="credits", tier="basic")
    await _event(db, credits_sub.id, order_id="ord_3")

    # Same order paid via both a credit transaction and a subscription event: one entry.
    dup_sub = await _subscription(db, tx_user.id, tier="pro")
    await _event(db, dup_sub.id, order_id="ord_1")

    refs = await candidate_order_refs(db)

    assert set(refs) == {"revolut:ord_1", "revolut:ord_2"}
    assert refs["revolut:ord_1"].user_id == tx_user.id
    assert refs["revolut:ord_2"].user_id == sub_user.id
    assert refs["revolut:ord_2"].tier == "plus"


def test_line_label_subscription_ref_uses_tier():
    candidate = Candidate(user_id=None, tier="plus")
    assert _line_label({}, candidate) == "Plus subscription"


def test_line_label_topup_ref_uses_order_line_item_name():
    candidate = Candidate(user_id=None)
    order = {"line_items": [{"name": "LibertAI usage credits ($20)"}]}
    assert _line_label(order, candidate) == "LibertAI usage credits ($20)"


def test_line_label_topup_ref_falls_back_without_line_items():
    candidate = Candidate(user_id=None)
    assert _line_label({}, candidate) == "LibertAI usage credits"
