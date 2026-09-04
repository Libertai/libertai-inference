"""Product-scoped owner handle for a plan subscription.

A subscription belongs to either a libertai ``User`` or a liberclaw account, never both.
``Owner`` carries whichever id applies plus the product, so callers (locking, queries,
webhook resolution) don't need to branch on product themselves.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import and_
from sqlalchemy.sql.elements import ColumnElement

from src.models.plan_subscription import PlanSubscription
from src.models.user import User
from src.subscription_tiers import PRODUCT_LIBERCLAW, PRODUCT_LIBERTAI


@dataclass(frozen=True)
class Owner:
    product: str
    user_id: uuid.UUID | None
    liberclaw_account_id: uuid.UUID | None
    # Resolved lazily for liberclaw (bridge lookup); always set for libertai.
    email: str | None

    @property
    def lock_id(self) -> uuid.UUID:
        """Id used for advisory locking / row identity: liberclaw_account_id for liberclaw
        rows, user_id otherwise. product='liberclaw' locks on liberclaw_account_id
        unconditionally, forever."""
        owning_id = self.liberclaw_account_id if self.product == PRODUCT_LIBERCLAW else self.user_id
        assert owning_id is not None, f"Owner({self.product}) missing its owning id"
        return owning_id

    def sub_filter(self) -> ColumnElement[bool]:
        owner_col = (
            PlanSubscription.liberclaw_account_id if self.product == PRODUCT_LIBERCLAW else PlanSubscription.user_id
        )
        return and_(PlanSubscription.product == self.product, owner_col == self.lock_id)

    @classmethod
    def from_subscription(cls, sub: PlanSubscription) -> "Owner":
        return cls(
            product=sub.product,
            user_id=sub.user_id,
            liberclaw_account_id=sub.liberclaw_account_id,
            email=None,
        )

    @classmethod
    def for_user(cls, user: User) -> "Owner":
        return cls(product=PRODUCT_LIBERTAI, user_id=user.id, liberclaw_account_id=None, email=user.email)

    @classmethod
    def for_liberclaw(cls, account_id: uuid.UUID, email: str | None = None) -> "Owner":
        return cls(product=PRODUCT_LIBERCLAW, user_id=None, liberclaw_account_id=account_id, email=email)
