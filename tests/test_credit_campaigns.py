"""Credit campaigns: event codes, their windows, one claim per person, and the Aleph Cloud half.

Runs the real service against the committed test DB (services open their own sessions), so every
test cleans up its own rows. Aleph network calls are stubbed; signing is real.
"""

import json
import uuid
from datetime import timedelta
from hashlib import sha256

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import HTTPException
from sqlalchemy import delete, select

from src.config import config
from src.interfaces.campaigns import AlephGrantStatus
from src.models.base import AsyncSessionLocal
from src.models.credit_campaign import CreditCampaign, CreditCampaignClaim, CreditCampaignWindow
from src.models.credit_transaction import CreditTransaction
from src.models.user import User
from src.services import aleph_credits
from src.services import campaign as svc
from src.services.auth_tokens import create_access_token
from src.services.credit import CreditService

pytestmark = pytest.mark.asyncio

PROMO_KEY = "0x" + "11" * 32
EVM = "0x52908400098527886e0f7030069857d2e4169ee7"  # EIP-55 test vector, lowercased
EVM_CHECKSUM = "0x52908400098527886E0F7030069857D2E4169EE7"
SOLANA = "4Nd1mBQtrMJVYVfKf2PJy9NZUZdTAsp7D4xWLs4gDB4T"


async def _user(email: str | None = None) -> User:
    async with AsyncSessionLocal() as db:
        user = User(email=email, address=None if email else f"0x{uuid.uuid4().hex[:40]}")
        db.add(user)
        await db.commit()
        return user


async def _campaign(windows: list[tuple[str, int, int, int]], aleph_amount: float = 20.0) -> CreditCampaign:
    """windows: (event name, start offset h, end offset h, max claims), offsets from now."""
    now = svc.utcnow()
    async with AsyncSessionLocal() as db:
        campaign = CreditCampaign(
            code=uuid.uuid4().hex[:8],
            name="test banner",
            libertai_amount=20.0,
            aleph_amount=aleph_amount,
            credit_validity_days=60,
        )
        db.add(campaign)
        await db.flush()
        for name, start, end, cap in windows:
            db.add(
                CreditCampaignWindow(
                    campaign_id=campaign.id,
                    event_name=name,
                    starts_at=now + timedelta(hours=start),
                    ends_at=now + timedelta(hours=end),
                    max_claims=cap,
                )
            )
        await db.commit()
        return campaign


@pytest.fixture
async def cleanup():
    campaigns: list[CreditCampaign] = []
    users: list[User] = []
    yield campaigns, users
    async with AsyncSessionLocal() as db:
        for campaign in campaigns:
            await db.execute(delete(CreditCampaign).where(CreditCampaign.id == campaign.id))
        for user in users:
            await db.execute(delete(CreditTransaction).where(CreditTransaction.user_id == user.id))
            await db.execute(delete(User).where(User.id == user.id))
        await db.commit()


@pytest.fixture
def promo_wallet(monkeypatch):
    """Configure the promo key and capture what would be submitted to Aleph."""
    monkeypatch.setattr(config, "ALEPH_PROMO_PRIVATE_KEY", PROMO_KEY)
    sent: list[dict] = []
    outcome = {"submit": "processed", "status": "unknown"}

    async def submit(message):
        sent.append(message)
        if isinstance(outcome["submit"], Exception):
            raise outcome["submit"]
        return outcome["submit"]

    async def status(_item_hash):
        return outcome["status"]

    monkeypatch.setattr(aleph_credits, "submit_message", submit)
    monkeypatch.setattr(aleph_credits, "get_message_status", status)
    return sent, outcome


async def _claim_row(campaign_id, user_id) -> CreditCampaignClaim:
    async with AsyncSessionLocal() as db:
        return (
            (
                await db.execute(
                    select(CreditCampaignClaim).where(
                        CreditCampaignClaim.campaign_id == campaign_id, CreditCampaignClaim.user_id == user_id
                    )
                )
            )
            .scalars()
            .one()
        )


# --- public status ---


async def test_public_status_follows_windows(cleanup):
    campaigns, _ = cleanup
    open_ = await _campaign([("Now", -1, 5, 10)])
    upcoming = await _campaign([("Later", 24, 48, 10)])
    closed = await _campaign([("Before", -48, -24, 10)])
    bare = await _campaign([])
    campaigns += [open_, upcoming, closed, bare]

    assert (await svc.get_public_campaign(open_.code)).status == "open"
    up = await svc.get_public_campaign(upcoming.code)
    assert up.status == "upcoming" and up.event is not None and up.event.name == "Later"
    assert (await svc.get_public_campaign(closed.code)).status == "closed"
    nothing = await svc.get_public_campaign(bare.code)
    assert nothing.status == "closed" and nothing.event is None

    with pytest.raises(HTTPException) as exc:
        await svc.get_public_campaign("nope1234")
    assert exc.value.status_code == 404


async def test_public_status_serializes_utc_with_z(cleanup):
    campaigns, _ = cleanup
    campaign = await _campaign([("Now", -1, 5, 10)])
    campaigns.append(campaign)
    body = (await svc.get_public_campaign(campaign.code)).model_dump(mode="json")
    assert body["event"]["starts_at"].endswith("Z")


async def test_inactive_code_is_closed(cleanup):
    campaigns, users = cleanup
    campaign = await _campaign([("Now", -1, 5, 10)])
    campaigns.append(campaign)
    async with AsyncSessionLocal() as db:
        (await db.get(CreditCampaign, campaign.id)).is_active = False
        await db.commit()
    assert (await svc.get_public_campaign(campaign.code)).status == "closed"
    user = await _user("off@example.com")
    users.append(user)
    with pytest.raises(HTTPException) as exc:
        await svc.claim_campaign(campaign.code, user, None)
    assert exc.value.status_code == 410


# --- claiming ---


async def test_claim_grants_libertai_voucher_once(cleanup):
    campaigns, users = cleanup
    campaign = await _campaign([("Summit", -1, 5, 10)])
    user = await _user("builder@example.com")
    campaigns.append(campaign)
    users.append(user)

    claim = await svc.claim_campaign(campaign.code, user, None)
    assert claim.event_name == "Summit"
    assert claim.libertai_amount == 20.0
    assert claim.aleph_status == AlephGrantStatus.none
    assert await CreditService.get_balance(user.id) == pytest.approx(20.0)
    remaining = claim.libertai_expires_at - svc.utcnow()
    assert timedelta(days=59, hours=23) < remaining <= timedelta(days=60)

    with pytest.raises(HTTPException) as exc:
        await svc.claim_campaign(campaign.code, user, None)
    assert exc.value.status_code == 409
    assert await CreditService.get_balance(user.id) == pytest.approx(20.0)
    assert (await svc.get_claim(campaign.code, user)).event_name == "Summit"


async def test_wallet_only_account_cannot_claim(cleanup):
    campaigns, users = cleanup
    campaign = await _campaign([("Summit", -1, 5, 10)])
    user = await _user(None)
    campaigns.append(campaign)
    users.append(user)
    with pytest.raises(HTTPException) as exc:
        await svc.claim_campaign(campaign.code, user, None)
    assert exc.value.status_code == 403


async def test_disposable_email_cannot_claim(cleanup):
    campaigns, users = cleanup
    campaign = await _campaign([("Summit", -1, 5, 10)])
    user = await _user("someone@mailinator.com")
    campaigns.append(campaign)
    users.append(user)
    with pytest.raises(HTTPException) as exc:
        await svc.claim_campaign(campaign.code, user, None)
    assert exc.value.status_code == 403


async def test_no_claims_outside_a_window(cleanup):
    campaigns, users = cleanup
    upcoming = await _campaign([("Later", 24, 48, 10)])
    closed = await _campaign([("Before", -48, -24, 10)])
    user = await _user("early@example.com")
    campaigns += [upcoming, closed]
    users.append(user)
    for campaign in (upcoming, closed):
        with pytest.raises(HTTPException) as exc:
            await svc.claim_campaign(campaign.code, user, None)
        assert exc.value.status_code == 410
    assert await CreditService.get_balance(user.id) == 0


async def test_window_cap(cleanup):
    campaigns, users = cleanup
    campaign = await _campaign([("Tiny", -1, 5, 1)])
    first, second = await _user("first@example.com"), await _user("second@example.com")
    campaigns.append(campaign)
    users += [first, second]

    await svc.claim_campaign(campaign.code, first, None)
    assert (await svc.get_public_campaign(campaign.code)).status == "full"
    with pytest.raises(HTTPException) as exc:
        await svc.claim_campaign(campaign.code, second, None)
    assert exc.value.status_code == 410


async def test_reactivated_banner_counts_per_event_and_still_one_claim_per_person(cleanup):
    campaigns, users = cleanup
    campaign = await _campaign([("Event 1", -72, -48, 10), ("Event 2", -1, 5, 10)])
    returning, newcomer = await _user("again@example.com"), await _user("new@example.com")
    campaigns.append(campaign)
    users += [returning, newcomer]

    # A claim made at the first event (seeded directly, the window is past).
    async with AsyncSessionLocal() as db:
        first_window = (
            (
                await db.execute(
                    select(CreditCampaignWindow).where(
                        CreditCampaignWindow.campaign_id == campaign.id, CreditCampaignWindow.event_name == "Event 1"
                    )
                )
            )
            .scalars()
            .one()
        )
        db.add(
            CreditCampaignClaim(
                campaign_id=campaign.id,
                window_id=first_window.id,
                user_id=returning.id,
                email_canonical="again@example.com",
                libertai_amount=20.0,
                aleph_amount=20.0,
                expires_at=svc.utcnow() + timedelta(days=10),
            )
        )
        await db.commit()

    with pytest.raises(HTTPException) as exc:
        await svc.claim_campaign(campaign.code, returning, None)
    assert exc.value.status_code == 409
    assert (await svc.claim_campaign(campaign.code, newcomer, None)).event_name == "Event 2"


async def test_invalid_aleph_address(cleanup):
    campaigns, users = cleanup
    campaign = await _campaign([("Summit", -1, 5, 10)])
    user = await _user("typo@example.com")
    campaigns.append(campaign)
    users.append(user)
    with pytest.raises(HTTPException) as exc:
        await svc.claim_campaign(campaign.code, user, "0x123")
    assert exc.value.status_code == 400
    assert await CreditService.get_balance(user.id) == 0


# --- the Aleph Cloud half ---


async def test_aleph_grant_is_a_signed_capped_transfer(cleanup, promo_wallet):
    sent, _ = promo_wallet
    campaigns, users = cleanup
    campaign = await _campaign([("Summit", -1, 5, 10)])
    user = await _user("wallet@example.com")
    campaigns.append(campaign)
    users.append(user)

    claim = await svc.claim_campaign(campaign.code, user, EVM)
    assert claim.aleph_status == AlephGrantStatus.sent
    assert claim.aleph_address == EVM_CHECKSUM

    [message] = sent
    promo = Account.from_key(PROMO_KEY).address
    assert message["sender"] == promo and message["channel"] == "ALEPH_CREDIT" and message["type"] == "POST"
    assert message["item_hash"] == sha256(message["item_content"].encode()).hexdigest() == claim.aleph_item_hash
    signed = encode_defunct(text=f"ETH\n{promo}\nPOST\n{message['item_hash']}")
    assert Account.recover_message(signed, signature=message["signature"]) == promo

    content = json.loads(message["item_content"])
    assert content["type"] == "aleph_credit_transfer" and content["address"] == promo
    [entry] = content["content"]["transfer"]["credits"]
    assert entry["address"] == EVM_CHECKSUM
    assert entry["amount"] == 20_000_000
    row = await _claim_row(campaign.id, user.id)
    assert abs(entry["expiration"] / 1000 - row.expires_at.timestamp()) < 2


async def test_aleph_retry_resubmits_the_same_message(cleanup, promo_wallet):
    sent, outcome = promo_wallet
    campaigns, users = cleanup
    campaign = await _campaign([("Summit", -1, 5, 10)])
    user = await _user("flaky@example.com")
    campaigns.append(campaign)
    users.append(user)

    outcome["submit"] = RuntimeError("node unreachable")
    claim = await svc.claim_campaign(campaign.code, user, SOLANA)
    assert claim.aleph_status == AlephGrantStatus.pending
    assert claim.aleph_address == SOLANA

    outcome["submit"] = "processed"
    outcome["status"] = "unknown"  # the node never saw the first attempt
    await svc.process_pending_aleph_grants()
    assert (await svc.get_claim(campaign.code, user)).aleph_status == AlephGrantStatus.sent
    assert len(sent) == 2 and sent[0]["item_hash"] == sent[1]["item_hash"]


async def test_aleph_already_processed_is_not_resubmitted(cleanup, promo_wallet):
    sent, outcome = promo_wallet
    campaigns, users = cleanup
    campaign = await _campaign([("Summit", -1, 5, 10)])
    user = await _user("lost-response@example.com")
    campaigns.append(campaign)
    users.append(user)

    outcome["submit"] = RuntimeError("response lost")
    await svc.claim_campaign(campaign.code, user, EVM)
    outcome["status"] = "processed"
    await svc.process_pending_aleph_grants()
    assert (await svc.get_claim(campaign.code, user)).aleph_status == AlephGrantStatus.sent
    assert len(sent) == 1


async def test_aleph_rejection_fails_without_touching_libertai(cleanup, promo_wallet):
    _, outcome = promo_wallet
    campaigns, users = cleanup
    campaign = await _campaign([("Summit", -1, 5, 10)])
    user = await _user("empty-pool@example.com")
    campaigns.append(campaign)
    users.append(user)

    outcome["submit"] = "rejected"
    claim = await svc.claim_campaign(campaign.code, user, EVM)
    assert claim.aleph_status == AlephGrantStatus.failed
    assert await CreditService.get_balance(user.id) == pytest.approx(20.0)


async def test_wallet_added_later_and_only_once(cleanup, promo_wallet):
    sent, _ = promo_wallet
    campaigns, users = cleanup
    campaign = await _campaign([("Summit", -1, 5, 10)])
    user = await _user("later@example.com")
    campaigns.append(campaign)
    users.append(user)

    await svc.claim_campaign(campaign.code, user, None)
    claim = await svc.attach_aleph_address(campaign.code, user, EVM)
    assert claim.aleph_status == AlephGrantStatus.sent and len(sent) == 1
    with pytest.raises(HTTPException) as exc:
        await svc.attach_aleph_address(campaign.code, user, SOLANA)
    assert exc.value.status_code == 409


async def test_one_wallet_per_code(cleanup, promo_wallet):
    campaigns, users = cleanup
    campaign = await _campaign([("Summit", -1, 5, 10)])
    first, second = await _user("a@example.com"), await _user("b@example.com")
    campaigns.append(campaign)
    users += [first, second]

    await svc.claim_campaign(campaign.code, first, EVM)
    with pytest.raises(HTTPException) as exc:
        await svc.claim_campaign(campaign.code, second, EVM_CHECKSUM)
    assert exc.value.status_code == 409
    # The failed claim rolled back whole: no LibertAI credits either.
    assert await CreditService.get_balance(second.id) == 0


async def test_no_promo_key_queues_until_configured(cleanup, promo_wallet, monkeypatch):
    sent, _ = promo_wallet
    campaigns, users = cleanup
    campaign = await _campaign([("Summit", -1, 5, 10)])
    user = await _user("queued@example.com")
    campaigns.append(campaign)
    users.append(user)

    monkeypatch.setattr(config, "ALEPH_PROMO_PRIVATE_KEY", "")
    claim = await svc.claim_campaign(campaign.code, user, EVM)
    assert claim.aleph_status == AlephGrantStatus.pending and not sent

    monkeypatch.setattr(config, "ALEPH_PROMO_PRIVATE_KEY", PROMO_KEY)
    await svc.process_pending_aleph_grants()
    assert (await svc.get_claim(campaign.code, user)).aleph_status == AlephGrantStatus.sent
    assert len(sent) == 1


async def test_libertai_only_campaign_needs_no_transfer(cleanup, promo_wallet):
    sent, _ = promo_wallet
    campaigns, users = cleanup
    campaign = await _campaign([("Summit", -1, 5, 10)], aleph_amount=0)
    user = await _user("ltai-only@example.com")
    campaigns.append(campaign)
    users.append(user)
    await svc.claim_campaign(campaign.code, user, EVM)
    assert not sent


# --- HTTP surface ---


async def test_routes(cleanup, async_client, promo_wallet):
    campaigns, users = cleanup
    campaign = await _campaign([("Summit", -1, 5, 10)])
    user = await _user("http@example.com")
    campaigns.append(campaign)
    users.append(user)
    auth = {"Authorization": f"Bearer {create_access_token(user.id)}"}

    public = await async_client.get(f"/credits/campaigns/{campaign.code}")
    assert public.status_code == 200 and public.json()["status"] == "open"
    assert (await async_client.post(f"/credits/campaigns/{campaign.code}/claim", json={})).status_code == 401
    assert (await async_client.get(f"/credits/campaigns/{campaign.code}/claim", headers=auth)).status_code == 404

    claimed = await async_client.post(
        f"/credits/campaigns/{campaign.code}/claim", json={"aleph_address": None}, headers=auth
    )
    assert claimed.status_code == 200, claimed.text
    body = claimed.json()
    assert body["aleph_status"] == "none" and body["libertai_expires_at"].endswith("Z")

    attached = await async_client.post(
        f"/credits/campaigns/{campaign.code}/claim/aleph", json={"aleph_address": EVM}, headers=auth
    )
    assert attached.status_code == 200 and attached.json()["aleph_status"] == "sent"
