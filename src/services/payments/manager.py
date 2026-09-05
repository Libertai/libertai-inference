"""Provider-agnostic payment orchestrator.

Owns the business logic for **top-ups** (one-off prepaid credit purchases) and
**subscriptions** (recurring tiers), driven by normalized :class:`PaymentEvent`s.
It never references a concrete provider — it is handed a :class:`PaymentProvider`
and an :class:`AsyncSession`, and speaks only the abstraction.

Ported/condensed from the liberclaw subscription manager; the remote tier-sync
(``change_user_tier`` HTTP call) is dropped because the tier now lives locally on
the ``plan_subscriptions`` row and is read directly by the entitlement service.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, true
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from src.config import config
from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.liberclaw_tiers import LIBERCLAW_TIERS
from src.models.credit_transaction import CreditTransaction
from src.models.plan_subscription import ACTIVE_STATUSES, ENDED_STATUSES, UNPAID_CHECKOUT_STATUSES, PlanSubscription
from src.models.plan_subscription_event import PlanSubscriptionEvent
from src.models.user import User
from src.services.geo import vat_rate_for_currency
from src.services.invoice import SERIES_LCLW, issue_invoice
from src.services.liberclaw import LiberclawService
from src.services.lifecycle_email import send_lifecycle_email
from src.services.payments.base import (
    LIVE_PROVIDER_STATES,
    CheckoutResult,
    PaymentCapability,
    PaymentEvent,
    PaymentEventType,
    PaymentProvider,
    UnsupportedCapability,
)
from src.services.payments.owner import Owner
from src.subscription_tiers import (
    DEFAULT_CURRENCY,
    DEFAULT_TIERS,
    PRODUCT_LIBERCLAW,
    PRODUCT_LIBERTAI,
    get_tier,
    is_downgrade,
    is_upgrade,
    paid_tiers,
)
from src.utils.logger import setup_logger
from src.utils.pg_locks import USER_SUBSCRIPTION_LOCK_CLASS

logger = setup_logger(__name__)

# A top-up credit transaction is keyed by this hash so webhook replays dedup and
# the pending row created at checkout time can be completed on confirmation.
TOPUP_EXT_REF_PREFIX = "topup:"


class SupersedeFailed(Exception):
    """A live subscription an activation replaces could not be cancelled at the provider.

    Raised before anything is written, so the transaction it aborts leaves no partial state and
    the provider's retry re-attempts the cancel from a clean slate.
    """


class ProviderCancelFailed(Exception):
    """An immediate cancel could not be confirmed at the provider.

    Raised after the transaction has been rolled back, so nothing local is terminal while the
    subscription stays payable at the provider.
    """


def _topup_external_ref(provider_id: str, order_id: str) -> str:
    return f"{provider_id}:{order_id}"


def _naive_utc(value: str) -> datetime:
    """Provider timestamps carry an offset; the period columns are naive UTC and get compared
    against ``datetime.now()``, which a mixed-awareness value would raise on."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def order_invoice_fields(order: dict, order_id: str) -> tuple[int, str, int | None, datetime]:
    """(gross_minor, currency, tax_minor, payment_date) off a Revolut order payload.

    ``order_id`` is only for the error message: a missing key here means the provider's
    payload shape changed, and a bare KeyError wouldn't say which order or field.
    """
    for field in ("amount", "currency"):
        if field not in order:
            raise ValueError(f"Order {order_id} is missing {field!r}: cannot derive invoice fields")
    gross = int(order["amount"])
    currency = order["currency"]
    tax = None
    line_items = order.get("line_items") or []
    taxes = [t.get("amount", 0) for li in line_items for t in (li.get("taxes") or [])]
    if taxes:
        tax = int(sum(taxes))
    completed = order.get("completed_at") or order.get("updated_at")
    payment_date = _naive_utc(completed) if completed else datetime.now(timezone.utc).replace(tzinfo=None)
    return gross, currency, tax, payment_date


async def paid_subscription_ids(db: AsyncSession, subs: Sequence[PlanSubscription]) -> set[uuid.UUID]:
    """Of ``subs``, the ids that carry positive proof of a payment: an ``activated``/``renewed`` event.

    Period dates are not proof of the opposite: ``_refresh_cycle_dates`` swallows provider read
    failures, so an activation can land with none, and ``manual`` grants never have any. Only a
    row nothing ever billed on may be treated as an abandoned checkout.
    """
    ids = [s.id for s in subs]
    if not ids:
        return set()
    return set(
        (
            await db.execute(
                select(PlanSubscriptionEvent.subscription_id).where(
                    PlanSubscriptionEvent.subscription_id.in_(ids),
                    PlanSubscriptionEvent.event_type.in_(("activated", "renewed")),
                )
            )
        )
        .scalars()
        .all()
    )


# How long before a cycle ends check_expirations cancels a wind-down at the provider.
PROVIDER_CANCEL_LEAD = timedelta(hours=2)

# Refusal for a checkout whose predecessor turned out to be paid: the row stays put until
# reconcile_pending activates it, so a retry shortly after succeeds or finds it active.
PAYMENT_BEING_CONFIRMED = "A payment on your subscription is still being confirmed — please try again in a few minutes"

# Days past current_period_end before a recurring liberclaw subscription with no renewal
# webhook in sight is expired. Only a renewal payment moves that date forward.
RENEWAL_GRACE_DAYS = 7

# Self-serve (no admin) trial tier per product — the cheapest paid tier each sells.
SELF_SERVE_TRIAL_TIER = {PRODUCT_LIBERTAI: "go", PRODUCT_LIBERCLAW: "starter"}

PROVIDER_ALREADY_CANCELLED = "Subscription already cancelled at the payment provider"


def _in_wind_down_cancel_window(sub: PlanSubscription) -> bool:
    """Is a provider cancellation of ``sub`` one check_expirations just asked for?

    The flag alone is not enough: it is set a whole cycle earlier, and a provider cancellation
    before this window is the user cancelling at the provider — terminal and refunded there, so
    leaving the row live would keep an entitlement (and a resumable row) against a subscription
    that no longer exists.
    """
    return (
        sub.cancel_at_period_end
        and sub.current_period_end is not None
        and sub.current_period_end <= datetime.now() + PROVIDER_CANCEL_LEAD
    )


def _lock_key(owning_id: uuid.UUID) -> int:
    """Signed 32-bit objid derived from an owner's lock id, for ``USER_SUBSCRIPTION_LOCK_CLASS``.

    Two owning ids sharing an objid merely serialize against each other.
    """
    return int.from_bytes(owning_id.bytes[:4], "big", signed=True)


class PaymentManager:
    def __init__(self, provider: PaymentProvider, db: AsyncSession):
        self.provider = provider
        self.db = db
        # One provider read per order per webhook: refund classification, subscription
        # resolution and invoice issuance all inspect the same payload.
        self._order_cache: dict[str, dict] = {}

    async def _get_order(self, order_id: str) -> dict:
        if order_id not in self._order_cache:
            self._order_cache[order_id] = await self.provider.get_order(order_id)
        return self._order_cache[order_id]

    # ------------------------------------------------------------------ helpers
    async def _active_subscription(self, owner: Owner, lock: bool = True) -> PlanSubscription | None:
        stmt = select(PlanSubscription).where(
            owner.sub_filter(),
            PlanSubscription.status.in_(ACTIVE_STATUSES),
        )
        if lock:
            stmt = stmt.with_for_update()
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def _lock_owner(self, owner: Owner) -> None:
        """Take the per-owner mutex over subscription mutations, for the rest of the transaction.

        Every path that touches more than one of an owner's rows takes it FIRST, before any row
        lock: the webhook and the checkout paths acquire the live row and the checkout row in
        opposite orders, so row-level locking alone deadlocks them.
        """
        await self.db.execute(
            select(func.pg_advisory_xact_lock(USER_SUBSCRIPTION_LOCK_CLASS, _lock_key(owner.lock_id)))
        )

    def _product_scope(self) -> ColumnElement[bool]:
        """Periodic jobs stay LTAI-only until the cutover flag flips: liberclaw rows are
        invisible to them until then."""
        if config.LIBERCLAW_BILLING_ENABLED:
            return true()
        return PlanSubscription.product == PRODUCT_LIBERTAI

    async def current_tier(self, owner: Owner) -> str:
        sub = await self._active_subscription(owner, lock=False)
        if sub and sub.status == "active":
            return sub.tier
        return DEFAULT_TIERS[owner.product]

    async def _log_event(
        self,
        subscription: PlanSubscription,
        event_type: str,
        provider_event_id: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        self.db.add(
            PlanSubscriptionEvent(
                subscription_id=subscription.id,
                event_type=event_type,
                provider_event_id=provider_event_id,
                metadata_json=metadata,
            )
        )

    # ------------------------------------------------------------------ top-ups
    async def start_topup(
        self,
        user: User,
        redirect_url: str,
        *,
        usd_credits: float,
        charge_amount: float,
        charge_currency: str,
    ) -> CheckoutResult:
        """Open a hosted checkout for a one-off prepaid credit purchase.

        The charge and the credit are decoupled: non-EU users pay arbitrary USD
        1:1 (``usd_credits == charge_amount``), while EU users buy a fixed pack —
        ``charge_amount`` is the gross EUR price (VAT-inclusive; the VAT portion is
        back-calculated and sent as an order line item) and ``usd_credits`` is the
        fixed USD value credited. The pending row always records ``usd_credits``
        because the prepaid balance is USD-denominated and webhook settlement
        completes the recorded amount as-is.
        """
        if not self.provider.supports(PaymentCapability.topup):
            raise UnsupportedCapability(f"{self.provider.id} does not support top-ups")
        if usd_credits <= 0:
            raise ValueError("Top-up credit amount must be positive")
        if charge_amount <= 0:
            raise ValueError("Top-up charge amount must be positive")

        result = await self.provider.create_topup(
            amount=charge_amount,
            currency=charge_currency,
            redirect_url=redirect_url,
            user_email=user.email,
            metadata={"ext_ref": f"{TOPUP_EXT_REF_PREFIX}{user.id}"},
            vat_rate=vat_rate_for_currency(charge_currency),
            item_name=f"LibertAI usage credits (${usd_credits:g})",
        )

        # Record the credits as pending now; they become spendable when the
        # provider confirms payment (ORDER_COMPLETED). Pending rows don't count
        # toward the balance (balance filters on status == completed).
        if result.order_id:
            self.db.add(
                CreditTransaction(
                    user_id=user.id,
                    amount=usd_credits,
                    amount_left=usd_credits,
                    provider=CreditTransactionProvider.revolut,
                    external_reference=_topup_external_ref(self.provider.id, result.order_id),
                    status=CreditTransactionStatus.pending,
                    is_active=True,
                )
            )
            await self.db.flush()
        return result

    async def _settle_topup(self, event: PaymentEvent) -> bool:
        """Complete/fail a pending top-up. Returns True if this event was a top-up."""
        if not event.order_id:
            return False
        tx = (
            await self.db.execute(
                select(CreditTransaction)
                .where(CreditTransaction.external_reference == _topup_external_ref(event.provider, event.order_id))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if tx is None:
            return False  # not a (locally-recorded) top-up — let the subscription path try

        if event.type == PaymentEventType.order_completed:
            if tx.status != CreditTransactionStatus.completed:
                tx.status = CreditTransactionStatus.completed
                # A card can be declined several times before one succeeds on the same order.
                # Each decline zeroed the row, so restore it in full — never leave a paid-for
                # top-up with amount_left 0 / is_active False.
                tx.is_active = True
                tx.amount_left = tx.amount
                logger.info(f"Top-up {tx.external_reference} completed ({tx.amount} credits)")
                # Not wrapped: a failed read must fail the webhook (provider redelivers) rather
                # than complete the top-up with no invoice to show for it.
                order = await self._get_order(event.order_id)
                gross, currency, tax, paid_at = order_invoice_fields(order, event.order_id)
                user = (await self.db.execute(select(User).where(User.id == tx.user_id))).scalar_one()
                if user.email is None or tx.external_reference is None:
                    raise RuntimeError(f"Top-up {tx.id} cannot be invoiced: missing user email or order reference")
                # The order's own line-item name is what the customer saw at checkout; the invoice
                # line mirrors it exactly, falling back to the credits label for older orders.
                line_items = order.get("line_items") or []
                label = (
                    line_items[0].get("name") if line_items else None
                ) or f"LibertAI usage credits (${tx.amount:g})"
                await issue_invoice(
                    self.db,
                    user_id=tx.user_id,
                    user_email=user.email,
                    external_reference=tx.external_reference,
                    gross_minor=gross,
                    currency=currency,
                    tax_minor=tax,
                    payment_date=paid_at,
                    line_label=label,
                )
            else:
                logger.info(f"Top-up {tx.external_reference} already completed, skipping")
        elif event.type == PaymentEventType.order_failed:
            if tx.status == CreditTransactionStatus.completed:
                # Out-of-order delivery: a declined attempt on an order that has since been
                # paid. Applying it would confiscate credits the user paid for and may have
                # already partly spent.
                logger.info(f"Ignoring failure for already-completed top-up {tx.external_reference}")
            else:
                tx.status = CreditTransactionStatus.error
                tx.is_active = False
                tx.amount_left = 0
        await self.db.flush()
        return True

    # ------------------------------------------------------------------ subscriptions
    async def _record_checkout_retired(
        self, sub: PlanSubscription, *, cancelled: bool, dedup_event: bool = False
    ) -> None:
        """Retire a never-paid checkout row. The single place any path may retire one.

        The audit event fires unconditionally by default: ``_is_retired_checkout`` keys on it,
        so a row whose provider cancel failed still refuses a later payment rather than
        activating as a fresh subscription carrying none of the state that retired it. Only the
        status write is gated on ``cancelled`` — writing ``expired`` while the link is live at
        the provider would mark the row dead locally while it stays payable.

        ``dedup_event=True`` for a caller that retries the SAME row across passes (the provider
        cancel every time, until it succeeds): the event must land once ever, not once per
        attempt, while the cancel itself is never skipped just because it already carries one.
        """
        if cancelled:
            sub.status = "expired"
        if dedup_event and await self._is_retired_checkout(sub):
            return
        await self._log_event(sub, "expired_abandoned_checkout")

    async def _retire_unpaid_checkouts(self, owner: Owner, statuses: tuple[str, ...]) -> int:
        """Expire the owner's never-paid checkout rows in ``statuses``, returning how many it kept.

        A kept row is one that turned out to be paid. Callers that go on to open a new checkout
        must refuse on a non-zero count: the row still occupies its partial unique index, and
        reconcile_pending will activate it within the hour.

        Its own query, never ``_active_subscription``: that helper's ``scalar_one_or_none``
        matches at most one row, and a mid-upgrade owner legitimately has two. Missing period
        dates select the candidates; only ``paid_subscription_ids`` decides which of them were
        never paid.
        """
        rows = (
            (
                await self.db.execute(
                    select(PlanSubscription)
                    .where(
                        owner.sub_filter(),
                        PlanSubscription.status.in_(statuses),
                        PlanSubscription.current_period_start.is_(None),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        paid_ids = await paid_subscription_ids(self.db, rows)
        kept = 0
        for row in rows:
            if row.id in paid_ids:
                logger.warning(f"Sub {row.id} has a payment on record despite no period dates, not retiring it")
                kept += 1
                continue
            if await self._live_at_provider(row):
                # Paid, but the activation webhook never landed to record it, so nothing local
                # says so. Cancelling here would cancel a subscription the user is paying for;
                # reconcile_pending adopts it instead.
                logger.warning(f"Sub {row.id} is live at the provider with no activation recorded, not retiring it")
                kept += 1
                continue
            cancelled = await self._cancel_on_provider(row)
            await self._record_checkout_retired(row, cancelled=cancelled)
        if rows:
            await self.db.flush()
        return kept

    async def _open_checkout(
        self, owner: Owner, tier: str, redirect_url: str, currency: str, status: str
    ) -> CheckoutResult:
        if tier not in paid_tiers(owner.product):
            raise ValueError(f"Invalid paid tier: {tier}")
        if not self.provider.supports(PaymentCapability.subscription):
            raise UnsupportedCapability(f"{self.provider.id} does not support subscriptions")
        if not owner.email:
            raise ValueError("User must have an email to subscribe")

        prev_customer_id = (
            await self.db.execute(
                select(PlanSubscription.provider_customer_id)
                .where(
                    owner.sub_filter(),
                    PlanSubscription.provider_customer_id.isnot(None),
                )
                .order_by(PlanSubscription.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        result = await self.provider.create_subscription(
            user_email=owner.email,
            tier=tier,
            currency=currency,
            redirect_url=redirect_url,
            provider_customer_id=prev_customer_id,
            product=owner.product,
        )
        sub = PlanSubscription(
            user_id=owner.user_id,
            tier=tier,
            status=status,
            provider=self.provider.id,
            provider_subscription_id=result.provider_subscription_id,
            provider_customer_id=result.provider_customer_id,
            # Locked at checkout time: renewals bill through the provider's
            # currency-specific plan, so the currency never changes mid-life.
            currency=currency,
            product=owner.product,
            liberclaw_account_id=owner.liberclaw_account_id,
        )
        self.db.add(sub)
        try:
            await self.db.flush()
        except IntegrityError:
            if status == "pending_upgrade":
                raise ValueError("An upgrade checkout is already open")
            raise ValueError("User already has an active subscription")
        await self._log_event(sub, "created", metadata={"tier": tier})
        return result

    async def start_checkout(self, owner: Owner, tier: str, redirect_url: str, currency: str) -> CheckoutResult:
        await self._lock_owner(owner)
        existing = await self._active_subscription(owner)
        if existing:
            if existing.status == "pending" and existing.current_period_start is None:
                if await self._retire_unpaid_checkouts(owner, ("pending",)):
                    raise ValueError(PAYMENT_BEING_CONFIRMED)
            elif existing.provider == "manual" or existing.cancel_at_period_end:
                # Nothing will renew this one, so a new checkout supersedes it instead of being
                # refused. Same tier is refused for a real wind-down still billed at the
                # provider — resume() undoes that rather than layering a second checkout on the
                # same plan — but allowed over a manual/trial row, which never renews regardless.
                if tier == existing.tier and existing.provider != "manual":
                    raise ValueError("User already has an active subscription")
                existing.status = "upgrading"
                await self._log_event(existing, "upgrade_started", metadata={"new_tier": tier})
                await self.db.flush()
            else:
                raise ValueError("User already has an active subscription")
        # An upgrade checkout is invisible to _active_subscription, so retire it explicitly:
        # otherwise an owner whose subscription lapsed mid-upgrade holds two payable checkouts.
        if await self._retire_unpaid_checkouts(owner, ("pending_upgrade",)):
            raise ValueError(PAYMENT_BEING_CONFIRMED)
        return await self._open_checkout(owner, tier, redirect_url, currency, "pending")

    async def upgrade(self, owner: Owner, new_tier: str, redirect_url: str, currency: str) -> CheckoutResult:
        if new_tier not in paid_tiers(owner.product):
            raise ValueError(f"Invalid tier: {new_tier}")
        await self._lock_owner(owner)
        # Validated against the live row's tier, not current_tier(): that returns "free" for
        # anything not exactly "active", which would admit an upgrade from no subscription at
        # all, and let an overdue higher-tier holder open a lower-tier "upgrade".
        live = (
            await self.db.execute(
                select(PlanSubscription)
                .where(
                    owner.sub_filter(),
                    PlanSubscription.status.in_(("active", "overdue")),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not live:
            raise ValueError("No active subscription")
        if not is_upgrade(live.tier, new_tier, owner.product):
            raise ValueError(f"Cannot upgrade from {live.tier} to {new_tier}")

        if await self._retire_unpaid_checkouts(owner, ("pending_upgrade",)):
            raise ValueError(PAYMENT_BEING_CONFIRMED)
        return await self._open_checkout(owner, new_tier, redirect_url, currency, "pending_upgrade")

    async def cancel(self, owner: Owner) -> dict:
        """Schedule cancellation at period end. Cancellation is TERMINAL on Revolut, so the
        provider-side cancel is DEFERRED to just before renewal (see check_expirations) —
        until then the owner can resume() for free. Cancel == downgrade to free."""
        await self._lock_owner(owner)
        sub = await self._active_subscription(owner)
        if not sub:
            raise ValueError("No active subscription")
        sub.cancel_at_period_end = True
        sub.pending_tier = DEFAULT_TIERS[owner.product]
        await self._log_event(sub, "cancel_requested")
        # A scheduled wind-down must not leave a payable upgrade link: paying it would build a
        # fresh row that carries none of the wind-down state, silently converting the request
        # into a renewing, more expensive subscription.
        await self._retire_unpaid_checkouts(owner, ("pending_upgrade",))
        if owner.product == PRODUCT_LIBERTAI:
            assert owner.user_id is not None  # libertai rows always carry user_id
            user = (await self.db.execute(select(User).where(User.id == owner.user_id))).scalar_one()
            await send_lifecycle_email(
                self.db,
                user,
                "cancellation_confirmed",
                "cancellation_confirmed",
                {"until": sub.current_period_end},
                transactional=True,
                once=False,
                resend_after=timedelta(hours=1),
            )
        await self.db.flush()
        return {
            "message": "Subscription will be cancelled at end of billing period",
            "effective_date": sub.current_period_end.isoformat() if sub.current_period_end else None,
        }

    async def force_cancel(self, owner: Owner) -> PlanSubscription:
        """Immediate terminal cancel, bypassing the deferred wind-down.

        ``provider_cancelled`` is pre-marked and flushed BEFORE the provider call (echo arm 1):
        a cancellation-confirmed webhook racing this call then reads the row as already terminal
        instead of re-processing it. A provider cancel that cannot be confirmed rolls the whole
        transaction back and raises — nothing here goes terminal locally unless the provider
        agreed too.
        """
        await self._lock_owner(owner)
        sub = await self._active_subscription(owner)
        if sub is None:
            raise ValueError("No active subscription")
        sub.provider_cancelled = True
        await self.db.flush()
        if not await self._cancel_on_provider(sub):
            # Read before the rollback: it expires every attribute, and a re-read would need a
            # transaction the caller has already been told to abandon. No re-assignment of the
            # pre-mark either — the rollback discards the flushed value with everything else.
            sub_id = sub.id
            await self.db.rollback()
            raise ProviderCancelFailed(f"Subscription {sub_id} could not be cancelled at the provider")
        sub.status = "cancelled"
        await self._log_event(sub, "cancelled", metadata={"source": "admin_force_cancel"})
        if owner.product == PRODUCT_LIBERCLAW:
            await self._lclw_sync_tier_free_unless_live(owner, exclude_sub_id=sub.id)
        await self.db.flush()
        return sub

    async def resume(self, owner: Owner) -> dict:
        """Undo a scheduled cancellation or paid downgrade before it takes effect."""
        sub = await self._active_subscription(owner)
        if not sub:
            raise ValueError("No active subscription")
        if sub.provider_cancelled:
            raise ValueError(PROVIDER_ALREADY_CANCELLED)
        if not sub.cancel_at_period_end and not sub.pending_tier:
            raise ValueError("Nothing to resume")
        now = datetime.now()
        end = sub.current_period_end
        if end is not None and (end.replace(tzinfo=None) if end.tzinfo else end) < now:
            raise ValueError("The billing period already ended")
        # Undo a scheduled paid downgrade: schedule a change back to the current plan
        # (overwrites the provider's pending change).
        if (
            sub.pending_tier
            and sub.pending_tier != DEFAULT_TIERS[owner.product]
            and sub.provider != "manual"
            and sub.provider_subscription_id
        ):
            await self.provider.change_subscription_plan(
                sub.provider_subscription_id,
                tier=sub.tier,
                currency=sub.currency or DEFAULT_CURRENCY,
                product=owner.product,
            )
        sub.pending_tier = None
        sub.cancel_at_period_end = False
        await self._log_event(sub, "resumed")
        await self.db.flush()
        return {"message": "Your subscription will continue", "tier": sub.tier}

    async def request_downgrade(self, owner: Owner, new_tier: str) -> dict:
        if new_tier not in paid_tiers(owner.product) and new_tier != DEFAULT_TIERS[owner.product]:
            raise ValueError(f"Invalid tier: {new_tier}")
        await self._lock_owner(owner)
        current = await self.current_tier(owner)
        if not is_downgrade(current, new_tier, owner.product):
            raise ValueError(f"Cannot downgrade from {current} to {new_tier}")
        sub = await self._active_subscription(owner)
        if not sub:
            raise ValueError("No active subscription")
        if new_tier == DEFAULT_TIERS[owner.product]:
            # Downgrade to free == cancel: the sub lapses at period end. Provider-side
            # cancel is deferred (terminal on Revolut) so this stays resumable.
            sub.pending_tier = new_tier
            sub.cancel_at_period_end = True
        else:
            # Paid -> paid: schedule the plan change on the provider FIRST (next cycle bills
            # the lower variation); only record pending_tier once the provider accepted it,
            # so a failure can't leave us billing the old tier while promising the new one.
            if sub.provider != "manual" and sub.provider_subscription_id:
                await self.provider.change_subscription_plan(
                    sub.provider_subscription_id,
                    tier=new_tier,
                    currency=sub.currency or DEFAULT_CURRENCY,
                    product=owner.product,
                )
            sub.pending_tier = new_tier
            # The sub keeps renewing (on the new plan) — supersede any earlier cancel request.
            sub.cancel_at_period_end = False
        await self._log_event(sub, "downgrade_requested", metadata={"new_tier": new_tier})
        # A scheduled wind-down must not leave a payable upgrade link: paying it would build a
        # fresh row that carries none of the wind-down state, silently converting the request
        # into a renewing, more expensive subscription.
        await self._retire_unpaid_checkouts(owner, ("pending_upgrade",))
        await self.db.flush()
        return {
            "effective_date": sub.current_period_end.isoformat() if sub.current_period_end else None,
            "new_tier": new_tier,
        }

    async def grant_trial(self, owner: Owner, tier: str, days: int, granted_by: str | None) -> PlanSubscription:
        """Admin-granted trial: a manual, no-payment row entitled for exactly ``days``."""
        if tier not in paid_tiers(owner.product):
            raise ValueError(f"Invalid paid tier: {tier}")
        if days < 1 or days > 90:
            raise ValueError("Trial duration must be 1-90 days")
        await self._lock_owner(owner)
        sub = await self._create_trial_row(owner, tier, days)
        await self._log_event(sub, "trial_granted", metadata={"granted_by": granted_by, "days": days, "tier": tier})
        if owner.product == PRODUCT_LIBERCLAW:
            assert owner.liberclaw_account_id is not None  # liberclaw rows always carry it
            await LiberclawService.update_tier_by_account_id(self.db, owner.liberclaw_account_id, tier)
        await self.db.flush()
        return sub

    async def _create_trial_row(self, owner: Owner, tier: str, days: int) -> PlanSubscription:
        """Insert the manual trial row itself. Caller logs its own event (admin grant vs.
        self-serve carry different metadata) and syncs the LCLW tier afterward."""
        if await self._active_subscription(owner):
            raise ValueError("User already has an active subscription")
        now = datetime.now()
        sub = PlanSubscription(
            user_id=owner.user_id,
            tier=tier,
            status="active",
            provider="manual",
            is_trial=True,
            currency=None,
            current_period_start=now,
            current_period_end=now + timedelta(days=days),
            product=owner.product,
            liberclaw_account_id=owner.liberclaw_account_id,
        )
        self.db.add(sub)
        try:
            await self.db.flush()
        except IntegrityError:
            raise ValueError("User already has an active subscription")
        return sub

    async def check_trial_eligibility(self, owner: Owner) -> tuple[bool, str | None]:
        """Whether ``owner`` may start a self-serve trial. Returns ``(eligible, reason)``:
        a trial is one-per-owner ever (whether granted or self-served) and refused for anyone
        who has ever completed a paid billing cycle."""
        if not owner.email:
            return False, "no_email"

        prior_trial = (
            await self.db.execute(
                select(PlanSubscription.id).where(owner.sub_filter(), PlanSubscription.is_trial == True).limit(1)
            )
        ).scalar_one_or_none()
        if prior_trial is not None:
            return False, "trial_used"

        prior_paid = (
            await self.db.execute(
                select(PlanSubscription.id)
                .where(
                    owner.sub_filter(),
                    PlanSubscription.is_trial == False,
                    PlanSubscription.current_period_end.is_not(None),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if prior_paid is not None:
            return False, "was_paid"

        if await self._active_subscription(owner, lock=False):
            return False, "active_sub"

        return True, None

    async def start_self_serve_trial(self, owner: Owner, days: int) -> PlanSubscription:
        """User-initiated trial on the product's cheapest paid tier, no card. One per owner, ever."""
        eligible, reason = await self.check_trial_eligibility(owner)
        if not eligible:
            raise ValueError(f"Not eligible for a trial ({reason})")
        await self._lock_owner(owner)
        tier = SELF_SERVE_TRIAL_TIER[owner.product]
        sub = await self._create_trial_row(owner, tier, days)
        await self._log_event(sub, "trial_started", metadata={"self_serve": True, "days": days, "tier": tier})
        if owner.product == PRODUCT_LIBERCLAW:
            assert owner.liberclaw_account_id is not None  # liberclaw rows always carry it
            await LiberclawService.update_tier_by_account_id(self.db, owner.liberclaw_account_id, tier)
        await self.db.flush()
        return sub

    async def override_tier(self, owner: Owner, tier: str) -> None:
        """Admin force-sets an owner's tier with an open-ended manual row, no billing.

        Supersedes any existing live subscription (paid or trial) via the same machinery an
        upgrade activation uses, parking it first so the fresh row never collides with it on
        the one-live-subscription index.
        """
        if tier not in paid_tiers(owner.product) and tier != DEFAULT_TIERS[owner.product]:
            raise ValueError(f"Invalid tier: {tier}")
        await self._lock_owner(owner)
        existing = await self._active_subscription(owner)
        if existing is not None:
            existing.status = "upgrading"
            await self._log_event(existing, "upgrade_started", metadata={"new_tier": tier})
            await self.db.flush()

        sub = PlanSubscription(
            user_id=owner.user_id,
            tier=tier,
            status="active",
            provider="manual",
            is_trial=False,
            currency=None,
            current_period_start=datetime.now(),
            current_period_end=None,
            product=owner.product,
            liberclaw_account_id=owner.liberclaw_account_id,
        )
        self.db.add(sub)
        await self.db.flush()
        await self._log_event(sub, "tier_overridden", metadata={"tier": tier})

        if existing is not None:
            # An admin override moves no money: the row it replaces is retired but never
            # credited a remainder, whatever was left of it.
            ok, _ = await self._supersede_other_subs(owner, exclude_sub_id=sub.id, credit_remainder=False)
            if not ok:
                raise SupersedeFailed(
                    f"Cannot override tier for owner {owner.lock_id}: its existing subscription "
                    f"could not be cancelled at the provider"
                )
        if owner.product == PRODUCT_LIBERCLAW:
            assert owner.liberclaw_account_id is not None  # liberclaw rows always carry it
            await LiberclawService.update_tier_by_account_id(self.db, owner.liberclaw_account_id, tier)
        await self.db.flush()

    async def _live_at_provider(self, sub: PlanSubscription) -> bool:
        """Whether the provider reports this subscription as paid and live.

        An unreadable provider answers True: not knowing is not permission to cancel, and
        the cost of waiting is a checkout the user must retry, against a subscription
        cancelled out from under a paying customer.
        """
        if sub.provider == "manual" or not sub.provider_subscription_id:
            return False
        try:
            info = await self.provider.get_subscription(sub.provider_subscription_id)
        except UnsupportedCapability:
            return False
        except Exception:
            logger.warning(f"Could not read provider state for sub {sub.id}; treating it as live", exc_info=True)
            return True
        return info.state in LIVE_PROVIDER_STATES

    async def _cancel_on_provider(self, sub: PlanSubscription) -> bool:
        """True when the provider-side subscription is known to be cancelled.

        Callers gate their status write on this: a swallowed failure that still wrote
        ``expired`` would leave a row dead locally while it stays payable at the provider.
        """
        if sub.provider == "manual" or not sub.provider_subscription_id:
            return True
        try:
            await self.provider.cancel_subscription(sub.provider_subscription_id)
            return True
        except Exception:
            logger.warning(f"Failed to cancel sub {sub.id} on provider", exc_info=True)
            return False

    async def _supersede_other_subs(
        self, owner: Owner, exclude_sub_id: uuid.UUID, *, credit_remainder: bool = True
    ) -> tuple[bool, str | None]:
        """Retire the owner's other live rows in favour of a just-paid subscription.

        Returns ``(ok, from_tier)``. ``ok`` is False when a row that must not be left behind —
        one that was paid for, or one still occupying the live-subscription index — could not be
        cancelled at the provider, so the caller cannot go on to activate a second live row.
        ``from_tier`` is the tier upgraded FROM when a paid row was superseded, so the caller can
        log a single ``upgraded`` event linking the pair.

        ``pending_upgrade`` is deliberately absent from the status set: ORDER_COMPLETED also
        fires for renewals of the live subscription, and including it would cancel the owner's
        open checkout at the provider mid-payment. ``upgrading`` is included to catch rows
        parked by the previous release.

        ``credit_remainder=False`` for a supersede that moved no money (an admin tier
        override): the old row is still retired, but nothing is credited for it.
        """
        rows = (
            (
                await self.db.execute(
                    select(PlanSubscription).where(
                        owner.sub_filter(),
                        PlanSubscription.status.in_((*ACTIVE_STATUSES, "upgrading")),
                        PlanSubscription.id != exclude_sub_id,
                    )
                )
            )
            .scalars()
            .all()
        )

        # Every provider cancel resolves before any local write, so that giving up leaves no
        # half-applied supersede behind. Only a row outside the one-live-subscription index
        # (``upgrading``) may be skipped when it will not cancel: leaving a live row behind
        # would collide with the ``active`` write the caller finishes on.
        paid_ids = await paid_subscription_ids(self.db, rows)
        retiring: list[tuple[PlanSubscription, bool]] = []
        for old_sub in rows:
            paid = old_sub.id in paid_ids or old_sub.current_period_start is not None
            logger.info(
                f"Superseding sub {old_sub.id} (provider {old_sub.provider_subscription_id}, "
                f"tier {old_sub.tier}, paid={paid}) in favour of {exclude_sub_id}"
            )
            if await self._cancel_on_provider(old_sub):
                retiring.append((old_sub, paid))
            elif paid or old_sub.status in ACTIVE_STATUSES:
                return False, None

        from_tier: str | None = None
        for old_sub, paid in retiring:
            if paid:
                from_tier = old_sub.tier
                old_sub.status = "cancelled"
                await self._log_event(old_sub, "cancelled_for_upgrade")
                if credit_remainder:
                    await self._credit_upgrade_remainder(old_sub)
            else:
                await self._record_checkout_retired(old_sub, cancelled=True)
        return True, from_tier

    async def _is_duplicate_event(self, event: PaymentEvent) -> bool:
        """Has this exact provider event already been recorded against any subscription?"""
        if not event.provider_event_id:
            return False
        existing = (
            await self.db.execute(
                select(PlanSubscriptionEvent.id).where(
                    PlanSubscriptionEvent.provider_event_id == event.provider_event_id
                )
            )
        ).scalar_one_or_none()
        if existing:
            logger.info(f"Duplicate event {event.provider_event_id}, skipping")
        return existing is not None

    async def _is_retired_checkout(self, sub: PlanSubscription) -> bool:
        """Has this row already been retired in favour of another subscription?

        Keyed on the audit events rather than the status: the row may have been expired by the
        sweep, by a later checkout, or by a wind-down request, and activating it now would
        cancel and prorate whatever the user has live at this moment.
        """
        return (
            await self.db.execute(
                select(PlanSubscriptionEvent.id)
                .where(
                    PlanSubscriptionEvent.subscription_id == sub.id,
                    PlanSubscriptionEvent.event_type.in_(("cancelled_for_upgrade", "expired_abandoned_checkout")),
                )
                .limit(1)
            )
        ).scalar_one_or_none() is not None

    async def _log_refused_activation(self, sub: PlanSubscription, event: PaymentEvent) -> None:
        """The user has been charged and received nothing. Container logs only retain since the
        last deploy, so the audit event — not this log line — is the durable record an operator
        finds the order id and subscription in.
        """
        logger.error(
            f"Refusing activation of retired checkout {sub.id} (user {sub.user_id}, "
            f"order {event.order_id}): the payment needs manual resolution"
        )
        # Carries the provider event id like any other recorded event, so a redelivery of the
        # same payment dedups against it instead of adding a row and an error log per delivery.
        await self._log_event(sub, "activation_refused", event.provider_event_id, {"order_id": event.order_id})

    async def _credit_unused_remainder(self, old_sub: PlanSubscription) -> None:
        """Refund the unused time of an upgraded-away cycle as prepaid (USD) credits.

        Upgrades start a NEW full-price subscription immediately and cancel the old one,
        and Revolut has no prorated plan changes — so we prorate on our side: the unused
        fraction of the old cycle times its monthly price lands on the prepaid balance.
        Idempotent via the per-subscription transaction hash.
        """
        assert old_sub.user_id is not None  # libertai rows always carry user_id
        start, end = old_sub.current_period_start, old_sub.current_period_end
        if not start or not end:
            return  # never-activated sub: nothing was paid for
        # Columns are naive UTC; normalize whatever we got (a just-refreshed aware value
        # or a naive DB read) before doing arithmetic.
        start = start.replace(tzinfo=None) if start.tzinfo else start
        end = end.replace(tzinfo=None) if end.tzinfo else end
        now = datetime.now()
        period = (end - start).total_seconds()
        remaining = (end - now).total_seconds()
        if period <= 0 or remaining <= 0:
            return
        monthly_price = get_tier(old_sub.tier, old_sub.product).price_cents / 100
        amount = round(monthly_price * min(remaining / period, 1.0), 2)
        if amount <= 0:
            return
        try:
            provider = CreditTransactionProvider(old_sub.provider)
        except ValueError:
            provider = CreditTransactionProvider.voucher
        # Same-session insert (like start_topup): atomic with the webhook transaction,
        # idempotent via the per-subscription hash.
        tx_hash = f"upgrade_remainder:{old_sub.id}"
        existing = (
            await self.db.execute(select(CreditTransaction.id).where(CreditTransaction.external_reference == tx_hash))
        ).scalar_one_or_none()
        if existing:
            return
        self.db.add(
            CreditTransaction(
                user_id=old_sub.user_id,
                amount=amount,
                amount_left=amount,
                provider=provider,
                external_reference=tx_hash,
                status=CreditTransactionStatus.completed,
                is_active=True,
            )
        )
        await self.db.flush()
        await self._log_event(old_sub, "upgrade_remainder_credited", metadata={"amount": amount})

    async def _credit_upgrade_remainder(self, old_sub: PlanSubscription) -> None:
        """Shared upgrade-remainder entry point for every product. Trials and manual grants
        collected no payment, so they credit nothing — checked before either product branch,
        so neither one's crediting logic ever runs against them."""
        if old_sub.is_trial or old_sub.provider == "manual":
            return
        if old_sub.product == PRODUCT_LIBERTAI:
            await self._credit_unused_remainder(old_sub)
        else:
            await self._credit_lclw_upgrade_remainder(old_sub)

    async def _credit_lclw_upgrade_remainder(self, old_sub: PlanSubscription) -> None:
        """Compensate the unused time of an upgraded-away LCLW cycle as extra usage credits
        (the unused fraction of the old tier's rolling-window cap), mirroring
        ``_credit_unused_remainder``'s LTAI prepaid-credit refund. Best-effort: a failed grant
        must not fail the webhook, so it is parked as ``upgrade_remainder_pending`` for the
        periodic retry sweep instead of raising.
        """
        assert old_sub.liberclaw_account_id is not None  # liberclaw rows always carry it
        if old_sub.tier not in LIBERCLAW_TIERS:
            return  # a tier retired from the registry has no cap left to prorate
        start, end = old_sub.current_period_start, old_sub.current_period_end
        if not start or not end:
            return  # never-activated sub: nothing was paid for
        start = start.replace(tzinfo=None) if start.tzinfo else start
        end = end.replace(tzinfo=None) if end.tzinfo else end
        now = datetime.now()
        period = (end - start).total_seconds()
        remaining = (end - now).total_seconds()
        if period <= 0 or remaining <= 0:
            return
        fraction = round(min(remaining / period, 1.0), 4)
        if fraction <= 0:
            return
        amount = round(LIBERCLAW_TIERS[old_sub.tier]["credits_limit"] * fraction, 2)
        if amount <= 0:
            return
        ref = f"upgrade_remainder:{old_sub.id}"
        try:
            # A SAVEPOINT: a DB-level failure inside the grant (unique collision, deadlock)
            # must roll back only to here, not poison the surrounding webhook transaction —
            # the activation this runs inside of must still be able to commit.
            async with self.db.begin_nested():
                granted = await LiberclawService.grant_extra_credits_by_account_id(
                    self.db, old_sub.liberclaw_account_id, amount, ref
                )
        except Exception:
            logger.error(f"Failed to grant LCLW upgrade remainder for sub {old_sub.id}", exc_info=True)
            await self._log_event(old_sub, "upgrade_remainder_pending", metadata={"amount": amount})
        else:
            await self._log_event(old_sub, "upgrade_remainder_credited", metadata={"amount": granted})

    # ------------------------------------------------------------------ webhook dispatch
    async def handle_event(self, event: PaymentEvent) -> None:
        # Top-ups settle first (local pending row keyed by order id).
        if event.type in (
            PaymentEventType.order_completed,
            PaymentEventType.order_failed,
        ) and await self._settle_topup(event):
            return

        sub = await self._resolve_subscription(event, lock=False)
        if not sub:
            # Expected noise: the Revolut merchant account is shared with liberclaw, so this
            # backend receives webhook events for orders/subscriptions it doesn't own.
            logger.info(f"Ignoring payment event with no matching subscription: {event}")
            return

        if sub.product == PRODUCT_LIBERCLAW and not config.LIBERCLAW_BILLING_ENABLED:
            # 200-skip, never 5xx: LCLW webhook ownership is cut over by this flag alone.
            logger.info(f"Ignoring liberclaw-owned payment event for sub {sub.id}: billing cutover flag is off")
            return

        # Ahead of the refusal checks below: a redelivery for a row that was legitimately
        # superseded since is ordinary duplicate traffic, not an incident.
        if await self._is_duplicate_event(event):
            return

        # Every check below would read a refund as a paid cycle, logging a renewal and pushing
        # the period end forward on money that was handed back.
        if event.type == PaymentEventType.order_completed and await self._is_refund_order(event.order_id):
            assert event.order_id is not None  # _is_refund_order is False without one
            order = await self._get_order(event.order_id)
            related_order_id = order.get("related_order_id")
            if related_order_id is None:
                logger.warning(f"Refund order {event.order_id} carries no related_order_id")
            metadata = {**event.metadata, "related_order_id": related_order_id}
            await self._log_event(sub, "refunded", event.provider_event_id, metadata)
            await self.db.flush()
            return

        activating = event.type == PaymentEventType.order_completed
        # Refused before anything is written: a refused row that carries an ``activated``
        # event gets an open-ended paying span in the subscription replay, because
        # ``expired_abandoned_checkout`` is not a terminal event.
        if activating and await self._is_retired_checkout(sub):
            await self._log_refused_activation(sub, event)
            return

        # Per-owner mutex for the whole webhook. Row-level ordering cannot serve here:
        # _resolve_subscription runs first (it is what yields the owner id), so any FOR UPDATE
        # it took would sit outside the ordering.
        owner = Owner.from_subscription(sub)
        await self._lock_owner(owner)
        locked_sub = await self._resolve_subscription(event, lock=True)
        if not locked_sub:
            logger.info(
                f"Sub {sub.id} no longer resolves from event {event.provider_event_id} under the "
                f"owner lock; dropping the event"
            )
            return
        sub = locked_sub
        owner = Owner.from_subscription(sub)

        # Redeliveries arriving together both clear the unlocked check above and then serialize
        # here. Without this second read the later one runs as a renewal — advancing the billing
        # cycle for a single payment — before the unique index on provider_event_id rejects it.
        if await self._is_duplicate_event(event):
            return

        # The refusal check above also ran on an unlocked read. What makes this one conclusive
        # is the FOR UPDATE the re-resolve holds on the row: any transaction retiring it has to
        # touch it too, so it has either already committed — and its event is visible here — or
        # it cannot proceed until this one ends.
        if activating and await self._is_retired_checkout(sub):
            await self._log_refused_activation(sub, event)
            return

        # A full User row (email, opt-out flag) is only needed for LTAI lifecycle emails and
        # invoicing; liberclaw resolves its own identity (bridge lookup) where it needs it.
        user: User | None = None
        if owner.product == PRODUCT_LIBERTAI:
            assert owner.user_id is not None  # libertai rows always carry user_id
            user = (await self.db.execute(select(User).where(User.id == owner.user_id))).scalar_one()

        if event.type == PaymentEventType.order_completed:
            already_activated = (
                await self.db.execute(
                    select(PlanSubscriptionEvent.id)
                    .where(
                        PlanSubscriptionEvent.subscription_id == sub.id,
                        PlanSubscriptionEvent.event_type == "activated",
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            superseded, upgraded_from = await self._supersede_other_subs(owner, exclude_sub_id=sub.id)
            if not superseded:
                detail = (
                    f"Cannot activate sub {sub.id} (owner {owner.lock_id}, order {event.order_id}): the live "
                    f"subscription it replaces could not be cancelled at the provider, so both would "
                    f"be live at once"
                )
                logger.error(detail)
                raise SupersedeFailed(detail)
            await self.db.flush()

            if sub.pending_tier and sub.pending_tier != DEFAULT_TIERS[owner.product]:
                # A scheduled paid->paid downgrade took effect: this payment is the first
                # cycle billed on the lower plan variation.
                await self._log_event(sub, "downgraded", metadata={"from": sub.tier, "to": sub.pending_tier})
                sub.tier = sub.pending_tier
                sub.pending_tier = None
            has_period = await self._refresh_cycle_dates(sub)
            # Last mutation of this row, and only reached once every row it replaces is flushed
            # non-live: the one-live-subscription index is enforced per statement, so any flush
            # while two rows are live fails.
            sub.status = "active"
            # First successful charge is "activated"; every later completed cycle is a renewal.
            await self._log_event(
                sub,
                "activated" if already_activated is None else "renewed",
                event.provider_event_id,
                event.metadata,
            )
            # If this activation completes an upgrade, record it on the new sub so the two
            # subscriptions read as a single "Go -> Plus" event downstream.
            if upgraded_from is not None and upgraded_from != sub.tier:
                await self._log_event(sub, "upgraded", metadata={"from": upgraded_from, "to": sub.tier})
            if owner.product == PRODUCT_LIBERCLAW:
                assert owner.liberclaw_account_id is not None  # liberclaw rows always carry it
                await LiberclawService.update_tier_by_account_id(self.db, owner.liberclaw_account_id, sub.tier)
            # First charge only; the send log dedups per tier across resubscriptions.
            if already_activated is None and owner.product == PRODUCT_LIBERTAI:
                assert user is not None
                await send_lifecycle_email(
                    self.db, user, f"paid_welcome_{sub.tier}", "paid_welcome", {"tier": sub.tier}
                )
            if event.order_id:
                # Last: issue_invoice holds a global advisory lock to commit, so nothing that
                # blocks on I/O (the email send above, any provider call) may run after it.
                # Not wrapped: a failed read must fail the webhook (provider redelivers) rather
                # than activate the subscription with no invoice to show for it.
                order = await self._get_order(event.order_id)
                gross, currency, tax, paid_at = order_invoice_fields(order, event.order_id)
                if owner.product == PRODUCT_LIBERTAI:
                    assert user is not None
                    if user.email is None:
                        raise RuntimeError(
                            f"User {owner.user_id} has no email; cannot issue invoice for order {event.order_id}"
                        )
                    await issue_invoice(
                        self.db,
                        user_id=sub.user_id,
                        user_email=user.email,
                        external_reference=_topup_external_ref(event.provider, event.order_id),
                        gross_minor=gross,
                        currency=currency,
                        tax_minor=tax,
                        payment_date=paid_at,
                        line_label=f"{get_tier(sub.tier, owner.product).name.capitalize()} subscription",
                        # A provider read failure leaves has_period False without stale-but-plausible
                        # dates on sub — the invoice then omits the period rather than showing one
                        # that doesn't match what actually got billed.
                        period_start=sub.current_period_start if has_period else None,
                        period_end=sub.current_period_end if has_period else None,
                    )
                else:
                    assert owner.liberclaw_account_id is not None  # liberclaw rows always carry it
                    lc_user = await LiberclawService.resolve_by_account_id(self.db, owner.liberclaw_account_id)
                    # user_id is only email-shaped for user_type == "email" — a discord/telegram
                    # id must never land in a legal invoice's buyer field.
                    if lc_user is None or lc_user.user_type != "email":
                        raise RuntimeError(
                            f"Liberclaw account {owner.liberclaw_account_id} has no email-typed bridge "
                            f"row; cannot issue invoice for order {event.order_id}"
                        )
                    email = owner.email or lc_user.user_id
                    channel = order.get("channel_data") or {}
                    await issue_invoice(
                        self.db,
                        series=SERIES_LCLW,
                        liberclaw_account_id=owner.liberclaw_account_id,
                        user_email=email,
                        external_reference=_topup_external_ref(event.provider, event.order_id),
                        gross_minor=gross,
                        currency=currency,
                        tax_minor=tax,
                        payment_date=paid_at,
                        line_label=(
                            f"LiberClaw {get_tier(sub.tier, product=PRODUCT_LIBERCLAW).name.capitalize()} subscription"
                        ),
                        period_start=sub.current_period_start if has_period else None,
                        period_end=sub.current_period_end if has_period else None,
                        provider_subscription_id=channel.get("subscription_id"),
                        cycle_id=channel.get("subscription_cycle_id"),
                    )
        # Both overdue paths below (order_failed and subscription_overdue) leave lc_users.tier
        # on its paid value for a liberclaw row: dunning keeps entitlement alive while the
        # provider retries the charge. Only the row actually ending drops the tier to free —
        # the lapsed catch-all in check_expirations is the backstop when it never ends cleanly.
        elif event.type == PaymentEventType.order_failed:
            if sub.status in UNPAID_CHECKOUT_STATUSES:
                # A card declined on the hosted checkout, not a failed subscription payment:
                # the sub was never active (nothing to be overdue about) and the owner usually
                # retries on the same order, which then completes. ``overdue`` sits inside the
                # live-subscription index, so writing it here on an upgrade checkout would
                # collide with the subscription it is meant to replace.
                await self._log_event(sub, "checkout_declined", event.provider_event_id, event.metadata)
            elif await self._order_completed(sub, event.order_id):
                # Out-of-order delivery: a declined attempt on an order that has since been
                # paid. Applying it would revoke a live, paid-for subscription.
                logger.info(f"Ignoring failure for already-completed order {event.order_id} on sub {sub.id}")
            else:
                sub.status = "overdue"
                await self._log_event(sub, "payment_failed", event.provider_event_id, event.metadata)
                if owner.product == PRODUCT_LIBERTAI:
                    assert user is not None
                    await self._send_payment_failed_email(user, sub)
        elif event.type == PaymentEventType.subscription_overdue:
            if sub.status in UNPAID_CHECKOUT_STATUSES:
                # Not a card decline — a provider-side overdue notice on a row that was never
                # paid. Left alone rather than pushed to ``overdue``, which is inside the
                # live-subscription index and would collide with the row it is replacing.
                await self._log_event(sub, "overdue_ignored_unpaid_checkout", event.provider_event_id, event.metadata)
            else:
                sub.status = "overdue"
                await self._log_event(sub, "overdue", event.provider_event_id, event.metadata)
                if owner.product == PRODUCT_LIBERTAI:
                    assert user is not None
                    await self._send_payment_failed_email(user, sub)
        elif event.type == PaymentEventType.subscription_cancelled:
            if sub.status in UNPAID_CHECKOUT_STATUSES:
                # A checkout nobody ever paid, dropped by the provider — an abandoned
                # checkout, not a subscription that ran and churned. The provider having
                # cancelled it is what makes the local row safe to retire.
                await self._record_checkout_retired(sub, cancelled=True)
            elif owner.product == PRODUCT_LIBERCLAW:
                if sub.provider_cancelled:
                    # arm 1: already terminal locally (pre-marked immediate cancel / migration).
                    # No state write, but the event is recorded with its provider event id so a
                    # redelivery dedups against it in _is_duplicate_event.
                    await self._log_event(sub, "provider_cancel_confirmed", event.provider_event_id, event.metadata)
                elif (
                    sub.status in ACTIVE_STATUSES
                    and sub.current_period_end
                    and sub.current_period_end > datetime.now()
                ):
                    # arm 2: wind-down regardless of cancel_at_period_end — an overlap cancel
                    # (superseded upgrade, provider-side action) arrives with the flag unset.
                    sub.cancel_at_period_end = True
                    sub.provider_cancelled = True
                    await self._log_event(sub, "provider_cancel_confirmed", event.provider_event_id, event.metadata)
                elif sub.status in ENDED_STATUSES:
                    # Echo of an ending already recorded elsewhere (upgrade supersede,
                    # retired checkout) — no status write, no tier write: a fresher row may
                    # already hold the account's live slot.
                    await self._log_event(sub, "provider_cancel_confirmed", event.provider_event_id, event.metadata)
                else:
                    # arm 3: a terminal cancel happening now — the cycle is already over (or
                    # unknown) and the row was still locally live.
                    sub.status = "cancelled"
                    await self._log_event(sub, "cancelled", event.provider_event_id, event.metadata)
                    await self._lclw_sync_tier_free_unless_live(owner, exclude_sub_id=sub.id)
            elif sub.status in ENDED_STATUSES or _in_wind_down_cancel_window(sub):
                # Echo of a cancel we issued (upgrade supersede, or the pre-renewal wind-down
                # cancel): the ending is already recorded, and a wind-down still owes its cycle.
                await self._log_event(sub, "provider_cancel_confirmed", event.provider_event_id, event.metadata)
            else:
                sub.status = "cancelled"
                await self._log_event(sub, "cancelled", event.provider_event_id, event.metadata)
        elif event.type == PaymentEventType.subscription_initiated:
            await self._log_event(sub, "initiated", event.provider_event_id, event.metadata)
        elif event.type == PaymentEventType.subscription_finished:
            sub.status = "expired"
            await self._log_event(sub, "finished", event.provider_event_id, event.metadata)
            if owner.product == PRODUCT_LIBERCLAW:
                await self._lclw_sync_tier_free_unless_live(owner, exclude_sub_id=sub.id)

        await self.db.flush()

    async def _lclw_sync_tier_free_unless_live(self, owner: Owner, exclude_sub_id: uuid.UUID) -> None:
        """Push ``lc_users.tier`` to free after a liberclaw row ends — unless another row for
        the same account already holds the live slot (``ACTIVE_STATUSES``), whose tier is the
        effective one and must not be stomped by a late echo of this row's own ending."""
        assert owner.liberclaw_account_id is not None  # liberclaw rows always carry it
        other_live = (
            await self.db.execute(
                select(PlanSubscription.id).where(
                    owner.sub_filter(),
                    PlanSubscription.status.in_(ACTIVE_STATUSES),
                    PlanSubscription.id != exclude_sub_id,
                )
            )
        ).scalar_one_or_none()
        if other_live is not None:
            logger.info(
                f"Skipping free-tier sync for liberclaw account {owner.liberclaw_account_id}: "
                f"row {other_live} still holds the live slot"
            )
            return
        await LiberclawService.update_tier_by_account_id(
            self.db, owner.liberclaw_account_id, DEFAULT_TIERS[PRODUCT_LIBERCLAW]
        )

    async def _send_payment_failed_email(self, user: User, sub: PlanSubscription) -> None:
        # Providers retry failed charges: pace to one notice per incident, not per attempt.
        await send_lifecycle_email(
            self.db,
            user,
            "payment_failed",
            "payment_failed",
            {"tier": sub.tier},
            transactional=True,
            once=False,
            resend_after=timedelta(days=3),
        )

    async def _order_completed(self, sub: PlanSubscription, order_id: str | None) -> bool:
        """Has this subscription already been successfully billed for ``order_id``?"""
        if not order_id:
            return False
        existing = (
            await self.db.execute(
                select(PlanSubscriptionEvent.id).where(
                    PlanSubscriptionEvent.subscription_id == sub.id,
                    PlanSubscriptionEvent.event_type.in_(("activated", "renewed")),
                    PlanSubscriptionEvent.metadata_json["order_id"].as_string() == order_id,
                )
            )
        ).scalar_one_or_none()
        return existing is not None

    async def _resolve_subscription(self, event: PaymentEvent, lock: bool = True) -> PlanSubscription | None:
        """Find the subscription an event belongs to. ``lock=False`` for the lookup that runs
        before the per-owner mutex is held, so no row lock is taken outside that ordering."""
        if event.provider_subscription_id:
            stmt = select(PlanSubscription).where(
                PlanSubscription.provider_subscription_id == event.provider_subscription_id
            )
            sub = (await self.db.execute(stmt.with_for_update() if lock else stmt)).scalar_one_or_none()
            if sub:
                return sub

        if event.order_id:
            try:
                order = await self._get_order(event.order_id)
                channel = order.get("channel_data") or {}
                rev_sub_id = channel.get("subscription_id")
                if rev_sub_id:
                    stmt = select(PlanSubscription).where(PlanSubscription.provider_subscription_id == rev_sub_id)
                    return (await self.db.execute(stmt.with_for_update() if lock else stmt)).scalar_one_or_none()
            except Exception:
                logger.warning(f"Failed to resolve order {event.order_id} to subscription", exc_info=True)
        return None

    async def _refresh_cycle_dates(self, sub: PlanSubscription) -> bool:
        """Pull the cycle window from the provider onto ``sub``. False means unknown, not
        unchanged: a failure leaves the previous cycle's dates in place, still looking current.
        """
        if not sub.provider_subscription_id:
            return False
        try:
            info = await self.provider.get_subscription(sub.provider_subscription_id)
            if info.current_cycle_start:
                sub.current_period_start = _naive_utc(info.current_cycle_start)
            if info.current_cycle_end:
                sub.current_period_end = _naive_utc(info.current_cycle_end)
            return info.current_cycle_end is not None
        except Exception:
            logger.warning("Failed to fetch cycle dates", exc_info=True)
            return False

    async def _is_refund_order(self, order_id: str | None) -> bool:
        """A refund is announced with the same completed-order event as a payment, under its own
        order id, so neither the event map nor event-id dedup separates them — only ``type``."""
        if not order_id:
            return False
        try:
            order = await self._get_order(order_id)
        except Exception:
            logger.warning(f"Failed to read order {order_id} to classify it", exc_info=True)
            return False
        return (order or {}).get("type") == "refund"

    # ------------------------------------------------------------------ periodic
    ABANDONED_UPGRADE_CHECKOUT_AGE = timedelta(hours=24)

    async def sweep_abandoned_upgrade_checkouts(self) -> list[uuid.UUID]:
        """Retire upgrade checkouts the owner never paid.

        The provider leaves an abandoned subscription payable for 30 days before cancelling it
        itself; paying one that late activates a stale row against whatever the owner has live
        by then. Cancelling here is the same action taken sooner. The provider state is checked
        first so a checkout completed moments ago is left alone.

        Candidates are collected lock-free, then each is resolved in its own transaction that
        takes the owner lock first and commits on its own. Locking the whole match set up front
        would hold every matched row across the run's provider round trips, so /upgrade,
        /cancel, /downgrade and the activation webhook of an unrelated owner would queue behind
        HTTP they have nothing to do with.
        """
        cutoff = datetime.now() - self.ABANDONED_UPGRADE_CHECKOUT_AGE
        candidates = (
            await self.db.execute(
                select(
                    PlanSubscription.id,
                    PlanSubscription.product,
                    PlanSubscription.user_id,
                    PlanSubscription.liberclaw_account_id,
                ).where(
                    self._product_scope(),
                    PlanSubscription.status == "pending_upgrade",
                    PlanSubscription.updated_at < cutoff,
                    # A paid row can never be swept: _cancel_on_provider's failures are
                    # swallowed, so a mis-selected live row would be cancelled silently.
                    PlanSubscription.current_period_start.is_(None),
                )
            )
        ).all()
        touched: list[uuid.UUID] = []
        failures = 0
        for sub_id, product, user_id, liberclaw_account_id in candidates:
            owner = Owner(product=product, user_id=user_id, liberclaw_account_id=liberclaw_account_id, email=None)
            try:
                await self._lock_owner(owner)
                sub = (
                    await self.db.execute(
                        select(PlanSubscription).where(PlanSubscription.id == sub_id).with_for_update()
                    )
                ).scalar_one_or_none()
                # The selection above read an unlocked snapshot: the row may have been paid,
                # retired or replaced while this run worked through the owners ahead of it.
                # Every exit from here ends the transaction, writes or not — the owner lock is
                # held for its lifetime and the next candidate takes its own.
                if sub is None or sub.status != "pending_upgrade" or sub.current_period_start is not None:
                    await self.db.rollback()
                    continue
                if sub.provider_subscription_id:
                    info = await self.provider.get_subscription(sub.provider_subscription_id)
                    if info.state not in ("pending", "cancelled"):
                        await self.db.rollback()
                        continue
                cancelled = await self._cancel_on_provider(sub)
                await self._record_checkout_retired(sub, cancelled=cancelled)
                await self.db.commit()
                if cancelled:
                    touched.append(owner.lock_id)
                else:
                    failures += 1
            except Exception:
                logger.warning(f"Could not sweep abandoned upgrade checkout {sub_id}", exc_info=True)
                failures += 1
                await self.db.rollback()
        if failures:
            logger.warning(
                f"{failures} abandoned upgrade checkout(s) could not be cancelled at the provider; "
                "they stay payable until the next pass"
            )
        return touched

    async def reconcile_pending(self) -> int:
        """Adopt payments the provider took but whose webhook never reached us.

        A delivery that fails is retried only a handful of times over about half an hour and
        then dropped for good, which leaves the customer charged, on the free tier, and holding
        a row indistinguishable from an abandoned checkout. Nothing else asks the provider
        whether a pending row was in fact paid. Returns the count adopted.

        The event is rebuilt with the id the provider would have sent, so a late redelivery
        dedups against it, and is replayed through ``handle_event`` rather than a second
        activation path — invoicing, supersede and the welcome email must not diverge here.
        """
        if not self.provider.supports(PaymentCapability.subscription):
            return 0
        rows = (
            (
                await self.db.execute(
                    select(PlanSubscription).where(
                        self._product_scope(),
                        PlanSubscription.status.in_(("pending", "pending_upgrade", "overdue")),
                        PlanSubscription.provider == self.provider.id,
                        PlanSubscription.provider_subscription_id.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        adopted = 0
        for sub in rows:
            # Re-read rather than trusting the query's IS NOT NULL: it narrows the column for
            # the reader, not for the type checker, and a later filter change would go unnoticed.
            provider_subscription_id = sub.provider_subscription_id
            if provider_subscription_id is None:
                continue
            try:
                event = await self.provider.missed_activation_event(provider_subscription_id)
                if event is None:
                    continue
                logger.warning(
                    f"Adopting sub {sub.id} (owner {Owner.from_subscription(sub).lock_id}): paid at the "
                    f"provider, no webhook recorded"
                )
                await self.handle_event(event)
                adopted += 1
            except Exception:
                logger.error(f"Reconcile failed for sub {sub.id}", exc_info=True)
        return adopted

    async def check_expirations(self) -> int:
        """Expire subscriptions past their period end (24h grace to avoid racing webhooks).

        Naive ``datetime.now()`` on purpose: the TIMESTAMP columns are naive and every
        other period computation (credit_subscription, entitlement) uses naive local
        time — mixing in an aware UTC cutoff here skewed the comparison on non-UTC hosts.
        """
        # Pass 0: deferred provider-side cancellation. cancel()/downgrade-to-free only flag
        # locally (Revolut cancellation is terminal, which would make resume impossible) —
        # the actual provider cancel happens here, shortly before the renewal would bill.
        # Repeat calls on an already-cancelled provider sub are swallowed by _cancel_on_provider.
        # Read without FOR UPDATE: locking the whole match set would hold rows across a provider
        # round trip each, blocking every subscription mutation of every user matched for the
        # length of the pass. The only local write is the cycle refresh below, and its values
        # come from the provider, so a concurrent webhook writing them too is not a conflict.
        pre_cutoff = datetime.now() + PROVIDER_CANCEL_LEAD
        pending_cancel = await self.db.execute(
            select(PlanSubscription).where(
                self._product_scope(),
                PlanSubscription.status.in_(["active", "overdue"]),
                PlanSubscription.cancel_at_period_end == True,
                # Already terminal at the provider (e.g. a wind-down confirmed by its own
                # cancel echo): nothing left to cancel there.
                PlanSubscription.provider_cancelled == False,
                PlanSubscription.current_period_end <= pre_cutoff,
            )
        )
        for sub in pending_cancel.scalars().all():
            # Cancelling is terminal and refunds the cycle it lands in, so a stale stored end
            # would refund a customer mid-cycle. Deferring costs one more cycle, which is
            # refundable; cancelling early is not.
            if not await self._refresh_cycle_dates(sub):
                logger.warning(
                    f"Deferring provider cancel for sub {sub.id}: cycle dates unreadable, so the "
                    f"stored period end cannot authorise a terminal cancel"
                )
                continue
            if sub.current_period_end and sub.current_period_end > pre_cutoff:
                logger.info(f"Deferring provider cancel for sub {sub.id}: provider cycle still running")
                continue
            await self._cancel_on_provider(sub)

        cutoff = datetime.now() - timedelta(hours=24)
        result = await self.db.execute(
            select(PlanSubscription)
            .where(
                self._product_scope(),
                PlanSubscription.status.in_(["active", "overdue"]),
                PlanSubscription.current_period_end < cutoff,
                PlanSubscription.cancel_at_period_end == True,
            )
            .with_for_update()
        )
        count = 0
        for sub in result.scalars().all():
            sub.status = "expired"
            new_tier = sub.pending_tier or DEFAULT_TIERS[sub.product]
            await self._log_event(sub, "expired", metadata={"new_tier": new_tier})
            if sub.product == PRODUCT_LIBERCLAW:
                assert sub.liberclaw_account_id is not None  # liberclaw rows always carry it
                await LiberclawService.update_tier_by_account_id(self.db, sub.liberclaw_account_id, new_tier)
            count += 1

        # Manual grants and trials (provider="manual") have no renewal webhook to wait for and
        # no grace period: they end exactly when they say they will. A NULL period end is an
        # open-ended override and is never touched here.
        now = datetime.now()
        manual_result = await self.db.execute(
            select(PlanSubscription)
            .where(
                self._product_scope(),
                PlanSubscription.status == "active",
                (PlanSubscription.provider == "manual") | (PlanSubscription.is_trial == True),
                PlanSubscription.current_period_end.is_not(None),
                PlanSubscription.current_period_end < now,
            )
            .with_for_update()
        )
        for sub in manual_result.scalars().all():
            sub.status = "expired"
            await self._log_event(sub, "expired", metadata={"new_tier": DEFAULT_TIERS[sub.product]})
            if sub.product == PRODUCT_LIBERCLAW:
                await self._lclw_sync_tier_free_unless_live(Owner.from_subscription(sub), exclude_sub_id=sub.id)
            count += 1

        # LCLW-only: a recurring subscription's period only moves forward on a renewal webhook.
        # Past this much longer grace than the general cancel_at_period_end pass above, none is
        # coming — there was never an explicit cancel to key off of.
        lapsed_cutoff = now - timedelta(days=RENEWAL_GRACE_DAYS)
        lapsed_result = await self.db.execute(
            select(PlanSubscription)
            .where(
                self._product_scope(),
                PlanSubscription.product == PRODUCT_LIBERCLAW,
                PlanSubscription.status.in_(("active", "overdue")),
                PlanSubscription.is_trial == False,
                PlanSubscription.current_period_end < lapsed_cutoff,
            )
            .with_for_update()
        )
        for sub in lapsed_result.scalars().all():
            sub.status = "expired"
            await self._log_event(sub, "expired", metadata={"new_tier": DEFAULT_TIERS[PRODUCT_LIBERCLAW]})
            await self._lclw_sync_tier_free_unless_live(Owner.from_subscription(sub), exclude_sub_id=sub.id)
            count += 1

        # LCLW-only: a checkout nobody ever paid, cleaned up after 24h so the account isn't
        # blocked from a fresh checkout. Routed through the single retirement helper: the
        # provider-cancel gate keeps a row payable (and thus never falsely marked dead) if the
        # provider link can't be confirmed cancelled.
        stale_pending_result = await self.db.execute(
            select(PlanSubscription)
            .where(
                self._product_scope(),
                PlanSubscription.product == PRODUCT_LIBERCLAW,
                PlanSubscription.status == "pending",
                PlanSubscription.current_period_start.is_(None),
                PlanSubscription.created_at < cutoff,
            )
            .with_for_update()
        )
        for sub in stale_pending_result.scalars().all():
            # Retried every pass until it succeeds — a first failed cancel must not leave the
            # row permanently pending. Only the audit event dedups (dedup_event=True): logging
            # it again on every retry would be one row per attempt for a link that's been
            # payable at the provider for weeks.
            cancelled = await self._cancel_on_provider(sub)
            await self._record_checkout_retired(sub, cancelled=cancelled, dedup_event=True)
            if cancelled:
                count += 1

        # An "upgrading" row is a paid subscription holding no entitlement while the provider
        # keeps billing it, so it is restored to "active" once it has sat untouched for 1h. No
        # provider call: the plan was never cancelled there. Users who already hold a live row
        # are skipped — reverting would put two rows in the one-active-sub index, and an
        # activation on that row supersedes this one anyway. No current path writes this
        # status; the pass drains the rows already carrying it.
        stale_cutoff = datetime.now() - timedelta(hours=1)
        stale = await self.db.execute(
            select(PlanSubscription)
            .where(
                self._product_scope(),
                PlanSubscription.status == "upgrading",
                PlanSubscription.updated_at < stale_cutoff,
            )
            .with_for_update()
        )
        for sub in stale.scalars().all():
            if await self._active_subscription(Owner.from_subscription(sub), lock=False):
                continue
            # A webhook can activate a new pending sub between the check above and this
            # write, tripping the one-active-sub partial unique index. A savepoint keeps
            # one collision from rolling back the whole cron batch — the row simply stays
            # "upgrading" and is resolved by the completed checkout (or the next run).
            sub_id = sub.id  # read before a potential rollback expires the instance
            try:
                async with self.db.begin_nested():
                    sub.status = "active"
                    await self._log_event(sub, "upgrade_abandoned_reverted")
                count += 1
            except IntegrityError:
                logger.info(f"Skipping upgrade revert for sub {sub_id}: owner gained an active sub concurrently")

        if count:
            await self.db.flush()
        return count
