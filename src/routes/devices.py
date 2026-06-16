from fastapi import APIRouter, Depends, Response, status

from src.interfaces.device_tokens import DeviceTokenRegisterRequest, DeviceTokenResponse
from src.models.user import User
from src.services.auth import get_current_user
from src.services.device_tokens import DeviceTokenService

router = APIRouter(prefix="/devices", tags=["Devices"])


@router.post("")
async def register_device_token(
    payload: DeviceTokenRegisterRequest, user: User = Depends(get_current_user)
) -> DeviceTokenResponse:
    return await DeviceTokenService.register_device_token(user.id, payload)


@router.delete("/{token:path}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_device_token(token: str, user: User = Depends(get_current_user)) -> Response:
    await DeviceTokenService.disable_device_token(user.id, token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
