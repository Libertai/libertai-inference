from fastapi import Depends, HTTPException, status

from src.interfaces.liberclaw import (
    LiberclawApiKeyRequest,
    LiberclawApiKeyResponse,
    LiberclawTierUpdate,
    LiberclawUserResponse,
)
from src.routes.liberclaw import router
from src.services.auth import verify_liberclaw_token
from src.services.liberclaw import LiberclawService
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


@router.post("/api-key", dependencies=[Depends(verify_liberclaw_token)])  # type: ignore
async def get_or_create_api_key(request: LiberclawApiKeyRequest) -> LiberclawApiKeyResponse:
    """Get or create an API key for a Liberclaw user."""
    try:
        return LiberclawService.get_or_create_api_key(user_id=request.user_id, user_type=request.user_type)
    except Exception as e:
        logger.error(f"Error in get_or_create_api_key: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.put("/tier", dependencies=[Depends(verify_liberclaw_token)])  # type: ignore
async def update_tier(request: LiberclawTierUpdate) -> None:
    """Update a Liberclaw user's tier."""
    try:
        LiberclawService.update_tier(user_id=request.user_id, user_type=request.user_type, tier=request.tier)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Error in update_tier: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/user", dependencies=[Depends(verify_liberclaw_token)])  # type: ignore
async def get_user(user_id: str, user_type: str) -> LiberclawUserResponse:
    """Get Liberclaw user info with usage stats."""
    try:
        return LiberclawService.get_user(user_id=user_id, user_type=user_type)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Error in get_user: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
