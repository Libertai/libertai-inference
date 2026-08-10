from sqlalchemy import select

from src.models.blocked_email_domain import BlockedEmailDomain


async def test_blocked_domain_row_round_trips(db):
    db.add(BlockedEmailDomain(domain="blocked-fixture.test", reason="unit test"))
    await db.flush()

    found = (
        (await db.execute(select(BlockedEmailDomain).where(BlockedEmailDomain.domain == "blocked-fixture.test")))
        .scalars()
        .first()
    )

    assert found is not None
    assert found.reason == "unit test"
    assert found.created_at is not None
