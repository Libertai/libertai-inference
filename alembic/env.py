import os
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, make_url
from sqlalchemy import pool

# Import all models that should be included in migrations
from src.models.api_key import ApiKey  # noqa
from src.models.auth_code import AuthCode  # noqa
from src.models.base import Base  # Import the Base from your models
from src.models.chat_request import ChatRequest  # noqa
from src.models.credit_transaction import CreditTransaction  # noqa
from src.models.inference_call import InferenceCall  # noqa
from src.models.liberclaw_user import LiberclawUser  # noqa
from src.models.liberclaw_credit_grant import LiberclawCreditGrant  # noqa
from src.models.magic_link import MagicLink  # noqa
from src.models.entitlement_window import EntitlementWindow  # noqa
from src.models.oauth_connection import OAuthConnection  # noqa
from src.models.plan_subscription import PlanSubscription  # noqa
from src.models.plan_subscription_event import PlanSubscriptionEvent  # noqa
from src.models.session import Session  # noqa
from src.models.user import User  # noqa
from src.models.wallet_challenge import WalletChallenge  # noqa
from src.models.wallet_connection import WalletConnection  # noqa

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

load_dotenv()

DATABASE_URL = os.path.expandvars(os.getenv("DATABASE_URL", ""))
if DATABASE_URL:
    DATABASE_URL = make_url(DATABASE_URL).set(drivername="postgresql+psycopg").render_as_string(hide_password=False)
config.set_main_option("sqlalchemy.url", DATABASE_URL)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
