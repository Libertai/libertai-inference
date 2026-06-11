"""Revolut Merchant API provider — fiat top-ups and subscriptions.

Ported from ``liberclaw`` and extended with one-off **orders** (top-ups) on top
of the existing subscription flow. Webhook payloads are normalized into the
shared :class:`PaymentEvent` so the manager stays provider-agnostic.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time

import httpx

from src.services.payments.base import (
    CheckoutResult,
    PaymentCapability,
    PaymentEvent,
    PaymentEventType,
    PaymentProvider,
    PaymentProviderKind,
    ProviderDescriptor,
    SubscriptionInfo,
)
from src.subscription_tiers import get_provider_plan

logger = logging.getLogger(__name__)

PROVIDER_ID = "revolut"

# Prefix used in ``merchant_order_ext_ref`` so the manager can recognize a
# one-off top-up order (vs. a subscription setup order) and resolve its user.
TOPUP_EXT_REF_PREFIX = "topup:"

# Map Revolut's event vocabulary onto the normalized types.
_EVENT_MAP: dict[str, PaymentEventType] = {
    "ORDER_COMPLETED": PaymentEventType.order_completed,
    "ORDER_PAYMENT_FAILED": PaymentEventType.order_failed,
    "ORDER_PAYMENT_DECLINED": PaymentEventType.order_failed,
    "ORDER_FAILED": PaymentEventType.order_failed,
    "SUBSCRIPTION_INITIATED": PaymentEventType.subscription_initiated,
    "SUBSCRIPTION_CANCELLED": PaymentEventType.subscription_cancelled,
    "SUBSCRIPTION_OVERDUE": PaymentEventType.subscription_overdue,
    "SUBSCRIPTION_FINISHED": PaymentEventType.subscription_finished,
}

# Max age for webhook timestamps (5 minutes), in milliseconds.
WEBHOOK_TIMESTAMP_TOLERANCE_MS = 5 * 60 * 1000


class RevolutProvider(PaymentProvider):
    def __init__(
        self,
        secret_key: str,
        webhook_secret: str,
        api_url: str,
        api_version: str,
    ):
        self.secret_key = secret_key
        self.webhook_secret = webhook_secret
        self.api_url = api_url.rstrip("/")
        self.api_version = api_version
        self._client: httpx.AsyncClient | None = None

    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id=PROVIDER_ID,
            kind=PaymentProviderKind.fiat,
            label="Card / bank (Revolut)",
            capabilities=[PaymentCapability.topup, PaymentCapability.subscription],
            currencies=["USD", "EUR"],
            enabled=bool(self.secret_key and self.webhook_secret),
        )

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.api_url,
                headers={
                    "Authorization": f"Bearer {self.secret_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Revolut-Api-Version": self.api_version,
                },
                timeout=30.0,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ---- top-ups (one-off orders) ----
    async def create_topup(
        self,
        *,
        amount: float,
        currency: str,
        redirect_url: str,
        user_email: str | None = None,
        metadata: dict | None = None,
    ) -> CheckoutResult:
        ext_ref = (metadata or {}).get("ext_ref")
        body: dict = {
            "amount": int(round(amount * 100)),  # minor units
            "currency": currency,
            "redirect_url": redirect_url,
        }
        if ext_ref:
            body["merchant_order_ext_ref"] = ext_ref
        if user_email:
            body["customer"] = {"email": user_email}

        resp = await self.client.post("/api/orders", json=body)
        resp.raise_for_status()
        data = resp.json()
        checkout_url = data.get("checkout_url")
        if not checkout_url:
            # Older API shape: fetch the order to obtain its hosted checkout URL.
            order = await self.get_order(data["id"])
            checkout_url = order["checkout_url"]
        return CheckoutResult(checkout_url=checkout_url, order_id=data["id"])

    # ---- subscriptions ----
    async def _create_customer(self, email: str) -> str:
        resp = await self.client.post("/api/1.0/customers", json={"email": email})
        resp.raise_for_status()
        return resp.json()["id"]

    async def _post_subscription(self, variation_id: str, customer_id: str, redirect_url: str) -> dict:
        resp = await self.client.post(
            "/api/subscriptions",
            json={
                "plan_variation_id": variation_id,
                "customer_id": customer_id,
                "setup_order_redirect_url": redirect_url,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def create_subscription(
        self,
        *,
        user_email: str,
        tier: str,
        currency: str,
        redirect_url: str,
        provider_customer_id: str | None = None,
    ) -> CheckoutResult:
        customer_id = provider_customer_id or await self._create_customer(user_email)

        plan = get_provider_plan(tier, PROVIDER_ID, currency)
        try:
            sub_data = await self._post_subscription(plan["variation_id"], customer_id, redirect_url)
        except httpx.HTTPStatusError:
            if provider_customer_id is None:
                raise
            # A REUSED customer id can be stale: deleted on Revolut's side, or minted in a
            # different environment (sandbox vs production) before an env switch. Retry once
            # with a freshly created customer instead of failing the checkout.
            logger.warning(
                f"Subscription create failed with reused customer {provider_customer_id}; retrying with a fresh customer"
            )
            customer_id = await self._create_customer(user_email)
            sub_data = await self._post_subscription(plan["variation_id"], customer_id, redirect_url)

        setup_order_id = sub_data["setup_order_id"]
        order_resp = await self.client.get(f"/api/orders/{setup_order_id}")
        order_resp.raise_for_status()
        checkout_url = order_resp.json()["checkout_url"]

        return CheckoutResult(
            checkout_url=checkout_url,
            provider_subscription_id=sub_data["id"],
            provider_customer_id=customer_id,
            order_id=setup_order_id,
        )

    async def cancel_subscription(self, provider_subscription_id: str) -> None:
        resp = await self.client.post(f"/api/subscriptions/{provider_subscription_id}/cancel")
        resp.raise_for_status()

    async def get_subscription(self, provider_subscription_id: str) -> SubscriptionInfo:
        resp = await self.client.get(f"/api/subscriptions/{provider_subscription_id}")
        resp.raise_for_status()
        data = resp.json()

        cycle_start = None
        cycle_end = None
        current_cycle_id = data.get("current_cycle_id")
        if current_cycle_id:
            try:
                cycle = await self.get_cycle(provider_subscription_id, current_cycle_id)
                cycle_start = cycle.get("start_date")
                cycle_end = cycle.get("end_date")
            except Exception:
                logger.warning(f"Failed to fetch cycle {current_cycle_id}", exc_info=True)

        return SubscriptionInfo(
            provider_subscription_id=data["id"],
            state=data["state"],
            current_cycle_start=cycle_start,
            current_cycle_end=cycle_end,
        )

    async def get_cycle(self, provider_subscription_id: str, cycle_id: str) -> dict:
        resp = await self.client.get(f"/api/subscriptions/{provider_subscription_id}/cycles/{cycle_id}")
        resp.raise_for_status()
        return resp.json()

    async def get_order(self, order_id: str) -> dict:
        resp = await self.client.get(f"/api/orders/{order_id}")
        resp.raise_for_status()
        return resp.json()

    # ---- inbound webhook ----
    def parse_webhook(self, headers: dict, body: bytes) -> PaymentEvent:
        timestamp = headers.get("revolut-request-timestamp", "")
        signature_header = headers.get("revolut-signature", "")

        if not timestamp or not signature_header:
            raise ValueError("Missing webhook signature headers")

        try:
            ts_ms = int(timestamp)
        except ValueError:
            raise ValueError("Invalid timestamp format")

        now_ms = int(time.time() * 1000)
        if abs(now_ms - ts_ms) > WEBHOOK_TIMESTAMP_TOLERANCE_MS:
            raise ValueError("Webhook timestamp too old or too far in the future")

        payload_to_sign = f"v1.{timestamp}.{body.decode('utf-8')}"
        expected_sig = "v1=" + hmac.new(
            self.webhook_secret.encode("utf-8"),
            msg=payload_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()

        provided_sigs = [s.strip() for s in signature_header.split(",")]
        if not any(hmac.compare_digest(expected_sig, sig) for sig in provided_sigs):
            raise ValueError("Invalid webhook signature")

        data = json.loads(body)
        raw_event = data.get("event", "")
        event_type = _EVENT_MAP.get(raw_event, PaymentEventType.ignored)

        order_id = data.get("order_id")
        ext_ref = data.get("merchant_order_ext_ref")

        return PaymentEvent(
            provider=PROVIDER_ID,
            type=event_type,
            provider_event_id=f"{raw_event}:{order_id or ''}",
            provider_subscription_id=None,  # resolved by the manager via order lookup
            order_id=order_id,
            metadata={
                "raw_event": raw_event,
                "order_id": order_id,
                "merchant_order_ext_ref": ext_ref,
            },
        )
