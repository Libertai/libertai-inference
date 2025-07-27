import fastapi
from fastapi import APIRouter, HTTPException, status, Cookie
from libertai_utils.chains.ethereum import format_eth_address
from libertai_utils.chains.index import is_signature_valid

from src.config import config
from src.interfaces.auth import (
    AuthLoginRequest,
    AuthLoginResponse,
    AuthMessageRequest,
    AuthMessageResponse,
    AuthStatusResponse,
)
from src.services.auth import create_access_token, verify_token
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
    if not is_signature_valid(request.chain, auth_message(request.address), request.signature, request.address):
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
        secure=True,
    )

    return AuthLoginResponse(access_token=access_token, address=request.address)


@router.get("/status")
async def check_auth_status(libertai_auth: str = Cookie(default=None)) -> AuthStatusResponse:
    """Check if the user is authenticated with a valid token."""
    if not libertai_auth:
        return AuthStatusResponse(authenticated=False)

    try:
        token_data = verify_token(libertai_auth)
        return AuthStatusResponse(authenticated=True, address=token_data.address)
    except HTTPException:
        # If token verification fails, return not authenticated
        return AuthStatusResponse(authenticated=False)
