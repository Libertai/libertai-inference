"""One-shot + delta-rerunnable migration: copy LiberClaw's subscriptions/subscription_events
into inference's plan_subscriptions/plan_subscription_events as product='liberclaw' rows.

Runs on the LiberClaw box with an SSH tunnel open to inference's Postgres; the script itself
just takes the LC DSN on the CLI and reads the inference DSN from the normal env/config
(``DATABASE_URL``, like every other script here). LC's tables are read via reflection over
plain SQLAlchemy Core against ``--lc-dsn`` — never by importing LiberClaw's own code.

First run (no ``--delta``): copies everything, id-idempotent (``ON CONFLICT DO NOTHING``) so a
retry after a partial failure never duplicates. Prints a ``watermark`` (max LC ``updated_at``
seen) — pass it to the following ``--delta`` runs via ``--watermark`` so a row edited on BOTH
sides since that watermark is reported for manual review instead of one side silently winning.

The copy (+ overrides + bridge) commits first; the Revolut cancellation check runs afterward
against durable state (``--revolut-only`` runs just this pass), each row in its own short
transaction opened only after its HTTP call returns — never inside a write transaction, so an
open row lock never blocks a live webhook across N HTTP round trips.

A wind-down row (LC status='cancelled' with cancel_at_period_end still set and the period not
yet over, and the account holds no OTHER live LC row) is translated on copy to inference's own
deferred-cancel shape: status='active', cancel_at_period_end stays True, provider_cancelled
stays False (rule 4's durable Revolut pass confirms it independently — the translation never
pre-declares it). Otherwise nothing ever scans a non-live LC row again and the paid lc_users
tier would never demote — inference's own expiry pass handles that natively once the row looks
like every other deferred cancel. A cancel-then-resubscribe account (LC allows a second row
since 'cancelled' sits outside its own live set) would otherwise collide two 'active' rows on
uq_one_active_plan_subscription_lclw — so translation is skipped entirely whenever a live LC
row exists for the same account, and at most ONE wind-down row per account (the one whose
period runs longest) is ever translated. Untranslated rows copy verbatim as 'cancelled'.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy import create_engine, make_url
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine

# SQLAlchemy configures every registered mapper on the first query, so any relationship's string
# reference must already be resolvable — import the full model set up front rather than relying
# on this script's own handful of direct imports. Mirrors alembic/env.py / scripts/backfill_invoices.py.
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
import src.models.lifecycle_email_send
import src.models.magic_link
import src.models.oauth_connection
import src.models.session
import src.models.user_billing_details
import src.models.wallet_connection  # noqa: F401
from src.config import config
from src.models.liberclaw_user import LiberclawUser
from src.models.plan_subscription import PlanSubscription
from src.models.plan_subscription_event import PlanSubscriptionEvent
from src.services.payments.base import PaymentProvider
from src.services.payments.registry import payment_registry
from src.subscription_tiers import PRODUCT_LIBERCLAW, paid_tiers

# LC's "live" statuses (uq_one_active_plan_subscription_lclw mirrors this set on the target).
LIVE_STATUSES = ("pending", "active", "overdue")
# LC's parked-upgrade status is "upgrading"; inference's own equivalent is "pending_upgrade" —
# neither should ever appear in a straight historical copy.
UPGRADING_STATUSES = ("upgrading", "pending_upgrade")

# Subscription fields an in-place delta UPDATE is allowed to touch. Deliberately excludes
# id/user_id/created_at/product/liberclaw_account_id (immutable identity) and
# provider_cancelled (owned exclusively by the Revolut-check pass — an LC-sourced field update
# must never clobber a previously-confirmed cancellation back to False).
UPDATE_FIELDS = (
    "tier",
    "status",
    "provider",
    "provider_subscription_id",
    "provider_customer_id",
    "currency",
    "current_period_start",
    "current_period_end",
    "cancel_at_period_end",
    "pending_tier",
    "is_trial",
    "updated_at",
)

PS = PlanSubscription.__table__
PSE = PlanSubscriptionEvent.__table__
LU = LiberclawUser.__table__


def now_naive_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _sync_engine(dsn: str) -> Engine:
    """psycopg (v3), same driver family as the app's async engine (src/models/base.py)."""
    return create_engine(make_url(dsn).set(drivername="postgresql+psycopg"))


def to_naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def reflect_lc_tables(engine: Engine) -> tuple[sa.Table, sa.Table, sa.Table]:
    meta = sa.MetaData()
    return (
        sa.Table("subscriptions", meta, autoload_with=engine),
        sa.Table("subscription_events", meta, autoload_with=engine),
        sa.Table("users", meta, autoload_with=engine),
    )


def _is_wind_down(m: Any, now: datetime, live_account_ids: set[Any]) -> bool:
    """LC represents "will cancel at period end" by flipping status to 'cancelled' immediately,
    while cancel_at_period_end stays set and the period runs out later — the opposite of
    inference's own model (stays 'active' with the flag until the period truly ends). Only
    while the period genuinely hasn't ended, AND the account holds no live LC row (a
    cancel-then-resubscribe pair — LC allows it, 'cancelled' sits outside its own live set),
    is a row a translation candidate; a live row governs entitlement instead, and translating
    both would collide two 'active' rows on the target's one-live-row-per-account partial
    unique index.

    Candidacy alone does not translate a row: see ``_wind_down_ids``.
    """
    raw_period_end = m["current_period_end"]
    if raw_period_end is not None and raw_period_end.tzinfo is None:
        # LC's column is timezone=True — a naive value here would silently mis-decide this
        # control-flow branch via astimezone()'s local-time assumption. Must survive python -O.
        raise ValueError(
            "LC current_period_end must be tz-aware (schema drift?) — refusing to guess "
            "the wind-down decision from an ambiguous naive value"
        )
    period_end = to_naive_utc(raw_period_end)
    return bool(
        m["status"] == "cancelled"
        and m["cancel_at_period_end"]
        and period_end is not None
        and period_end > now
        and m["user_id"] not in live_account_ids
    )


def _wind_down_ids(lc_subs: Sequence[sa.Row], now: datetime, live_account_ids: set[Any]) -> set[Any]:
    """Ids of the rows actually translated — AT MOST ONE per account, the one whose period
    runs longest (id breaks a tie, for determinism across reruns).

    A cancel -> resubscribe -> cancel-again account holds several wind-down candidates at once;
    translating them all would collide as many 'active' rows on the target's
    one-live-row-per-account partial unique index, silently dropping every row but one. The
    losers copy verbatim as 'cancelled'.
    """
    best: dict[Any, tuple[tuple[datetime, str], Any]] = {}
    for row in lc_subs:
        m = row._mapping
        if not _is_wind_down(m, now, live_account_ids):
            continue
        period_end = to_naive_utc(m["current_period_end"])
        if period_end is None:  # unreachable: _is_wind_down requires a period end
            continue
        rank = (period_end, str(m["id"]))
        current = best.get(m["user_id"])
        if current is None or rank > current[0]:
            best[m["user_id"]] = (rank, m["id"])
    return {sub_id for _, sub_id in best.values()}


def subscription_values(row: sa.Row, wind_down_ids: set[Any]) -> dict[str, Any]:
    m = row._mapping
    return {
        "id": m["id"],
        "user_id": None,
        "tier": m["tier"],
        "status": "active" if m["id"] in wind_down_ids else m["status"],
        "provider": m["provider"],
        "provider_subscription_id": m["provider_subscription_id"],
        "provider_customer_id": m["provider_customer_id"],
        "currency": m["currency"] or "EUR",
        "current_period_start": to_naive_utc(m["current_period_start"]),
        "current_period_end": to_naive_utc(m["current_period_end"]),
        "cancel_at_period_end": m["cancel_at_period_end"],
        "pending_tier": m["pending_tier"],
        "is_trial": m["is_trial"],
        "created_at": to_naive_utc(m["created_at"]),
        "updated_at": to_naive_utc(m["updated_at"]),
        "product": "liberclaw",
        "liberclaw_account_id": m["user_id"],
        # NEW-3: never pre-declared by the translation — cancel_at_period_end stays True
        # verbatim, so the durable Revolut candidates query (rule 4) picks this row up on its
        # own and confirms (or swallows) it against the actual provider state.
        "provider_cancelled": False,
    }


def event_values(row: sa.Row, trial_granted_by_by_sub: dict[Any, Any]) -> dict[str, Any]:
    m = row._mapping
    metadata = dict(m["metadata"] or {})
    if m["event_type"] == "trial_granted" and "trial_granted_by" not in metadata:
        granted_by = trial_granted_by_by_sub.get(m["subscription_id"])
        if granted_by is not None:
            metadata["trial_granted_by"] = str(granted_by)
    return {
        "id": m["id"],
        "subscription_id": m["subscription_id"],
        "event_type": m["event_type"],
        "provider_event_id": m["provider_event_id"],
        "metadata_json": metadata or None,
        "created_at": to_naive_utc(m["created_at"]),
    }


def _trial_granted_by_map(lc_subs: Sequence[sa.Row]) -> dict[Any, Any]:
    return {
        row._mapping["id"]: row._mapping["trial_granted_by"]
        for row in lc_subs
        if row._mapping.get("trial_granted_by") is not None
    }


def _lc_subscribed_and_live_ids(lc_subs: Sequence[sa.Row]) -> tuple[set[Any], set[Any]]:
    subscribed = {row._mapping["user_id"] for row in lc_subs}
    live = {row._mapping["user_id"] for row in lc_subs if row._mapping["status"] in LIVE_STATUSES}
    return subscribed, live


def _wind_down_excluded(
    lc_users: Sequence[sa.Row], paid: set[str], subscribed_ids: set[Any], live_ids: set[Any]
) -> list[str]:
    """LC users on a paid tier who hold a (non-live) LC subscription row — LC keeps
    ``users.tier`` paid while a cancelled/expired period runs out, so these must NOT get a
    free-forever manual override; reported so the operator can see who was excluded and why.
    """
    return [
        str(row._mapping["id"])
        for row in lc_users
        if row._mapping["tier"] in paid and row._mapping["id"] in subscribed_ids and row._mapping["id"] not in live_ids
    ]


def preflight_event_collisions(inf_conn: Connection, lc_events: Sequence[sa.Row], lc_sub_ids: set[Any]) -> list[str]:
    """Abort-worthy: an LC event's id/provider_event_id already belongs to a NON-liberclaw
    (genuinely foreign) inference event. A collision against our own previously-copied rows
    (subscription_id already in this LC batch) is expected on a rerun and not a violation.
    """
    lc_ids = {row._mapping["id"] for row in lc_events}
    lc_provider_ids = {row._mapping["provider_event_id"] for row in lc_events if row._mapping["provider_event_id"]}
    rows = inf_conn.execute(
        sa.select(PSE.c.id, PSE.c.provider_event_id).where(~PSE.c.subscription_id.in_(lc_sub_ids))
    ).all()
    existing_ids = {r.id for r in rows}
    existing_provider_ids = {r.provider_event_id for r in rows if r.provider_event_id}
    return [str(x) for x in sorted(lc_ids & existing_ids, key=str)] + sorted(lc_provider_ids & existing_provider_ids)


def _insert_events(inf_conn: Connection, lc_events: Sequence[sa.Row], trial_granted_by_by_sub: dict[Any, Any]) -> int:
    if not lc_events:
        return 0
    stmt = (
        pg_insert(PSE)
        .values([event_values(row, trial_granted_by_by_sub) for row in lc_events])
        .on_conflict_do_nothing()  # bare form: catches BOTH the id PK and the provider_event_id unique constraint
        .returning(PSE.c.id)
    )
    return len(inf_conn.execute(stmt).all())


def first_copy(
    inf_conn: Connection,
    lc_subs: Sequence[sa.Row],
    lc_events: Sequence[sa.Row],
    wind_down_ids: set[Any],
) -> dict:
    lc_sub_ids = {row._mapping["id"] for row in lc_subs}

    collisions = preflight_event_collisions(inf_conn, lc_events, lc_sub_ids)
    if collisions:
        raise SystemExit(f"Aborting: event id/provider_event_id collides with non-liberclaw rows: {collisions}")

    inserted_ids: set[Any] = set()
    if lc_subs:
        stmt = (
            pg_insert(PS)
            .values([subscription_values(row, wind_down_ids) for row in lc_subs])
            .on_conflict_do_nothing()
            .returning(PS.c.id)
        )
        inserted_ids = {r.id for r in inf_conn.execute(stmt).all()}

    inserted_events = _insert_events(inf_conn, lc_events, _trial_granted_by_map(lc_subs))

    # Report only what was actually WRITTEN this run — a rerun's ON CONFLICT DO NOTHING skip
    # must not keep re-reporting the same ids as freshly translated.
    translated = [str(sub_id) for sub_id in sorted(inserted_ids & wind_down_ids, key=str)]

    seen: list[datetime] = [ts for row in lc_subs if (ts := to_naive_utc(row._mapping["updated_at"])) is not None]
    watermark_seen = max(seen, default=None)
    return {
        "inserted": len(inserted_ids),
        "updated": 0,
        "diff": [],
        "inserted_events": inserted_events,
        "wind_down_translated": translated,
        "re_elected": 0,
        "watermark": watermark_seen.isoformat() if watermark_seen else None,
        "warn_stale_watermark": False,
    }


def _differs(inf_row: sa.Row, translated: dict[str, Any]) -> bool:
    for field in UPDATE_FIELDS:
        if field == "updated_at":
            continue
        if getattr(inf_row, field) != translated[field]:
            return True
    return False


def delta_copy(
    inf_conn: Connection,
    lc_subs: Sequence[sa.Row],
    lc_events: Sequence[sa.Row],
    watermark: datetime | None,
    wind_down_ids: set[Any],
) -> dict:
    lc_sub_ids = {row._mapping["id"] for row in lc_subs}
    existing = (
        {row.id: row for row in inf_conn.execute(sa.select(PS).where(PS.c.id.in_(lc_sub_ids))).all()}
        if lc_sub_ids
        else {}
    )

    # The account's elected wind-down row can change between runs (a later wind-down row
    # appears and wins it). Inference still holds the previous winner as translated-'active',
    # so writing the new winner would collide on the one-live-row-per-account partial unique
    # index — the loser must give up the live slot FIRST, hence: demote, then update, then
    # insert. The delta's updated_at guard cannot carry this demotion: the loser's own LC row
    # may not have changed at all since it was copied. Election state is derived by this
    # script, not sourced from LC, so it is written unconditionally.
    winner_accounts = {row._mapping["user_id"] for row in lc_subs if row._mapping["id"] in wind_down_ids}
    re_elected: list[str] = []
    demoted_ids: set[Any] = set()
    for row in lc_subs:
        m = row._mapping
        inf_row = existing.get(m["id"])
        if inf_row is None or m["id"] in wind_down_ids or m["user_id"] not in winner_accounts:
            continue
        if inf_row.status == "active" and m["status"] != "active":
            # The FULL translated state, not just the status: this write lands the LC updated_at
            # verbatim, which then equals the guarded update's own comparison value below and
            # blocks it — so any other LC-side change on this row (tier, period, flags) has to
            # ride along here or it would not sync at all this run. updated_at is pinned
            # explicitly for the same reason the guard needs it: the column's onupdate would
            # stamp wall-clock time, poisoning the next delta's watermark math.
            demotion_values = {k: v for k, v in subscription_values(row, wind_down_ids).items() if k in UPDATE_FIELDS}
            inf_conn.execute(sa.update(PS).where(PS.c.id == m["id"]).values(**demotion_values))
            re_elected.append(str(m["id"]))
            demoted_ids.add(m["id"])
    if re_elected:
        print(f"INFO wind-down re-election — previous winner(s) demoted to their LC status: {re_elected}")

    # Translated ids are collected only at the point of an actual write (a successful update
    # here, or an insert below) — never for a row merely elected as translatable.
    translated: list[str] = []
    updated = 0
    diff_rows: list[str] = []
    for row in lc_subs:
        m = row._mapping
        sub_id = m["id"]
        inf_row = existing.get(sub_id)
        if inf_row is None or sub_id in demoted_ids:
            # A demoted row already holds this run's full LC state; its guarded update could
            # only be a no-op (equal timestamps) or a re-write of what was just written.
            continue
        values = subscription_values(row, wind_down_ids)
        lc_updated = values["updated_at"]
        both_modified_since_watermark = (
            watermark is not None
            and inf_row.updated_at is not None
            and inf_row.updated_at > watermark  # strictly after: AT the watermark means "unchanged since"
            and lc_updated is not None
            and lc_updated > watermark
            and _differs(inf_row, values)
        )
        if both_modified_since_watermark:
            diff_rows.append(str(sub_id))
            continue
        update_values = {k: v for k, v in values.items() if k in UPDATE_FIELDS}
        update_stmt = sa.update(PS).where(PS.c.id == sub_id, PS.c.updated_at < lc_updated).values(**update_values)
        if inf_conn.execute(update_stmt).rowcount:
            updated += 1
            if sub_id in wind_down_ids:
                translated.append(str(sub_id))

    missing = [row for row in lc_subs if row._mapping["id"] not in existing]
    inserted_ids: set[Any] = set()
    if missing:
        stmt = (
            pg_insert(PS)
            .values([subscription_values(row, wind_down_ids) for row in missing])
            .on_conflict_do_nothing()
            .returning(PS.c.id)
        )
        inserted_ids = {r.id for r in inf_conn.execute(stmt).all()}
    translated += [str(sub_id) for sub_id in sorted(inserted_ids & wind_down_ids, key=str)]

    inserted_events = _insert_events(inf_conn, lc_events, _trial_granted_by_map(lc_subs))

    newer_than_watermark = 0
    if watermark is not None:
        newer_than_watermark = sum(
            1 for row in lc_subs if (to_naive_utc(row._mapping["updated_at"]) or watermark) > watermark
        )
    warn_stale_watermark = len(inserted_ids) == 0 and updated == 0 and not diff_rows and newer_than_watermark > 0
    if warn_stale_watermark:
        print(
            f"WARNING: {newer_than_watermark} LC row(s) are newer than the watermark but nothing was "
            "inserted/updated/diffed this run — check the --watermark value"
        )

    return {
        "inserted": len(inserted_ids),
        "updated": updated,
        "diff": diff_rows,
        "inserted_events": inserted_events,
        "wind_down_translated": translated,
        "re_elected": len(re_elected),
        "warn_stale_watermark": warn_stale_watermark,
    }


def _revolut_candidates(inf_engine: Engine, lc_subs: Sequence[sa.Row]) -> list[dict[str, Any]]:
    """N1: durable, re-runnable candidate set — every LC row with cancel_at_period_end set
    whose inference row STILL has provider_cancelled=False, joined by id against inference's
    CURRENT state rather than which ids this particular invocation's copy step touched. A crash
    between the copy commit and this pass, a prior "unknown" GET failure, or simply never having
    run this pass before are all picked up identically on the next invocation (or ``--revolut-only``).
    A row LC no longer flags cancel_at_period_end on is never a candidate, even if inference's
    own (possibly stale, not yet delta-synced) copy still shows the flag set.

    ``updated_at`` in each candidate is read from INFERENCE, not LC (NEW-1): the optimistic
    guard in ``apply_revolut_check`` compares against this same value on write-back, so it must
    be what's ACTUALLY stored — using LC's current value instead would never converge once LC's
    own updated_at has moved past what a stale (not yet delta-synced) copy holds, reporting a
    false "lost the race" on every single check even with zero real webhook contention.
    """
    lc_flagged = {
        row._mapping["id"]: row._mapping
        for row in lc_subs
        if row._mapping["cancel_at_period_end"] and row._mapping["provider_subscription_id"]
    }
    if not lc_flagged:
        return []
    with inf_engine.connect() as conn:
        rows = conn.execute(
            sa.select(PS.c.id, PS.c.updated_at).where(
                PS.c.id.in_(lc_flagged.keys()), PS.c.provider_cancelled.is_(False)
            )
        ).all()
    return [
        {
            "id": r.id,
            "provider_subscription_id": lc_flagged[r.id]["provider_subscription_id"],
            "updated_at": r.updated_at,
        }
        for r in rows
    ]


async def apply_revolut_check(
    inf_engine: Engine, provider: PaymentProvider, candidates: list[dict[str, Any]], *, persist: bool
) -> dict:
    """Rule 4: GET the Revolut subscription for each candidate and only mark provider_cancelled
    True when it reports state == 'cancelled'.

    Runs AFTER the copy transaction commits, one short transaction per row opened only after
    its HTTP call returns (I3) — never holds a write transaction across an HTTP round trip.
    Writes ``updated_at`` back to the exact value the candidate carries (C1): leaving it out of
    ``.values()`` would let the column's ``onupdate`` stamp wall-clock now(), which would then
    wrongly land the row on every future delta's manual-diff list. The UPDATE also carries an
    optimistic guard (N3/NEW-1) on that same ``updated_at`` — if a live webhook wrote the row
    first, this GET's answer is now stale and must not stomp the webhook's write; the row is
    reported as a lost race instead, and ``confirmed_cancelled`` is only incremented once the
    write actually lands (never claimed on a lost race).
    A request failure (timeout, 5xx, ...) never aborts the run — it's counted as "unknown" and
    provider_cancelled is left exactly as the copy step wrote it, to be retried next run.
    """
    if not candidates:
        return {"checked": 0, "confirmed_cancelled": 0, "swallowed": [], "unknown": [], "lost_race": []}
    confirmed = 0
    swallowed: list[str] = []
    unknown: list[str] = []
    lost_race: list[str] = []
    for i, c in enumerate(candidates):
        if i:
            await asyncio.sleep(0.1)  # polite batching, matches the cycles endpoint
        try:
            info = await provider.get_subscription(c["provider_subscription_id"])
        except httpx.HTTPError as e:
            unknown.append(str(c["id"]))
            print(f"WARNING revolut check failed for sub {c['id']}: {e!r} — provider_cancelled left untouched")
            continue
        is_cancelled = info.state == "cancelled"
        if not is_cancelled:
            swallowed.append(str(c["id"]))
            print(
                f"WARNING swallowed cancel: sub {c['id']} has cancel_at_period_end set but Revolut "
                f"reports state={info.state!r} — operator should confirm/cancel manually"
            )
        if not persist:
            if is_cancelled:
                confirmed += 1
            continue
        with inf_engine.begin() as conn:
            result = conn.execute(
                sa.update(PS)
                .where(PS.c.id == c["id"], PS.c.updated_at == c["updated_at"])
                .values(provider_cancelled=is_cancelled, updated_at=c["updated_at"])
            )
        if result.rowcount == 0:
            lost_race.append(str(c["id"]))
            print(
                f"WARNING revolut check lost the race for sub {c['id']} — a webhook updated "
                "it first; provider_cancelled left as the webhook wrote it"
            )
        elif is_cancelled:
            confirmed += 1
    return {
        "checked": len(candidates),
        "confirmed_cancelled": confirmed,
        "swallowed": swallowed,
        "unknown": unknown,
        "lost_race": lost_race,
    }


def synthesize_overrides(
    inf_conn: Connection,
    lc_users: Sequence[sa.Row],
    paid: set[str],
    lc_subscribed_ids: set[Any],
    now: datetime,
) -> dict:
    """Rule 5 (corrected per C2): an LC user on a paid tier gets a manual, open-ended active
    override ONLY when they hold NO LC subscription row at all — a winding-down payer (LC
    keeps ``users.tier`` paid while a cancelled/expired period runs out) is excluded here; the
    caller reports that exclusion set (``_wind_down_excluded``) separately, since it needs no
    inference read and must be visible even under ``--dry-run``.
    """
    existing_live = {
        row.liberclaw_account_id
        for row in inf_conn.execute(
            sa.select(PS.c.liberclaw_account_id).where(
                PS.c.product == "liberclaw", PS.c.status.in_(LIVE_STATUSES), PS.c.liberclaw_account_id.isnot(None)
            )
        ).all()
    }
    created_for_user_ids: set[Any] = set()
    skipped_race: list[str] = []
    for row in lc_users:
        m = row._mapping
        user_id = m["id"]
        if m["tier"] not in paid or user_id in lc_subscribed_ids or user_id in existing_live:
            continue
        sub_id = uuid.uuid4()
        # Delta guard: a comped user who subscribed for real between runs already has a live
        # row by now — bare ON CONFLICT DO NOTHING (any unique/partial-index violation, not
        # just id) skips this insert instead of a UniqueViolation aborting the whole delta.
        inserted = inf_conn.execute(
            pg_insert(PS)
            .values(
                id=sub_id,
                user_id=None,
                tier=m["tier"],
                status="active",
                provider="manual",
                provider_subscription_id=None,
                provider_customer_id=None,
                currency="EUR",
                current_period_start=None,
                current_period_end=None,
                cancel_at_period_end=False,
                pending_tier=None,
                is_trial=False,
                created_at=now,
                updated_at=now,
                product="liberclaw",
                liberclaw_account_id=user_id,
                provider_cancelled=False,
            )
            .on_conflict_do_nothing()
            .returning(PS.c.id)
        ).all()
        if not inserted:
            skipped_race.append(str(user_id))
            continue
        inf_conn.execute(
            sa.insert(PSE).values(
                id=uuid.uuid4(),
                subscription_id=sub_id,
                event_type="override_migrated",
                provider_event_id=None,
                metadata_json={"source": "migration", "lc_tier": m["tier"], "trial_granted_by": None},
                created_at=now,
            )
        )
        existing_live.add(user_id)
        created_for_user_ids.add(user_id)
    if skipped_race:
        print(f"WARNING override synthesis skipped (a live row now exists — concurrent subscribe): {skipped_race}")
    return {"created_for_user_ids": created_for_user_ids, "skipped_race": skipped_race}


def bridge_completion(
    inf_conn: Connection, lc_users_by_id: dict[Any, sa.Row], target_ids: set[Any], now: datetime
) -> dict:
    """Rule 6: direct INSERT of missing lc_users rows — never via get_or_create_api_key.

    Resolves by liberclaw_account_id first, then by (email, 'email') among rows with NO
    account id bound yet (C3 + N5): a legacy pre-billing bridge row (liberclaw_account_id
    NULL is a documented production reality) is UPDATEd in place so any API key already FK'd
    to its id inherits the bridge — inserting a second row for the same email instead would
    crash on the unique constraint. A row whose email already resolves but is bound to a
    DIFFERENT account id is a genuine identity conflict (never silently reassigned) — reported
    instead. A fresh row is only ever inserted when neither lookup resolves.
    """
    if not target_ids:
        return {"created": 0, "updated": 0, "skipped_no_email": [], "conflicts": []}
    by_account_id = {
        row.liberclaw_account_id
        for row in inf_conn.execute(
            sa.select(LU.c.liberclaw_account_id).where(LU.c.liberclaw_account_id.in_(target_ids))
        ).all()
    }
    created = 0
    updated = 0
    skipped_no_email: list[str] = []
    conflicts: list[str] = []
    for uid in sorted(target_ids - by_account_id, key=str):
        lc_user = lc_users_by_id.get(uid)
        if lc_user is None:
            continue
        email = lc_user._mapping["email"]
        if email is None:
            skipped_no_email.append(str(uid))
            continue
        tier = lc_user._mapping["tier"]
        legacy_row = inf_conn.execute(
            sa.select(LU.c.id).where(
                LU.c.user_id == email, LU.c.user_type == "email", LU.c.liberclaw_account_id.is_(None)
            )
        ).first()
        if legacy_row is not None:
            inf_conn.execute(sa.update(LU).where(LU.c.id == legacy_row.id).values(liberclaw_account_id=uid, tier=tier))
            updated += 1
            continue
        conflict_row = inf_conn.execute(
            sa.select(LU.c.liberclaw_account_id).where(LU.c.user_id == email, LU.c.user_type == "email")
        ).first()
        if conflict_row is not None:
            conflicts.append(f"{uid} email={email} already bound to {conflict_row.liberclaw_account_id}")
            continue
        inf_conn.execute(
            sa.insert(LU).values(
                id=uuid.uuid4(), user_id=email, user_type="email", tier=tier, liberclaw_account_id=uid, created_at=now
            )
        )
        created += 1
    if conflicts:
        print(f"WARNING bridge conflicts (email already bound to a different account id): {conflicts}")
    return {"created": created, "updated": updated, "skipped_no_email": skipped_no_email, "conflicts": conflicts}


def _report(name: str, violations: list) -> bool:
    passed = not violations
    suffix = f" -> {violations[:20]}" if violations else ""
    print(f"{'PASS' if passed else 'FAIL'} {name}: {len(violations)} violation(s){suffix}")
    return passed


def verify_source(lc_subs: Sequence[sa.Row], lc_users_by_id: dict[Any, sa.Row]) -> bool:
    """Checks over LC data alone — no inference read, so these run even under ``--dry-run``."""
    ok = True
    upgrading = [str(row._mapping["id"]) for row in lc_subs if row._mapping["status"] in UPGRADING_STATUSES]
    _report("zero upgrading rows (LC source)", upgrading)
    if upgrading:
        # Abort before any write, like preflight_event_collisions: an LC 'upgrading' row has no
        # inference equivalent, so a run that copied one would have to be unpicked by hand.
        raise SystemExit(f"Aborting: LC source holds upgrading row(s): {upgrading[:20]}")

    subscribed_ids = {row._mapping["user_id"] for row in lc_subs}
    ok &= _report(
        "no subscribed LC user lacks email",
        [
            str(uid)
            for uid in subscribed_ids
            if uid in lc_users_by_id and lc_users_by_id[uid]._mapping["email"] is None
        ],
    )
    return ok


def verify_target(
    inf_conn: Connection,
    lc_subs: Sequence[sa.Row],
    lc_users: Sequence[sa.Row],
    paid: set[str],
    diff_ids: set[str],
    wind_down_ids: set[Any],
) -> bool:
    """Checks against the committed inference state — skipped entirely under ``--dry-run``,
    since nothing was persisted to check meaningfully."""
    ok = True

    lc_sub_ids = {row._mapping["id"] for row in lc_subs}
    compare_ids = {sid for sid in lc_sub_ids if str(sid) not in diff_ids}
    # Compare against each row's TRANSLATED status — a wind-down row legitimately sits as
    # 'active' on the target while LC's own raw status still says 'cancelled'.
    lc_status_counts: Counter = Counter(
        subscription_values(row, wind_down_ids)["status"] for row in lc_subs if row._mapping["id"] in compare_ids
    )
    inf_status_counts: dict[str, int] = (
        {
            status: count
            for status, count in inf_conn.execute(
                sa.select(PS.c.status, sa.func.count())
                .where(PS.c.product == "liberclaw", PS.c.id.in_(compare_ids))
                .group_by(PS.c.status)
            ).all()
        }
        if compare_ids
        else {}
    )
    mismatches = [
        f"{status}: lc={count} inf={inf_status_counts.get(status, 0)}"
        for status, count in lc_status_counts.items()
        if inf_status_counts.get(status, 0) != count
    ]
    ok &= _report("per-status counts match (excluding manual-diff rows, wind-down translated)", mismatches)

    # Minor 1: an honest existence check — every paid-tier LC user with no LC subscription row
    # must hold SOME live inference liberclaw row (native or manual), not a count-equality that
    # goes vacuously trivial once N2(a)'s live-row lookup and the override step's own
    # just-created row both key off the exact same "does a live row exist" query.
    subscribed_ids = {row._mapping["user_id"] for row in lc_subs}
    candidate_eligible_ids = {
        row._mapping["id"]
        for row in lc_users
        if row._mapping["tier"] in paid and row._mapping["id"] not in subscribed_ids
    }
    covered_ids = (
        {
            row.liberclaw_account_id
            for row in inf_conn.execute(
                sa.select(PS.c.liberclaw_account_id).where(
                    PS.c.product == "liberclaw",
                    PS.c.status.in_(LIVE_STATUSES),
                    PS.c.liberclaw_account_id.in_(candidate_eligible_ids),
                )
            ).all()
        }
        if candidate_eligible_ids
        else set()
    )
    ok &= _report(
        "every paid-tier LC user with no LC subscription has a live inference row",
        [str(uid) for uid in candidate_eligible_ids - covered_ids],
    )

    dup_live = inf_conn.execute(
        sa.select(PS.c.liberclaw_account_id, sa.func.count())
        .where(PS.c.product == "liberclaw", PS.c.status.in_(LIVE_STATUSES), PS.c.liberclaw_account_id.isnot(None))
        .group_by(PS.c.liberclaw_account_id)
        .having(sa.func.count() > 1)
    ).all()
    ok &= _report("live-sub partial unique holds", [str(tuple(r)) for r in dup_live])

    dup_provider_ids = inf_conn.execute(
        sa.select(PS.c.provider_subscription_id, sa.func.count())
        .where(PS.c.provider_subscription_id.isnot(None))
        .group_by(PS.c.provider_subscription_id)
        .having(sa.func.count() > 1)
    ).all()
    ok &= _report("provider_subscription_id globally unique", [str(tuple(r)) for r in dup_provider_ids])

    activated_sub_ids = sa.select(PSE.c.subscription_id).where(PSE.c.event_type == "activated")
    missing_activated = inf_conn.execute(
        sa.select(PS.c.id).where(
            PS.c.product == "liberclaw",
            sa.or_(PS.c.current_period_start.isnot(None), PS.c.current_period_end.isnot(None)),
            ~PS.c.id.in_(activated_sub_ids),
        )
    ).all()
    ok &= _report("every sub with period dates has an activated event", [str(r.id) for r in missing_activated])

    # Durable across re-runs — every account holding a manual+active inference row, regardless
    # of which run created it, rather than this run's own run-local created-ids set.
    manual_active_ids = {
        row.liberclaw_account_id
        for row in inf_conn.execute(
            sa.select(PS.c.liberclaw_account_id).where(
                PS.c.product == "liberclaw",
                PS.c.provider == "manual",
                PS.c.status == "active",
                PS.c.liberclaw_account_id.isnot(None),
            )
        ).all()
    }
    # Same target definition bridge_completion works from — it can only ever bridge an account
    # LC's own users table knows (it needs that row's email). A manual row for an account
    # outside it (a post-cutover trial, an admin override) is out of this migration's scope;
    # demanding a bridge row for it would fail verify forever with no way to satisfy it.
    lc_user_ids = {row._mapping["id"] for row in lc_users}
    bridge_target_ids = subscribed_ids | (manual_active_ids & lc_user_ids)
    bridged_ids = (
        {
            row.liberclaw_account_id
            for row in inf_conn.execute(
                sa.select(LU.c.liberclaw_account_id).where(LU.c.liberclaw_account_id.in_(bridge_target_ids))
            ).all()
        }
        if bridge_target_ids
        else set()
    )
    ok &= _report(
        "bridge complete for every LC user holding any subscription row",
        [str(uid) for uid in bridge_target_ids if uid not in bridged_ids],
    )

    return ok


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lc-dsn", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--delta", action="store_true")
    parser.add_argument("--skip-revolut-check", action="store_true")
    parser.add_argument("--watermark")
    parser.add_argument(
        "--revolut-only",
        action="store_true",
        help="Run only the Revolut cancellation check pass (N1), against durable inference state.",
    )
    args = parser.parse_args(argv)
    if args.skip_revolut_check and not args.dry_run:
        parser.error("--skip-revolut-check is only valid with --dry-run")
    if args.delta and not args.watermark and not args.revolut_only:
        parser.error("--delta requires --watermark=<iso>, from the first run's printed watermark")
    return args


async def run(args: argparse.Namespace) -> dict:
    lc_engine = _sync_engine(args.lc_dsn)
    inf_engine = _sync_engine(config.DATABASE_URL)
    now = now_naive_utc()
    paid = paid_tiers(product=PRODUCT_LIBERCLAW)

    subs_table, events_table, users_table = reflect_lc_tables(lc_engine)
    # I4: one REPEATABLE READ transaction across all three reads — a torn snapshot (e.g. an
    # event for a subscription inserted after the subscriptions SELECT) would FK-abort the
    # event insert on the inference side, or silently drop it.
    with lc_engine.connect() as lc_conn:
        lc_conn = lc_conn.execution_options(isolation_level="REPEATABLE READ")
        with lc_conn.begin():
            lc_users = lc_conn.execute(sa.select(users_table)).all()
            lc_subs = lc_conn.execute(sa.select(subs_table)).all()
            lc_events = lc_conn.execute(sa.select(events_table)).all() if not args.revolut_only else []

    if args.revolut_only:
        if args.skip_revolut_check:
            revolut_report: dict = {
                "checked": 0,
                "confirmed_cancelled": 0,
                "swallowed": [],
                "unknown": [],
                "lost_race": [],
            }
        else:
            provider = payment_registry.get("revolut")
            candidates = _revolut_candidates(inf_engine, lc_subs)
            revolut_report = await apply_revolut_check(inf_engine, provider, candidates, persist=not args.dry_run)
        # Minor 4: exit stays 0 (operator-decides posture, matching rule 4 generally) but an
        # unresolved row is never silently green — a top-level WARNING always calls it out.
        unresolved = len(revolut_report["unknown"]) + len(revolut_report["lost_race"])
        if unresolved:
            print(
                f"WARNING: revolut-only run finished with {len(revolut_report['unknown'])} unknown and "
                f"{len(revolut_report['lost_race'])} lost-race row(s) unresolved — review needed"
            )
        return {"mode": "revolut-only", "dry_run": args.dry_run, "revolut": revolut_report, "verify_ok": True}

    watermark = datetime.fromisoformat(args.watermark) if args.watermark else None
    lc_users_by_id = {row._mapping["id"]: row for row in lc_users}
    lc_subscribed_ids, lc_live_ids = _lc_subscribed_and_live_ids(lc_subs)
    wind_down_ids = _wind_down_ids(lc_subs, now, lc_live_ids)
    wind_down_excluded = _wind_down_excluded(lc_users, paid, lc_subscribed_ids, lc_live_ids)
    if wind_down_excluded:
        print(f"INFO override synthesis excluded (winding down — LC subscription row exists): {wind_down_excluded}")

    # Source-side verify always runs, --dry-run included — no inference read needed for it.
    source_ok = verify_source(lc_subs, lc_users_by_id)

    with inf_engine.connect() as inf_conn:
        trans = inf_conn.begin()
        try:
            if args.delta:
                copy_report = delta_copy(inf_conn, lc_subs, lc_events, watermark, wind_down_ids)
            else:
                copy_report = first_copy(inf_conn, lc_subs, lc_events, wind_down_ids)

            override_report = synthesize_overrides(inf_conn, lc_users, paid, lc_subscribed_ids, now)
            target_ids = lc_subscribed_ids | override_report["created_for_user_ids"]
            bridge_report = bridge_completion(inf_conn, lc_users_by_id, target_ids, now)

            if args.dry_run:
                trans.rollback()
            else:
                trans.commit()
        except Exception:
            trans.rollback()
            raise

        target_ok = True
        if not args.dry_run:
            target_ok = verify_target(
                inf_conn,
                lc_subs,
                lc_users,
                paid,
                set(copy_report.get("diff", [])),
                wind_down_ids,
            )

    # I3 + N1: the Revolut pass runs AFTER the copy commits, never inside a write transaction,
    # against durable state — re-running it (including via --revolut-only) always catches up.
    if args.skip_revolut_check:
        revolut_report = {"checked": 0, "confirmed_cancelled": 0, "swallowed": [], "unknown": [], "lost_race": []}
    else:
        provider = payment_registry.get("revolut")
        candidates = _revolut_candidates(inf_engine, lc_subs)
        revolut_report = await apply_revolut_check(inf_engine, provider, candidates, persist=not args.dry_run)

    return {
        "mode": "delta" if args.delta else "first-copy",
        "dry_run": args.dry_run,
        **copy_report,
        "revolut": revolut_report,
        "overrides_created": len(override_report["created_for_user_ids"]),
        "overrides_skipped_race": override_report["skipped_race"],
        "wind_down_excluded": wind_down_excluded,
        "bridge": bridge_report,
        "verify_ok": source_ok and target_ok,
    }


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = await run(args)
    print(report)
    return 0 if report["verify_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
