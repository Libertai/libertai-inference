"""Manage credit campaign codes (event QR codes): create a code, open event windows, read stats.

Run from the repo root with the app's env, e.g. ``python -m scripts.credit_campaign list``.

    create  --name "Stand-up banner #1" [--code cg83tswx] [--libertai 20] [--aleph 20] [--days 60]
    window  CODE --event "Agentic AI Summit 2026" --start 2026-10-14T07:00 --end 2026-10-16T22:00 --max 300
    list
    stats   CODE
    toggle  CODE --off | --on

Times are UTC. Reusing a banner at a new event = one more ``window`` on the same code.
"""

import argparse
import asyncio
import secrets
from datetime import datetime

import httpx
from sqlalchemy import func, select

# SQLAlchemy configures every registered mapper on the first query, so import the full model set
# up front. Mirrors the list in alembic/env.py and tests/conftest.py.
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
import src.models.plan_subscription
import src.models.plan_subscription_event
import src.models.session
import src.models.user
import src.models.wallet_connection  # noqa: F401
from src.config import config
from src.interfaces.campaigns import AlephGrantStatus
from src.models.base import AsyncSessionLocal
from src.models.credit_campaign import CreditCampaign, CreditCampaignClaim, CreditCampaignWindow
from src.services.aleph_credits import ALEPH_CREDITS_PER_USD, promo_wallet_address
from src.services.campaign import utcnow

# No 0/o/1/i/l: the code may get typed from a photo of the banner.
CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.removesuffix("Z")).replace(tzinfo=None)


async def _campaign(db, code: str) -> CreditCampaign:
    campaign = (await db.execute(select(CreditCampaign).where(CreditCampaign.code == code))).scalars().first()
    if campaign is None:
        raise SystemExit(f"No campaign with code {code!r}")
    return campaign


async def _promo_balance_usd() -> float | None:
    address = promo_wallet_address()
    if address is None:
        return None
    async with httpx.AsyncClient(base_url=config.ALEPH_API_URL, timeout=30) as client:
        response = await client.get(f"/api/v0/addresses/{address}/balance")
    if response.status_code == 404:
        return 0.0
    response.raise_for_status()
    return float(response.json().get("credit_balance", 0)) / ALEPH_CREDITS_PER_USD


async def _print_funding(aleph_usd_committed: float) -> None:
    address = promo_wallet_address()
    if address is None:
        print("Aleph promo wallet: ALEPH_PROMO_PRIVATE_KEY not set — Aleph grants will queue, not send.")
        return
    try:
        balance = await _promo_balance_usd()
    except httpx.HTTPError as e:
        print(f"Aleph promo wallet {address}: balance unavailable ({e!s})")
        return
    print(f"Aleph promo wallet {address}: ${balance:,.2f} of credits")
    if balance is not None and balance < aleph_usd_committed:
        print(
            f"  ⚠ open/upcoming windows can claim up to ${aleph_usd_committed:,.2f} on Aleph: "
            f"top it up from the backoffice (credit transfer, 12-month expiry) before the event."
        )


async def create(args) -> None:
    code = args.code or "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    async with AsyncSessionLocal() as db:
        campaign = CreditCampaign(
            code=code.lower(),
            name=args.name,
            libertai_amount=args.libertai,
            aleph_amount=args.aleph,
            credit_validity_days=args.days,
        )
        db.add(campaign)
        await db.commit()
    print(f"Created {campaign.code}: ${args.libertai} LibertAI + ${args.aleph} Aleph, valid {args.days} days")
    print(f"QR URL: https://libertai.io/claim?e={campaign.code}")
    print("It accepts no claims until you add a window.")


async def window(args) -> None:
    async with AsyncSessionLocal() as db:
        campaign = await _campaign(db, args.code)
        w = CreditCampaignWindow(
            campaign_id=campaign.id,
            event_name=args.event,
            starts_at=_parse_utc(args.start),
            ends_at=_parse_utc(args.end),
            max_claims=args.max,
        )
        db.add(w)
        await db.commit()
        print(f"{campaign.code}: {w.event_name!r} open {w.starts_at} → {w.ends_at} UTC, up to {w.max_claims} claims")
        print(
            f"Worst case: ${w.max_claims * campaign.libertai_amount:,.2f} LibertAI, "
            f"${w.max_claims * campaign.aleph_amount:,.2f} Aleph"
        )
    await _print_funding(args.max * campaign.aleph_amount)


async def list_campaigns(_args) -> None:
    now = utcnow()
    committed = 0.0
    async with AsyncSessionLocal() as db:
        campaigns = (await db.execute(select(CreditCampaign).order_by(CreditCampaign.created_at))).scalars().all()
        for campaign in campaigns:
            state = "" if campaign.is_active else " [OFF]"
            print(
                f"{campaign.code}  {campaign.name}{state}  "
                f"${campaign.libertai_amount:g}+${campaign.aleph_amount:g}/{campaign.credit_validity_days}d"
            )
            windows = (
                (
                    await db.execute(
                        select(CreditCampaignWindow)
                        .where(CreditCampaignWindow.campaign_id == campaign.id)
                        .order_by(CreditCampaignWindow.starts_at)
                    )
                )
                .scalars()
                .all()
            )
            for w in windows:
                claims = await db.scalar(
                    select(func.count()).select_from(CreditCampaignClaim).where(CreditCampaignClaim.window_id == w.id)
                )
                live = "OPEN " if w.starts_at <= now < w.ends_at else ("next " if w.starts_at > now else "     ")
                print(f"  {live}{w.event_name}: {w.starts_at} → {w.ends_at} UTC, {claims}/{w.max_claims} claimed")
                if w.ends_at > now and campaign.is_active:
                    committed += (w.max_claims - int(claims or 0)) * campaign.aleph_amount
    await _print_funding(committed)


async def stats(args) -> None:
    async with AsyncSessionLocal() as db:
        campaign = await _campaign(db, args.code)
        rows = (
            await db.execute(
                select(CreditCampaignWindow.event_name, CreditCampaignClaim.aleph_status, func.count())
                .join(CreditCampaignWindow, CreditCampaignWindow.id == CreditCampaignClaim.window_id)
                .where(CreditCampaignClaim.campaign_id == campaign.id)
                .group_by(CreditCampaignWindow.event_name, CreditCampaignClaim.aleph_status)
            )
        ).all()
        failed = (
            (
                await db.execute(
                    select(CreditCampaignClaim).where(
                        CreditCampaignClaim.campaign_id == campaign.id,
                        CreditCampaignClaim.aleph_status == AlephGrantStatus.failed,
                    )
                )
            )
            .scalars()
            .all()
        )
    print(f"{campaign.code}  {campaign.name}")
    for event_name, aleph_status, count in rows:
        print(f"  {event_name}: {count} claim(s), Aleph {aleph_status.value}")
    for claim in failed:
        print(
            f"  FAILED Aleph grant {claim.id} → {claim.aleph_address} ({claim.aleph_item_hash}): {claim.aleph_error}"
        )
    if failed:
        print("  After fixing the cause (usually: top up the promo wallet), set aleph_status='pending',")
        print("  aleph_attempts=0, aleph_message=NULL on those rows; the retry job re-signs and sends.")


async def toggle(args) -> None:
    async with AsyncSessionLocal() as db:
        campaign = await _campaign(db, args.code)
        campaign.is_active = args.on
        await db.commit()
    print(f"{campaign.code} is now {'ON' if args.on else 'OFF'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create")
    p.add_argument("--name", required=True)
    p.add_argument("--code")
    p.add_argument("--libertai", type=float, default=20.0)
    p.add_argument("--aleph", type=float, default=20.0)
    p.add_argument("--days", type=int, default=60)
    p.set_defaults(func=create)

    p = sub.add_parser("window")
    p.add_argument("code")
    p.add_argument("--event", required=True)
    p.add_argument("--start", required=True, help="UTC, e.g. 2026-10-14T07:00")
    p.add_argument("--end", required=True, help="UTC")
    p.add_argument("--max", type=int, required=True, help="claim cap for this event")
    p.set_defaults(func=window)

    sub.add_parser("list").set_defaults(func=list_campaigns)

    p = sub.add_parser("stats")
    p.add_argument("code")
    p.set_defaults(func=stats)

    p = sub.add_parser("toggle")
    p.add_argument("code")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--on", dest="on", action="store_true")
    group.add_argument("--off", dest="on", action="store_false")
    p.set_defaults(func=toggle)

    args = parser.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
