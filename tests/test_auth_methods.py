from src.config import config
from src.services import oauth
from src.services.magic_link import create_magic_link, verify_magic_link

# --- magic link ---


async def test_magic_link_verify_by_token(db, monkeypatch):
    monkeypatch.setattr(config, "MAGIC_LINK_SECRET", "test-secret")
    token, _code = await create_magic_link(db, "User@Example.com")
    assert await verify_magic_link(db, token=token) == "user@example.com"
    # single-use
    assert await verify_magic_link(db, token=token) is None


async def test_magic_link_verify_by_code_and_attempts(db, monkeypatch):
    monkeypatch.setattr(config, "MAGIC_LINK_SECRET", "test-secret")
    _token, code = await create_magic_link(db, "code@example.com")

    assert await verify_magic_link(db, email="code@example.com", code="000000") is None  # wrong code
    assert await verify_magic_link(db, email="code@example.com", code=code) == "code@example.com"


# --- oauth ---


def test_authorize_url_contains_client_id(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "gid")
    monkeypatch.setattr(config, "GITHUB_CLIENT_ID", "ghid")
    google = oauth.get_authorize_url("google", "state123", "https://api/cb")
    assert "accounts.google.com" in google and "client_id=gid" in google and "state=state123" in google
    github = oauth.get_authorize_url("github", "state123", "https://api/cb")
    assert "github.com/login/oauth/authorize" in github and "client_id=ghid" in github


class _FakeResp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status

    def json(self):
        return self._data

    def raise_for_status(self):
        return None


class _FakeClient:
    def __init__(self, routes):
        self._routes = routes

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        return self._routes[("POST", url)]

    async def get(self, url, **kwargs):
        return self._routes[("GET", url)]


async def test_google_exchange_maps_user_info(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "gid")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", "gsecret")
    routes = {
        ("POST", "https://oauth2.googleapis.com/token"): _FakeResp({"access_token": "at"}),
        ("GET", "https://www.googleapis.com/oauth2/v3/userinfo"): _FakeResp(
            {"sub": "123", "email": "g@example.com", "email_verified": True, "name": "G User", "picture": "http://a"}
        ),
    }
    monkeypatch.setattr(oauth.httpx, "AsyncClient", lambda **kwargs: _FakeClient(routes))

    info = await oauth.exchange_code_for_user_info("google", "code", "https://api/cb")
    assert info.provider == "google"
    assert info.provider_id == "123"
    assert info.email == "g@example.com"
