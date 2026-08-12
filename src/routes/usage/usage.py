from fastapi import Depends

from src.interfaces.usage import UsageResponse, UsageWindow
from src.models.base import AsyncSessionLocal
from src.models.user import User
from src.routes.usage import router
from src.services.auth import get_user_from_api_key
from src.services.entitlement import effective_prepaid, get_allowance_state, used_percent


@router.get("", description="Allowance usage of the API key's owner")  # type: ignore
async def get_usage(user: User = Depends(get_user_from_api_key)) -> UsageResponse:
    async with AsyncSessionLocal() as db:
        state = await get_allowance_state(db, user.id)

    return UsageResponse(
        plan=state.tier,
        window_5h=UsageWindow(
            used_percent=used_percent(state.window_5h_used, state.window_5h_limit),
            resets_at=state.window_5h_resets_at,
        ),
        weekly=UsageWindow(
            used_percent=used_percent(state.weekly_used, state.weekly_limit),
            resets_at=state.weekly_resets_at,
        ),
        extra_usage_credits=effective_prepaid(
            state.prepaid_balance, state.monthly_extra_credit_cap, state.extra_credits_used_this_month
        ),
    )
