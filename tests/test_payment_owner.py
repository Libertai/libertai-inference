import uuid

import pytest

from src.models.plan_subscription import PlanSubscription
from src.models.user import User
from src.services.payments.owner import Owner
from src.subscription_tiers import PRODUCT_LIBERCLAW, PRODUCT_LIBERTAI


def test_lock_id_libertai_uses_user_id():
    user_id = uuid.uuid4()
    owner = Owner(product=PRODUCT_LIBERTAI, user_id=user_id, liberclaw_account_id=None, email=None)
    assert owner.lock_id == user_id


def test_lock_id_liberclaw_uses_account_id():
    account_id = uuid.uuid4()
    owner = Owner(product=PRODUCT_LIBERCLAW, user_id=None, liberclaw_account_id=account_id, email=None)
    assert owner.lock_id == account_id


def test_lock_id_libertai_asserts_when_user_id_missing():
    owner = Owner(product=PRODUCT_LIBERTAI, user_id=None, liberclaw_account_id=uuid.uuid4(), email=None)
    with pytest.raises(AssertionError):
        _ = owner.lock_id


def test_lock_id_liberclaw_asserts_when_account_id_missing():
    owner = Owner(product=PRODUCT_LIBERCLAW, user_id=uuid.uuid4(), liberclaw_account_id=None, email=None)
    with pytest.raises(AssertionError):
        _ = owner.lock_id


def test_sub_filter_libertai_compiles_on_user_id():
    user_id = uuid.uuid4()
    owner = Owner(product=PRODUCT_LIBERTAI, user_id=user_id, liberclaw_account_id=None, email=None)
    sql = str(owner.sub_filter().compile(compile_kwargs={"literal_binds": True}))
    assert "plan_subscriptions.user_id" in sql
    assert "plan_subscriptions.liberclaw_account_id" not in sql
    assert user_id.hex in sql
    assert "'libertai'" in sql


def test_sub_filter_liberclaw_compiles_on_account_id():
    account_id = uuid.uuid4()
    owner = Owner(product=PRODUCT_LIBERCLAW, user_id=None, liberclaw_account_id=account_id, email=None)
    sql = str(owner.sub_filter().compile(compile_kwargs={"literal_binds": True}))
    assert "plan_subscriptions.liberclaw_account_id" in sql
    assert "plan_subscriptions.user_id" not in sql
    assert account_id.hex in sql
    assert "'liberclaw'" in sql


def test_from_subscription_libertai_row():
    user_id = uuid.uuid4()
    sub = PlanSubscription(user_id=user_id, tier="go", provider="revolut", product=PRODUCT_LIBERTAI)
    owner = Owner.from_subscription(sub)
    assert owner.product == PRODUCT_LIBERTAI
    assert owner.user_id == user_id
    assert owner.liberclaw_account_id is None
    assert owner.email is None
    assert owner.lock_id == user_id


def test_from_subscription_liberclaw_row():
    account_id = uuid.uuid4()
    sub = PlanSubscription(
        user_id=None,
        tier="starter",
        provider="revolut",
        product=PRODUCT_LIBERCLAW,
        liberclaw_account_id=account_id,
    )
    owner = Owner.from_subscription(sub)
    assert owner.product == PRODUCT_LIBERCLAW
    assert owner.liberclaw_account_id == account_id
    assert owner.user_id is None
    assert owner.email is None
    assert owner.lock_id == account_id


def test_for_user():
    user = User(email="a@b.com")
    user.id = uuid.uuid4()
    owner = Owner.for_user(user)
    assert owner.product == PRODUCT_LIBERTAI
    assert owner.user_id == user.id
    assert owner.liberclaw_account_id is None
    assert owner.email == "a@b.com"


def test_for_liberclaw():
    account_id = uuid.uuid4()
    owner = Owner.for_liberclaw(account_id, email="c@d.com")
    assert owner.product == PRODUCT_LIBERCLAW
    assert owner.liberclaw_account_id == account_id
    assert owner.user_id is None
    assert owner.email == "c@d.com"


def test_for_liberclaw_default_email_none():
    owner = Owner.for_liberclaw(uuid.uuid4())
    assert owner.email is None
