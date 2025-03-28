import fastapi
from fastapi import APIRouter, HTTPException, status
from libertai_utils.chains.ethereum import format_eth_address
from libertai_utils.chains.index import is_signature_valid
from libertai_utils.interfaces.subscription import SubscriptionChain

from src.config import config
from src.interfaces.auth import AuthLoginRequest, AuthLoginResponse, AuthMessageRequest, AuthMessageResponse
from src.services.auth import create_access_token
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

router = APIRouter(prefix="/auth", tags=["Authentication"])


# TODO: put this in a better place
def auth_message(address: str) -> str:
    return f"Sign to authenticate with LibertAI with your wallet: {format_eth_address(address)}"


@router.post("/message")
async def get_auth_message(request: AuthMessageRequest) -> AuthMessageResponse:
    """Get the static message for wallet signature authentication."""

    return AuthMessageResponse(message=auth_message(request.address))


@router.post("/login")
async def login_with_wallet(request: AuthLoginRequest, response: fastapi.Response) -> AuthLoginResponse:
    """Authenticate with a wallet signature."""
    if not is_signature_valid(
        SubscriptionChain.base, auth_message(request.address), request.signature, request.address
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid signature",
        )

    # Create access token
    access_token = create_access_token(address=request.address)

    # Set the token as an HTTP-only cookie
    response.set_cookie(
        key="libertai_auth",
        value=access_token,
        httponly=True,
        max_age=config.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        samesite="none",
        secure=False if config.IS_DEVELOPMENT else True,
    )

    logger.debug(f"Generated access token for address {request.address}")

    return AuthLoginResponse(access_token=access_token, address=request.address)
