"""OAuth (Google, GitHub) — build the authorize URL and exchange a code for normalized user info.

Direct httpx implementation (no framework session needed); creds come from config.
"""

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from src.config import config

SUPPORTED_PROVIDERS = ("google", "github")


@dataclass
class OAuthUserInfo:
    provider: str
    provider_id: str
    email: str | None
    name: str | None
    avatar_url: str | None


def _client_credentials(provider: str) -> tuple[str, str]:
    if provider == "google":
        return config.GOOGLE_CLIENT_ID, config.GOOGLE_CLIENT_SECRET
    if provider == "github":
        return config.GITHUB_CLIENT_ID, config.GITHUB_CLIENT_SECRET
    raise ValueError(f"Unsupported OAuth provider: {provider}")


def get_authorize_url(provider: str, state: str, redirect_uri: str) -> str:
    client_id, _ = _client_credentials(provider)
    if provider == "google":
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    if provider == "github":
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": "read:user user:email",
            "state": state,
        }
        return f"https://github.com/login/oauth/authorize?{urlencode(params)}"
    raise ValueError(f"Unsupported OAuth provider: {provider}")


async def exchange_code_for_user_info(provider: str, code: str, redirect_uri: str) -> OAuthUserInfo:
    client_id, client_secret = _client_credentials(provider)
    async with httpx.AsyncClient(timeout=10.0) as client:
        if provider == "google":
            return await _google_user_info(client, client_id, client_secret, code, redirect_uri)
        if provider == "github":
            return await _github_user_info(client, client_id, client_secret, code, redirect_uri)
    raise ValueError(f"Unsupported OAuth provider: {provider}")


async def _google_user_info(
    client: httpx.AsyncClient, client_id: str, client_secret: str, code: str, redirect_uri: str
) -> OAuthUserInfo:
    token_resp = await client.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
    )
    token_resp.raise_for_status()
    access_token = token_resp.json()["access_token"]

    info_resp = await client.get(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    info_resp.raise_for_status()
    data = info_resp.json()
    return OAuthUserInfo(
        provider="google",
        provider_id=str(data["sub"]),
        email=data.get("email"),
        name=data.get("name"),
        avatar_url=data.get("picture"),
    )


async def _github_user_info(
    client: httpx.AsyncClient, client_id: str, client_secret: str, code: str, redirect_uri: str
) -> OAuthUserInfo:
    token_resp = await client.post(
        "https://github.com/login/oauth/access_token",
        headers={"Accept": "application/json"},
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        },
    )
    token_resp.raise_for_status()
    access_token = token_resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"}

    user_resp = await client.get("https://api.github.com/user", headers=headers)
    user_resp.raise_for_status()
    user = user_resp.json()

    # GitHub may omit a public email on /user; resolve the primary one.
    email: str | None = user.get("email")
    emails_resp = await client.get("https://api.github.com/user/emails", headers=headers)
    if emails_resp.status_code == 200:
        for entry in emails_resp.json():
            if entry.get("primary"):
                email = entry.get("email")
                break

    return OAuthUserInfo(
        provider="github",
        provider_id=str(user["id"]),
        email=email,
        name=user.get("name") or user.get("login"),
        avatar_url=user.get("avatar_url"),
    )
