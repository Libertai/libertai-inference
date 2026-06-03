import base64
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta

import fastapi
import jwt
from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from libertai_utils.chains.ethereum import format_eth_address
from libertai_utils.chains.index import is_signature_valid
from sqlalchemy import select

from src.config import config
from src.interfaces.auth import (
    AuthLoginRequest,
    AuthLoginResponse,
    AuthMessageRequest,
    AuthMessageResponse,
    AuthStatusResponse,
    CliCodeRequest,
    CliCodeResponse,
    CurrentUserResponse,
    EmailLoginRequest,
    ExchangeRequest,
    RefreshRequest,
    TokenPairResponse,
    VerifyMagicLinkRequest,
    WalletChallengeRequest,
    WalletChallengeResponse,
    WalletVerifyRequest,
)
from src.models.auth_code import AuthCode
from src.models.base import AsyncSessionLocal
from src.models.session import Session
from src.models.user import User
from src.services import magic_link, oauth, wallet_auth
from src.services.auth import create_access_token, get_current_user, verify_token
from src.services.auth_tokens import REFRESH, create_access_token as create_user_access_token, create_refresh_token, decode_token
from src.services.users import get_or_create_user_by_email, get_or_create_user_by_oauth, get_or_create_user_by_wallet, link_wallet
from src.utils.encryption import decrypt, encrypt
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

router = APIRouter(prefix="/auth", tags=["Authentication"])

_AUTH_CODE_TTL_SECONDS = 60


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _pkce_matches(verifier: str, challenge: str) -> bool:
    """PKCE S256: base64url(SHA256(verifier)) without padding must equal the stored challenge."""
    digest = hashlib.sha256(verifier.encode()).digest()
    expected = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return secrets.compare_digest(expected, challenge)


async def _issue_token_pair(db, user: User, device_info: str | None = None) -> TokenPairResponse:
    """Create a Session + access/refresh pair, storing the refresh hash for rotation."""
    session = Session(
        user_id=user.id,
        refresh_token_hash="pending",
        expires_at=datetime.now() + timedelta(days=config.JWT_REFRESH_TOKEN_EXPIRE_DAYS),
        device_info=device_info,
    )
    db.add(session)
    await db.flush()
    refresh = create_refresh_token(user.id, session.id)
    session.refresh_token_hash = _hash(refresh)
    await db.flush()
    return TokenPairResponse(access_token=create_user_access_token(user.id), refresh_token=refresh)


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
    access_token = create_access_token(address=request.address, chain=request.chain)

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


@router.get("/me")
async def get_me(user: User = Depends(get_current_user)) -> CurrentUserResponse:
    """Return the authenticated user's profile (email/OAuth or wallet)."""
    return CurrentUserResponse(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
        address=user.address,
    )


# --- Wallet (EVM) challenge/verify ---


@router.post("/wallet/challenge")
async def wallet_challenge(request: WalletChallengeRequest) -> WalletChallengeResponse:
    """Issue a nonce message for an EVM wallet to sign."""
    async with AsyncSessionLocal() as db:
        message = await wallet_auth.create_challenge(db, request.address)
        await db.commit()
    return WalletChallengeResponse(message=message)


@router.post("/wallet/verify")
async def wallet_verify(request: WalletVerifyRequest) -> TokenPairResponse:
    """Verify a signed challenge and return a token pair (creates/links the wallet user)."""
    async with AsyncSessionLocal() as db:
        if not await wallet_auth.verify_signature(db, request.address, request.signature):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")
        user = await get_or_create_user_by_wallet(db, format_eth_address(request.address), "base")
        pair = await _issue_token_pair(db, user)
        await db.commit()
    return pair


# --- Email magic link ---


@router.post("/login/email", status_code=status.HTTP_204_NO_CONTENT)
async def login_email(request: EmailLoginRequest, background_tasks: BackgroundTasks) -> None:
    """Send a magic-link email (token + 6-digit code).

    The email is dispatched in the background so SMTP latency/failures never block the
    login request (the magic link is persisted before we return).
    """
    async with AsyncSessionLocal() as db:
        token, code = await magic_link.create_magic_link(db, request.email)
        await db.commit()
    background_tasks.add_task(magic_link.send_magic_link_email, request.email, token, code)


@router.post("/verify-magic-link")
async def verify_magic_link_route(request: VerifyMagicLinkRequest) -> TokenPairResponse:
    """Verify a magic link (by token or email+code) and return a token pair."""
    async with AsyncSessionLocal() as db:
        email = await magic_link.verify_magic_link(
            db, token=request.token, email=request.email, code=request.code
        )
        if email is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired link")
        user, _ = await get_or_create_user_by_email(db, email)
        pair = await _issue_token_pair(db, user)
        await db.commit()
    return pair


# --- OAuth (Google / GitHub) ---


@router.get("/oauth/{provider}")
async def oauth_start(provider: str) -> RedirectResponse:
    """Redirect to the provider's consent screen."""
    if provider not in oauth.SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown provider")
    state = secrets.token_urlsafe(16)
    redirect_uri = f"{config.API_URL}/auth/oauth/{provider}/callback"
    response = RedirectResponse(oauth.get_authorize_url(provider, state, redirect_uri))
    response.set_cookie("oauth_state", state, httponly=True, max_age=600, samesite="lax", secure=True)
    return response


@router.get("/oauth/{provider}/callback")
async def oauth_callback(
    provider: str, code: str, state: str, oauth_state: str = Cookie(default=None)
) -> RedirectResponse:
    """Provider redirect target: verify state, mint tokens, hand back a one-time code to the frontend."""
    if provider not in oauth.SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown provider")
    if not oauth_state or not secrets.compare_digest(oauth_state, state):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OAuth state")

    redirect_uri = f"{config.API_URL}/auth/oauth/{provider}/callback"
    info = await oauth.exchange_code_for_user_info(provider, code, redirect_uri)

    async with AsyncSessionLocal() as db:
        user, _ = await get_or_create_user_by_oauth(db, info)
        pair = await _issue_token_pair(db, user)
        one_time_code = secrets.token_urlsafe(32)
        db.add(
            AuthCode(
                code_hash=_hash(one_time_code),
                user_id=user.id,
                access_token=encrypt(pair.access_token),
                refresh_token=encrypt(pair.refresh_token),
                expires_at=datetime.now() + timedelta(seconds=_AUTH_CODE_TTL_SECONDS),
            )
        )
        await db.commit()

    response = RedirectResponse(f"{config.FRONTEND_URL}/auth/callback?code={one_time_code}")
    response.delete_cookie("oauth_state")
    return response


@router.post("/exchange")
async def exchange_code(request: ExchangeRequest) -> TokenPairResponse:
    """Exchange a one-time OAuth code for the token pair (single-use)."""
    async with AsyncSessionLocal() as db:
        auth_code = (
            await db.execute(select(AuthCode).where(AuthCode.code_hash == _hash(request.code)))
        ).scalars().first()
        if auth_code is None or auth_code.expires_at < datetime.now():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired code")
        # PKCE-bound codes (CLI loopback) require the matching verifier; OAuth codes don't.
        if auth_code.challenge is not None:
            if request.verifier is None or not _pkce_matches(request.verifier, auth_code.challenge):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid PKCE verifier")
        pair = TokenPairResponse(
            access_token=decrypt(auth_code.access_token), refresh_token=decrypt(auth_code.refresh_token)
        )
        await db.delete(auth_code)
        await db.commit()
    return pair


@router.post("/cli/code")
async def cli_code(request: CliCodeRequest, user: User = Depends(get_current_user)) -> CliCodeResponse:
    """Mint a one-time, PKCE-bound code for the CLI loopback flow.

    Called by the console SPA once the user is authenticated (by any method). The code is
    bound to the supplied PKCE challenge and exchanged by the CLI at /auth/exchange.
    """
    async with AsyncSessionLocal() as db:
        db_user = await db.get(User, user.id)
        if db_user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
        pair = await _issue_token_pair(db, db_user)
        one_time_code = secrets.token_urlsafe(32)
        db.add(
            AuthCode(
                code_hash=_hash(one_time_code),
                user_id=db_user.id,
                access_token=encrypt(pair.access_token),
                refresh_token=encrypt(pair.refresh_token),
                expires_at=datetime.now() + timedelta(seconds=_AUTH_CODE_TTL_SECONDS),
                challenge=request.challenge,
            )
        )
        await db.commit()
    return CliCodeResponse(code=one_time_code)


# --- Refresh / logout ---


@router.post("/refresh")
async def refresh_tokens(request: RefreshRequest) -> TokenPairResponse:
    """Rotate a refresh token (one-time use per token) and return a fresh pair."""
    try:
        payload = decode_token(request.refresh_token, REFRESH)
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    sid = payload.get("sid")
    async with AsyncSessionLocal() as db:
        session = await db.get(Session, uuid.UUID(sid)) if sid else None
        if session is None or session.revoked_at is not None or session.expires_at < datetime.now():
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
        if not secrets.compare_digest(session.refresh_token_hash, _hash(request.refresh_token)):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
        user = await db.get(User, session.user_id)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
        new_refresh = create_refresh_token(user.id, session.id)
        session.refresh_token_hash = _hash(new_refresh)
        await db.flush()
        access = create_user_access_token(user.id)
        await db.commit()
    return TokenPairResponse(access_token=access, refresh_token=new_refresh)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: RefreshRequest) -> None:
    """Revoke the session backing a refresh token."""
    try:
        payload = decode_token(request.refresh_token, REFRESH)
    except jwt.PyJWTError:
        return
    sid = payload.get("sid")
    if not sid:
        return
    async with AsyncSessionLocal() as db:
        session = await db.get(Session, uuid.UUID(sid))
        if session is not None and session.revoked_at is None:
            session.revoked_at = datetime.now()
            await db.commit()


# --- Account linking (logged-in) ---


@router.post("/link/wallet", status_code=status.HTTP_204_NO_CONTENT)
async def link_wallet_route(request: WalletVerifyRequest, user: User = Depends(get_current_user)) -> None:
    """Attach a verified EVM wallet to the logged-in user (e.g. a fiat user adding crypto)."""
    async with AsyncSessionLocal() as db:
        if not await wallet_auth.verify_signature(db, request.address, request.signature):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")
        await link_wallet(db, user, format_eth_address(request.address), "base")
        await db.commit()
