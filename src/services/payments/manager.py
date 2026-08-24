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

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.models.credit_transaction import CreditTransaction
from src.models.plan_subscription import ACTIVE_STATUSES, UNPAID_CHECKOUT_STATUSES, PlanSubscription
from src.models.plan_subscription_event import PlanSubscriptionEvent
from src.models.user import User
from src.services.geo import vat_rate_for_currency
from src.services.invoice import issue_invoice
from src.services.lifecycle_email import send_lifecycle_email
from src.services.payments.base import (
    CheckoutResult,
    PaymentCapability,
    PaymentEvent,
    PaymentEventType,
    PaymentProvider,
    UnsupportedCapability,
)
from src.subscription_tiers import (
    DEFAULT_CURRENCY,
    DEFAULT_TIER,
    PAID_TIERS,
    get_tier,
    is_downgrade,
    is_upgrade,
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


def _user_lock_key(user_id: uuid.UUID) -> int:
    """Signed 32-bit objid derived from a user id, for ``USER_SUBSCRIPTION_LOCK_CLASS``.

    Two user ids sharing an objid merely serialize against each other.
    """
    return int.from_bytes(user_id.bytes[:4], "big", signed=True)


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
    async def _active_subscription(self, user_id: uuid.UUID, lock: bool = True) -> PlanSubscription | None:
        stmt = select(PlanSubscription).where(
            PlanSubscription.user_id == user_id,
            PlanSubscription.status.in_(ACTIVE_STATUSES),
        )
        if lock:
            stmt = stmt.with_for_update()
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def _lock_user(self, user_id: uuid.UUID) -> None:
        """Take the per-user mutex over subscription mutations, for the rest of the transaction.

        Every path that touches more than one of a user's rows takes it FIRST, before any row
        lock: the webhook and the checkout paths acquire the live row and the checkout row in
        opposite orders, so row-level locking alone deadlocks them.
        """
        await self.db.execute(
            select(func.pg_advisory_xact_lock(USER_SUBSCRIPTION_LOCK_CLASS, _user_lock_key(user_id)))
        )

    async def current_tier(self, user_id: uuid.UUID) -> str:
        sub = await self._active_subscription(user_id, lock=False)
        if sub and sub.status == "active":
            return sub.tier
        return DEFAULT_TIER

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
    async def _record_checkout_retired(self, sub: PlanSubscription, *, cancelled: bool) -> None:
        """Retire a never-paid checkout row. The single place any path may retire one.

        The audit event is unconditional: ``_is_retired_checkout`` keys on it, so a row whose
        provider cancel failed still refuses a later payment rather than activating as a fresh
        subscription carrying none of the state that retired it. Only the status write is gated
        on ``cancelled`` — writing ``expired`` while the link is live at the provider would mark
        the row dead locally while it stays payable.
        """
        if cancelled:
            sub.status = "expired"
        await self._log_event(sub, "expired_abandoned_checkout")

    async def _retire_unpaid_checkouts(self, user_id: uuid.UUID, statuses: tuple[str, ...]) -> None:
        """Expire the user's never-paid checkout rows in ``statuses``.

        Its own query, never ``_active_subscription``: that helper's ``scalar_one_or_none``
        matches at most one row, and a mid-upgrade user legitimately has two. Missing period
        dates select the candidates; only ``paid_subscription_ids`` decides which of them were
        never paid.
        """
        rows = (
            (
                await self.db.execute(
                    select(PlanSubscription)
                    .where(
                        PlanSubscription.user_id == user_id,
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
        for row in rows:
            if row.id in paid_ids:
                logger.warning(f"Sub {row.id} has a payment on record despite no period dates, not retiring it")
                continue
            cancelled = await self._cancel_on_provider(row)
            await self._record_checkout_retired(row, cancelled=cancelled)
        if rows:
            await self.db.flush()

    async def _open_checkout(
        self, user: User, tier: str, redirect_url: str, currency: str, status: str
    ) -> CheckoutResult:
        if tier not in PAID_TIERS:
            raise ValueError(f"Invalid paid tier: {tier}")
        if not self.provider.supports(PaymentCapability.subscription):
            raise UnsupportedCapability(f"{self.provider.id} does not support subscriptions")
        if not user.email:
            raise ValueError("User must have an email to subscribe")

        prev_customer_id = (
            await self.db.execute(
                select(PlanSubscription.provider_customer_id)
                .where(
                    PlanSubscription.user_id == user.id,
                    PlanSubscription.provider_customer_id.isnot(None),
                )
                .order_by(PlanSubscription.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        result = await self.provider.create_subscription(
            user_email=user.email,
            tier=tier,
            currency=currency,
            redirect_url=redirect_url,
            provider_customer_id=prev_customer_id,
        )
        sub = PlanSubscription(
            user_id=user.id,
            tier=tier,
            status=status,
            provider=self.provider.id,
            provider_subscription_id=result.provider_subscription_id,
            provider_customer_id=result.provider_customer_id,
            # Locked at checkout time: renewals bill through the provider's
            # currency-specific plan, so the currency never changes mid-life.
            currency=currency,
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

    async def start_checkout(self, user: User, tier: str, redirect_url: str, currency: str) -> CheckoutResult:
        await self._lock_user(user.id)
        existing = await self._active_subscription(user.id)
        if existing:
            if existing.status == "pending" and existing.current_period_start is None:
                await self._retire_unpaid_checkouts(user.id, ("pending",))
            else:
                raise ValueError("User already has an active subscription")
        # An upgrade checkout is invisible to _active_subscription, so retire it explicitly:
        # otherwise a user whose subscription lapsed mid-upgrade holds two payable checkouts.
        await self._retire_unpaid_checkouts(user.id, ("pending_upgrade",))
        return await self._open_checkout(user, tier, redirect_url, currency, "pending")

    async def upgrade(self, user: User, new_tier: str, redirect_url: str, currency: str) -> CheckoutResult:
        if new_tier not in PAID_TIERS:
            raise ValueError(f"Invalid tier: {new_tier}")
        await self._lock_user(user.id)
        # Validated against the live row's tier, not current_tier(): that returns "free" for
        # anything not exactly "active", which would admit an upgrade from no subscription at
        # all, and let an overdue higher-tier holder open a lower-tier "upgrade".
        live = (
            await self.db.execute(
                select(PlanSubscription)
                .where(
                    PlanSubscription.user_id == user.id,
                    PlanSubscription.status.in_(("active", "overdue")),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not live:
            raise ValueError("No active subscription")
        if not is_upgrade(live.tier, new_tier):
            raise ValueError(f"Cannot upgrade from {live.tier} to {new_tier}")

        await self._retire_unpaid_checkouts(user.id, ("pending_upgrade",))
        return await self._open_checkout(user, new_tier, redirect_url, currency, "pending_upgrade")

    async def cancel(self, user: User) -> dict:
        """Schedule cancellation at period end. Cancellation is TERMINAL on Revolut, so the
        provider-side cancel is DEFERRED to just before renewal (see check_expirations) —
        until then the user can resume() for free. Cancel == downgrade to free."""
        await self._lock_user(user.id)
        sub = await self._active_subscription(user.id)
        if not sub:
            raise ValueError("No active subscription")
        sub.cancel_at_period_end = True
        sub.pending_tier = DEFAULT_TIER
        await self._log_event(sub, "cancel_requested")
        # A scheduled wind-down must not leave a payable upgrade link: paying it would build a
        # fresh row that carries none of the wind-down state, silently converting the request
        # into a renewing, more expensive subscription.
        await self._retire_unpaid_checkouts(user.id, ("pending_upgrade",))
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

    async def resume(self, user: User) -> dict:
        """Undo a scheduled cancellation or paid downgrade before it takes effect."""
        sub = await self._active_subscription(user.id)
        if not sub:
            raise ValueError("No active subscription")
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
            and sub.pending_tier != DEFAULT_TIER
            and sub.provider != "manual"
            and sub.provider_subscription_id
        ):
            await self.provider.change_subscription_plan(
                sub.provider_subscription_id, tier=sub.tier, currency=sub.currency or DEFAULT_CURRENCY
            )
        sub.pending_tier = None
        sub.cancel_at_period_end = False
        await self._log_event(sub, "resumed")
        await self.db.flush()
        return {"message": "Your subscription will continue", "tier": sub.tier}

    async def request_downgrade(self, user: User, new_tier: str) -> dict:
        if new_tier not in PAID_TIERS and new_tier != DEFAULT_TIER:
            raise ValueError(f"Invalid tier: {new_tier}")
        await self._lock_user(user.id)
        current = await self.current_tier(user.id)
        if not is_downgrade(current, new_tier):
            raise ValueError(f"Cannot downgrade from {current} to {new_tier}")
        sub = await self._active_subscription(user.id)
        if not sub:
            raise ValueError("No active subscription")
        if new_tier == DEFAULT_TIER:
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
                    sub.provider_subscription_id, tier=new_tier, currency=sub.currency or DEFAULT_CURRENCY
                )
            sub.pending_tier = new_tier
            # The sub keeps renewing (on the new plan) — supersede any earlier cancel request.
            sub.cancel_at_period_end = False
        await self._log_event(sub, "downgrade_requested", metadata={"new_tier": new_tier})
        # A scheduled wind-down must not leave a payable upgrade link: paying it would build a
        # fresh row that carries none of the wind-down state, silently converting the request
        # into a renewing, more expensive subscription.
        await self._retire_unpaid_checkouts(user.id, ("pending_upgrade",))
        await self.db.flush()
        return {
            "effective_date": sub.current_period_end.isoformat() if sub.current_period_end else None,
            "new_tier": new_tier,
        }

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

    async def _supersede_other_subs(self, user_id: uuid.UUID, exclude_sub_id: uuid.UUID) -> tuple[bool, str | None]:
        """Retire the user's other live rows in favour of a just-paid subscription.

        Returns ``(ok, from_tier)``. ``ok`` is False when a row that must not be left behind —
        one that was paid for, or one still occupying the live-subscription index — could not be
        cancelled at the provider, so the caller cannot go on to activate a second live row.
        ``from_tier`` is the tier upgraded FROM when a paid row was superseded, so the caller can
        log a single ``upgraded`` event linking the pair.

        ``pending_upgrade`` is deliberately absent from the status set: ORDER_COMPLETED also
        fires for renewals of the live subscription, and including it would cancel the user's
        open checkout at the provider mid-payment. ``upgrading`` is included to catch rows
        parked by the previous release.
        """
        rows = (
            (
                await self.db.execute(
                    select(PlanSubscription).where(
                        PlanSubscription.user_id == user_id,
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
                await self._credit_unused_remainder(old_sub)
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
        monthly_price = get_tier(old_sub.tier).price_cents / 100
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

        # Ahead of the refusal checks below: a redelivery for a row that was legitimately
        # superseded since is ordinary duplicate traffic, not an incident.
        if await self._is_duplicate_event(event):
            return

        # Every check below would read a refund as a paid cycle, logging a renewal and pushing
        # the period end forward on money that was handed back.
        if event.type == PaymentEventType.order_completed and await self._is_refund_order(event.order_id):
            await self._log_event(sub, "refunded", event.provider_event_id, event.metadata)
            await self.db.flush()
            return

        activating = event.type == PaymentEventType.order_completed
        # Refused before anything is written: a refused row that carries an ``activated``
        # event gets an open-ended paying span in the subscription replay, because
        # ``expired_abandoned_checkout`` is not a terminal event.
        if activating and await self._is_retired_checkout(sub):
            await self._log_refused_activation(sub, event)
            return

        # Per-user mutex for the whole webhook. Row-level ordering cannot serve here:
        # _resolve_subscription runs first (it is what yields the user id), so any FOR UPDATE
        # it took would sit outside the ordering.
        await self._lock_user(sub.user_id)
        locked_sub = await self._resolve_subscription(event, lock=True)
        if not locked_sub:
            logger.info(
                f"Sub {sub.id} no longer resolves from event {event.provider_event_id} under the "
                f"user lock; dropping the event"
            )
            return
        sub = locked_sub

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

        user = (await self.db.execute(select(User).where(User.id == sub.user_id))).scalar_one()

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
            superseded, upgraded_from = await self._supersede_other_subs(user.id, exclude_sub_id=sub.id)
            if not superseded:
                detail = (
                    f"Cannot activate sub {sub.id} (user {user.id}, order {event.order_id}): the live "
                    f"subscription it replaces could not be cancelled at the provider, so both would "
                    f"be live at once"
                )
                logger.error(detail)
                raise SupersedeFailed(detail)
            await self.db.flush()

            if sub.pending_tier and sub.pending_tier != DEFAULT_TIER:
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
            # First charge only; the send log dedups per tier across resubscriptions.
            if already_activated is None:
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
                if user.email is None:
                    raise RuntimeError(f"User {user.id} has no email; cannot issue invoice for order {event.order_id}")
                await issue_invoice(
                    self.db,
                    user_id=sub.user_id,
                    user_email=user.email,
                    external_reference=_topup_external_ref(event.provider, event.order_id),
                    gross_minor=gross,
                    currency=currency,
                    tax_minor=tax,
                    payment_date=paid_at,
                    line_label=f"{get_tier(sub.tier).name.capitalize()} subscription",
                    # A provider read failure leaves has_period False without stale-but-plausible
                    # dates on sub — the invoice then omits the period rather than showing one
                    # that doesn't match what actually got billed.
                    period_start=sub.current_period_start if has_period else None,
                    period_end=sub.current_period_end if has_period else None,
                )
        elif event.type == PaymentEventType.order_failed:
            if sub.status in UNPAID_CHECKOUT_STATUSES:
                # A card declined on the hosted checkout, not a failed subscription payment:
                # the sub was never active (nothing to be overdue about) and the user usually
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
                await self._send_payment_failed_email(user, sub)
        elif event.type == PaymentEventType.subscription_cancelled:
            sub.status = "cancelled"
            await self._log_event(sub, "cancelled", event.provider_event_id, event.metadata)
        elif event.type == PaymentEventType.subscription_initiated:
            await self._log_event(sub, "initiated", event.provider_event_id, event.metadata)
        elif event.type == PaymentEventType.subscription_finished:
            sub.status = "expired"
            await self._log_event(sub, "finished", event.provider_event_id, event.metadata)

        await self.db.flush()

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
        before the per-user mutex is held, so no row lock is taken outside that ordering."""
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
        """Retire upgrade checkouts the user never paid.

        The provider leaves an abandoned subscription payable for 30 days before cancelling it
        itself; paying one that late activates a stale row against whatever the user has live
        by then. Cancelling here is the same action taken sooner. The provider state is checked
        first so a checkout completed moments ago is left alone.

        Candidates are collected lock-free, then each is resolved in its own transaction that
        takes the user lock first and commits on its own. Locking the whole match set up front
        would hold every matched row across the run's provider round trips, so /upgrade,
        /cancel, /downgrade and the activation webhook of an unrelated user would queue behind
        HTTP they have nothing to do with.
        """
        cutoff = datetime.now() - self.ABANDONED_UPGRADE_CHECKOUT_AGE
        candidates = (
            await self.db.execute(
                select(PlanSubscription.id, PlanSubscription.user_id).where(
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
        for sub_id, user_id in candidates:
            try:
                await self._lock_user(user_id)
                sub = (
                    await self.db.execute(
                        select(PlanSubscription).where(PlanSubscription.id == sub_id).with_for_update()
                    )
                ).scalar_one_or_none()
                # The selection above read an unlocked snapshot: the row may have been paid,
                # retired or replaced while this run worked through the users ahead of it.
                # Every exit from here ends the transaction, writes or not — the user lock is
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
                    touched.append(user_id)
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
        pre_cutoff = datetime.now() + timedelta(hours=2)
        pending_cancel = await self.db.execute(
            select(PlanSubscription).where(
                PlanSubscription.status.in_(["active", "overdue"]),
                PlanSubscription.cancel_at_period_end == True,
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
                PlanSubscription.status.in_(["active", "overdue"]),
                PlanSubscription.current_period_end < cutoff,
                (PlanSubscription.cancel_at_period_end == True) | (PlanSubscription.is_trial == True),
            )
            .with_for_update()
        )
        count = 0
        for sub in result.scalars().all():
            sub.status = "expired"
            await self._log_event(sub, "expired", metadata={"new_tier": sub.pending_tier or DEFAULT_TIER})
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
                PlanSubscription.status == "upgrading",
                PlanSubscription.updated_at < stale_cutoff,
            )
            .with_for_update()
        )
        for sub in stale.scalars().all():
            if await self._active_subscription(sub.user_id, lock=False):
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
                logger.info(f"Skipping upgrade revert for sub {sub_id}: user gained an active sub concurrently")

        if count:
            await self.db.flush()
        return count
