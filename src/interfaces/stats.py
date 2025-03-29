from typing import Dict

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