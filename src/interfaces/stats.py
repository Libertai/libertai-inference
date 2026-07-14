from enum import Enum

from pydantic import BaseModel


class TokenStats(BaseModel):
    """Stats about token usage for the current month."""

    inference_calls: int
    total_tokens: int
    input_tokens: int
    output_tokens: int
    credits_used: float


class DashboardStats(BaseModel):
    """Dashboard statistics for a user."""

    address: str
    monthly_usage: dict[str, float]
    current_month: TokenStats


class UsageByEntity(BaseModel):
    """Usage statistics grouped by model or API key."""

    name: str
    calls: int
    total_tokens: int
    cost: float


class DailyTokens(BaseModel):
    """Input and output tokens for a single day."""

    input_tokens: int
    output_tokens: int


class UsageStats(BaseModel):
    """Detailed usage statistics for a date range."""

    inference_calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost: float
    daily_usage: dict[str, DailyTokens]
    usage_by_model: list[UsageByEntity]
    usage_by_api_key: list[UsageByEntity]


class CreditsUsage(BaseModel):
    credits_used: float
    used_at: str
    model_name: str


class GlobalCreditsStats(BaseModel):
    """Credit usage statistics for a date range."""

    total_credits_used: float
    credits_usage: list[CreditsUsage]


class ModelApiUsage(BaseModel):
    model_name: str
    used_at: str
    call_count: int


class GlobalApiStats(BaseModel):
    """Api usage statistics for a date range."""

    total_calls: int
    api_usage: list[ModelApiUsage]


class Call(BaseModel):
    date: str
    nb_input_tokens: int
    nb_output_tokens: int
    nb_cached_tokens: int
    model_name: str


class GlobalTokensStats(BaseModel):
    total_input_tokens: int
    total_output_tokens: int
    total_cached_tokens: int
    calls: list[Call]


class ChatCallUsage(BaseModel):
    model_name: str
    used_at: str
    call_count: int


class GlobalChatCallsStats(BaseModel):
    """Chat API calls statistics for a date range."""

    total_calls: int
    chat_usage: list[ChatCallUsage]


class ChatTokenUsage(BaseModel):
    date: str
    nb_input_tokens: int
    nb_output_tokens: int
    nb_cached_tokens: int
    model_name: str


class GlobalChatTokensStats(BaseModel):
    """Chat token usage statistics for a date range."""

    total_input_tokens: int
    total_output_tokens: int
    total_cached_tokens: int
    token_usage: list[ChatTokenUsage]


class GlobalSummaryStats(BaseModel):
    """Global summary statistics across all key types."""

    total_requests: int
    total_input_tokens: int
    total_output_tokens: int


class DailyActiveUsers(BaseModel):
    """Number of distinct users active on a single day."""

    date: str
    active_users: int


class GlobalUsersStats(BaseModel):
    """Distinct-user (DAU) statistics for a date range.

    ``total_unique_users`` counts distinct users over the whole range (NOT the sum of
    the daily counts, since a user active on several days is counted once).
    """

    total_unique_users: int
    daily_active_users: list[DailyActiveUsers]


class UsersWindow(str, Enum):
    """Rolling window for active-user counts: DAU (day), WAU (week), MAU (month)."""

    day = "day"
    week = "week"
    month = "month"

    @property
    def days(self) -> int:
        return {"day": 1, "week": 7, "month": 30}[self.value]


class SegmentMessageUsage(BaseModel):
    """Chat messages on a single day for one subscription segment."""

    date: str
    segment: str  # "anonymous" | "free" | "go" | "plus" | "max"
    message_count: int


class GlobalSegmentMessagesStats(BaseModel):
    """Chat messages per subscription segment over a date range.

    Sourced from ``chat_requests`` (the full chat history, predating metering), so legacy and
    current messages share one continuous series. Segment is the sender's CURRENT tier — anon
    (shared key), free (no paid sub), or the paid tier (go/plus/max). Historical tier at send
    time isn't recorded, so a user's past messages are attributed to their tier today.
    """

    total_messages: int
    messages: list[SegmentMessageUsage]


class SegmentCallUsage(BaseModel):
    """Inference calls on a single day for one subscription segment."""

    date: str
    segment: str  # "free" | "go" | "plus" | "max"
    call_count: int


class GlobalSegmentCallsStats(BaseModel):
    """Inference calls per subscription segment over a date range (api/cli key types).

    Segment is the caller's CURRENT active paid tier (go/plus/max) or "free" — no anonymous
    bucket, since api/cli use per-user keys. Historical tier at call time isn't recorded, so a
    user's past calls are attributed to their tier today, mirroring the messages-by-segment caveat.
    """

    total_calls: int
    calls: list[SegmentCallUsage]


class CreditsConsumptionDay(BaseModel):
    """Credits consumed on a single day, split by what covered them."""

    date: str
    tier_credits: float  # covered by the subscription entitlement window
    prepaid_credits: float  # overflow drawn from the prepaid balance


class TierCreditsDay(BaseModel):
    """Total credits consumed on a single day by users who were on one tier that day."""

    date: str
    tier: str  # "free" | "go" | "plus" | "max"
    credits: float


class GlobalCreditsConsumptionStats(BaseModel):
    """Credit consumption over a date range (api/cli/chat keys), tier-covered vs prepaid.

    ``daily_by_tier`` splits total consumption by the tier the user held THAT day (historical
    attribution via subscription event replay), including a ``free`` bucket for users with no
    paid subscription. It totals ``credits_used``, so prepaid spend is included.
    """

    total_credits: float
    total_tier_credits: float
    total_prepaid_credits: float
    daily: list[CreditsConsumptionDay]
    daily_by_tier: list[TierCreditsDay]


class TierSubscribers(BaseModel):
    tier: str
    active_subscribers: int


class GlobalSubscriptionsStats(BaseModel):
    """Current snapshot of the user base by segment.

    Subscriptions cover all usage (chat/API/CLI), so this is a user-segmentation view, not a
    chat metric: paid subscribers per tier, registered free users (no active paid sub), and
    anonymous users (distinct IPs that have used logged-out chat).
    """

    subscribers_by_tier: list[TierSubscribers]
    total_paid_subscribers: int
    free_users: int
    anonymous_users: int


class TierSubscribersDay(BaseModel):
    """Active paid subscribers in one tier on a single day."""

    date: str
    tier: str  # "go" | "plus" | "max"
    active_subscribers: int


class GlobalSubscribersOverTimeStats(BaseModel):
    """Active paid subscribers per tier for each day in a date range.

    A subscription counts toward a day if it had started (``created_at``) on or before that day and
    had not yet ended — active/overdue subs run to today, cancelled/expired ones end on their last
    update. Tier is the subscription's CURRENT tier (historical tier changes aren't recorded),
    mirroring the messages-by-segment caveat.
    """

    daily: list[TierSubscribersDay]


class TierPrice(BaseModel):
    tier: str
    monthly_price: float


class TierEconomicsDay(BaseModel):
    """One tier's subscriber count and plan-covered credit draw on a single day."""

    date: str
    tier: str  # "go" | "plus" | "max"
    active_subscribers: int
    credits: float  # tier_credits_used: the portion the entitlement window covered


class GlobalTierEconomicsStats(BaseModel):
    """Per-tier subscribers and plan-covered credit draw per day, for value-for-price analysis.

    ``credits`` is ``tier_credits_used`` — the value a subscriber pulled out of their PLAN.
    Prepaid overflow is excluded: the user paid for that separately, so charging it against the
    subscription would double-count it.

    Spans ALL providers (Revolut and the credits rail), unlike the Revolut-only MRR series: this
    measures value-for-price, and a credits-rail subscriber pays the same tier price.

    Ratios and cumulative sums are deliberately NOT computed here — the client derives them, so
    new chart lenses need no API change.
    """

    daily: list[TierEconomicsDay]
    tier_prices: list[TierPrice]


class SubscriptionStatusFilter(str, Enum):
    """Status filter for the latest-subscribers list. ``all`` includes every status;
    omitting the filter defaults to everything EXCEPT ``pending`` (abandoned checkouts)."""

    all = "all"
    active = "active"
    pending = "pending"
    overdue = "overdue"
    cancelled = "cancelled"
    expired = "expired"
    upgrading = "upgrading"


class LatestSubscriber(BaseModel):
    """A single recent plan subscription with a human-friendly label for its user.

    ``user_label`` is ``display_name (contact)`` when the user set a name, else the bare
    ``contact`` (contact resolves email > wallet address > user id).
    """

    user_label: str
    tier: str
    status: str
    provider: str
    is_trial: bool
    cancel_at_period_end: bool
    created_at: str  # ISO date-time
    current_period_end: str | None


class GlobalLatestSubscribersStats(BaseModel):
    """Most recent plan subscriptions across all providers, newest first.

    ``total`` is the count of ALL rows matching the status filter (ignoring ``limit``),
    so callers can show "showing N of total".
    """

    subscribers: list[LatestSubscriber]
    total: int


class SubscriptionActivityType(str, Enum):
    """Human-facing subscription lifecycle events for the activity feed."""

    subscribed = "subscribed"
    upgraded = "upgraded"
    downgraded = "downgraded"
    cancelled = "cancelled"
    churned = "churned"
    payment_failed = "payment_failed"


class SubscriptionActivityEvent(BaseModel):
    """One lifecycle event, mapped from the raw event log to a human-facing type.

    ``tier`` is the resulting tier; for upgrades/downgrades ``from_tier`` is the prior tier
    (render as "from_tier -> tier").
    """

    created_at: str  # ISO date-time
    type: SubscriptionActivityType
    user_label: str
    tier: str
    from_tier: str | None = None
    provider: str


class GlobalSubscriptionActivityStats(BaseModel):
    """Recent subscription lifecycle events across all providers, newest first."""

    events: list[SubscriptionActivityEvent]


class MrrByTier(BaseModel):
    tier: str
    mrr: float


class MrrDay(BaseModel):
    date: str
    mrr: float


class TopupDay(BaseModel):
    """Completed Revolut credit purchases on a single day."""

    date: str
    amount: float


class GlobalSubscriptionsRevenueStats(BaseModel):
    """Revolut (fiat) MRR, nominal and currency-blind; trials excluded. Event-replayed history.

    ``topups_daily`` covers completed Revolut credit purchases, excluding ``upgrade_remainder``
    grants and pending checkouts. Its window is widened back to the first day of ``start_date``'s
    calendar month so the client can draw a correct month-to-date line for a mid-month range.
    """

    current_mrr: float
    mrr_by_tier: list[MrrByTier]
    daily: list[MrrDay]
    topups_daily: list[TopupDay]
    total_topups: float


class ChurnWeek(BaseModel):
    week_start: str  # Monday, YYYY-MM-DD
    new: int
    churned: int
    net: int


class GlobalSubscriptionsChurnStats(BaseModel):
    """Revolut, non-trial. new = first activations (upgrade replacements excluded);
    churned = real terminations (cancelled/expired/finished; upgrades excluded)."""

    weekly: list[ChurnWeek]
    total_new: int
    total_churned: int
