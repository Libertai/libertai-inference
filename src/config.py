import logging
import os

from dotenv import load_dotenv
from eth_typing import ChecksumAddress
from solders.pubkey import Pubkey
from web3 import Web3


class _Config:
    LTAI_PAYMENT_PROCESSOR_CONTRACT_BASE: ChecksumAddress
    LTAI_PAYMENT_PROCESSOR_CONTRACT_SOLANA: Pubkey
    BASE_RPC_URL: str
    SOLANA_RPC_URL: str

    DATABASE_URL: str

    THIRDWEB_WEBHOOK_SECRET: str

    LOG_LEVEL: int
    LOG_FILE: str | None

    JWT_SECRET: str
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int
    # Allow localhost frontends as CORS + magic-link redirect targets (local dev only)
    ALLOW_LOCALHOST_FRONTENDS: bool

    ADMIN_SECRET: str
    LIBERCLAW_SECRET: str
    # Accepted alongside LIBERCLAW_SECRET during rotation, so redeploying with a new secret
    # doesn't 401 in-flight callers still sending the old one.
    LIBERCLAW_SECRET_PREVIOUS: str | None

    LIBERTAI_CHAT_API_KEY: str
    LIBERTAI_CHAT_API_BASE_URL: str

    THIRDWEB_SECRET_KEY: str
    THIRDWEB_VAULT_ACCESS_TOKEN: str

    # OAuth (apps registered under the LibertAI org / Google Cloud project)
    GOOGLE_CLIENT_ID: str
    GOOGLE_CLIENT_SECRET: str
    GITHUB_CLIENT_ID: str
    GITHUB_CLIENT_SECRET: str

    # Magic-link / email (Resend; falls back to console logging when RESEND_API_KEY is unset)
    MAGIC_LINK_SECRET: str
    RESEND_API_KEY: str
    EMAIL_FROM: str
    # Lifecycle emails invite a reply, so they come from a monitored mailbox.
    EMAIL_FROM_LIFECYCLE: str

    # Token encryption (Fernet); _PREVIOUS enables key rotation
    ENCRYPTION_KEY: str
    ENCRYPTION_KEY_PREVIOUS: str | None

    # URLs + token lifetimes
    FRONTEND_URL: str
    # Frontends allowed to receive a user (CORS + magic-link redirect target).
    ALLOWED_FRONTEND_URLS: list[str]
    API_URL: str
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int

    # Warm API-key pool: keys pre-created and pre-propagated to instances so a freshly
    # "created" key is recognized immediately (no ~30s propagation wait).
    POOL_SIZE: int
    POOL_WARM_THRESHOLD_SECONDS: int
    POOL_RECONCILE_INTERVAL_SECONDS: int

    # GeoIP database (MaxMind GeoLite2 Country), mounted in prod; missing in dev -> USD fallback
    GEOIP_DB_PATH: str

    # Provider-agnostic fiat payments (Revolut first)
    REVOLUT_SECRET_KEY: str
    REVOLUT_WEBHOOK_SECRET: str
    REVOLUT_API_URL: str
    REVOLUT_API_VERSION: str
    # Optional JSON override of the per-tier Revolut plan ids (sandbox envs have their
    # own plan ids): {"go": {"USD": {"plan_id": ..., "variation_id": ...}, ...}, ...}
    REVOLUT_PLAN_IDS: str

    # Per-tier rolling-window credit allowances, configured per environment.
    # JSON: {"free": {"window_5h": ..., "weekly": ...}, ...}. Required for every tier;
    # an incomplete or missing value stops the app from starting.
    SUBSCRIPTION_TIER_LIMITS: str
    # Gates PaymentManager ownership of LiberClaw subscriptions (webhooks, snapshot push/retry,
    # reconcile/expire/sweep LCLW scope). Off: LCLW-resolved webhook events 200-skip, never 5xx.
    LIBERCLAW_BILLING_ENABLED: bool

    def __init__(self):
        load_dotenv()
        self.LTAI_PAYMENT_PROCESSOR_CONTRACT_BASE = Web3.to_checksum_address(
            os.getenv("LTAI_PAYMENT_PROCESSOR_CONTRACT_BASE")
        )
        self.LTAI_PAYMENT_PROCESSOR_CONTRACT_SOLANA = Pubkey.from_string(
            os.getenv("LTAI_PAYMENT_PROCESSOR_CONTRACT_SOLANA")
        )
        self.BASE_RPC_URL = os.getenv("BASE_RPC_URL")
        self.SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL")

        self.DATABASE_URL = os.path.expandvars(os.getenv("DATABASE_URL", ""))

        self.THIRDWEB_WEBHOOK_SECRET = os.getenv("THIRDWEB_WEBHOOK_SECRET")

        # Configure logging
        log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
        self.LOG_LEVEL = getattr(logging, log_level_str, logging.INFO)
        self.LOG_FILE = os.getenv("LOG_FILE", None)

        self.JWT_SECRET = os.getenv("JWT_SECRET")
        self.JWT_ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES"))
        self.ALLOW_LOCALHOST_FRONTENDS = os.getenv("ALLOW_LOCALHOST_FRONTENDS", "False").lower() == "true"

        self.ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")
        self.LIBERCLAW_SECRET: str = os.getenv("LIBERCLAW_SECRET", "")
        self.LIBERCLAW_SECRET_PREVIOUS = os.getenv("LIBERCLAW_SECRET_PREVIOUS") or None

        self.LIBERTAI_CHAT_API_KEY = os.getenv("LIBERTAI_CHAT_API_KEY")
        self.LIBERTAI_CHAT_API_BASE_URL = os.getenv("LIBERTAI_CHAT_API_BASE_URL")
        self.THIRDWEB_SECRET_KEY = os.getenv("THIRDWEB_SECRET_KEY", "")
        self.THIRDWEB_VAULT_ACCESS_TOKEN = os.getenv("THIRDWEB_VAULT_ACCESS_TOKEN", "")

        # OAuth
        self.GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
        self.GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
        self.GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "")
        self.GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")

        # Magic-link / email
        self.MAGIC_LINK_SECRET = os.getenv("MAGIC_LINK_SECRET", "")
        self.RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
        self.EMAIL_FROM = os.getenv("EMAIL_FROM", "LibertAI <noreply@libertai.io>")
        self.EMAIL_FROM_LIFECYCLE = os.getenv("EMAIL_FROM_LIFECYCLE", "LibertAI <support@libertai.io>")

        # Token encryption (Fernet)
        self.ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")
        self.ENCRYPTION_KEY_PREVIOUS = os.getenv("ENCRYPTION_KEY_PREVIOUS", None)

        # URLs + token lifetimes
        self.FRONTEND_URL = os.getenv("FRONTEND_URL", "")
        # Origins we're willing to send a logged-in user to. Used both for CORS and to
        # validate the magic-link redirect target so the sign-in email points back to the
        # app the request came from (chat vs console), never an attacker-supplied URL.
        self.ALLOWED_FRONTEND_URLS = [
            "https://console.libertai.io",
            "https://analytics.libertai.io",
            "https://beta.chat.libertai.io",
            "https://chat.libertai.io",
        ] + (["http://localhost:5173", "http://localhost:3000"] if self.ALLOW_LOCALHOST_FRONTENDS else [])
        self.API_URL = os.getenv("API_URL", "")
        self.JWT_REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("JWT_REFRESH_TOKEN_EXPIRE_DAYS", "90"))

        # Warm API-key pool
        self.POOL_SIZE = int(os.getenv("POOL_SIZE", "5"))
        self.POOL_WARM_THRESHOLD_SECONDS = int(os.getenv("POOL_WARM_THRESHOLD_SECONDS", "60"))
        self.POOL_RECONCILE_INTERVAL_SECONDS = int(os.getenv("POOL_RECONCILE_INTERVAL_SECONDS", "300"))

        # GeoIP database
        self.GEOIP_DB_PATH = os.getenv("GEOIP_DB_PATH", "/data/GeoLite2-Country.mmdb")

        # Payments (Revolut)
        self.REVOLUT_SECRET_KEY = os.getenv("REVOLUT_SECRET_KEY", "")
        self.REVOLUT_WEBHOOK_SECRET = os.getenv("REVOLUT_WEBHOOK_SECRET", "")
        self.REVOLUT_API_URL = os.getenv("REVOLUT_API_URL", "https://merchant.revolut.com")
        self.REVOLUT_API_VERSION = os.getenv("REVOLUT_API_VERSION", "2026-04-20")
        self.REVOLUT_PLAN_IDS = os.getenv("REVOLUT_PLAN_IDS", "")

        # Subscription tiers
        self.SUBSCRIPTION_TIER_LIMITS = os.getenv("SUBSCRIPTION_TIER_LIMITS", "")
        self.LIBERCLAW_BILLING_ENABLED = os.getenv("LIBERCLAW_BILLING_ENABLED", "False").lower() == "true"


config = _Config()
