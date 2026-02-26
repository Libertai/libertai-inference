from fastapi import APIRouter, Depends

from src.services.auth import verify_admin_token
from src.services.x402 import x402_service

router = APIRouter(prefix="/x402", tags=["x402"])


@router.get("/prices", dependencies=[Depends(verify_admin_token)])
async def get_x402_prices() -> dict[str, dict]:
    return await x402_service.get_current_prices()
