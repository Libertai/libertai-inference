"""Shared helpers for the /liberclaw channel's invoice read endpoints.

LCLW invoice issuance happens inside ``PaymentManager.handle_event`` (inference owns the
webhooks); this module keeps only the identity-bridge probe the read routes log on.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.liberclaw_user import LiberclawUser
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
