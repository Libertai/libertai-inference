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

class AgentUsage(BaseModel):
    name: str
    created_at: str

class GlobalAgentStats(BaseModel):
    """Agent usage statistics for a date range."""
    total_agents_created: int
    total_vouchers: int
    total_subscriptions: int
    agents: list[AgentUsage]

class ModelApiUsage(BaseModel):
    model_name: str
    used_at: str

class GlobalApiStats(BaseModel):
    """Api usage statistics for a date range."""
    total_calls: int
    api_usage: list[ModelApiUsage]


class Call(BaseModel):
    date: str
    nb_input_tokens: int
    nb_output_tokens: int

class GlobalTokensStats(BaseModel):
    total_input_tokens: int
    total_output_tokens: int
    calls: list[Call]
