from typing import Dict, List


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
    monthly_usage: Dict[str, float]
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
    daily_usage: Dict[str, DailyTokens]
    usage_by_model: List[UsageByEntity]
    usage_by_api_key: List[UsageByEntity]
