from pydantic import BaseModel


class AnonUsageResponse(BaseModel):
    """Anonymous (logged-out) per-IP chat usage, for the frontend's free-message meter."""

    used: int
    limit: int
    allowed: bool
    resets_at: str | None  # ISO timestamp, or null when no window is active yet
