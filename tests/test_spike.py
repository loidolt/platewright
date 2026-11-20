import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from platewright.bambuddy.client import s256
from platewright.config import Settings
from platewright.web.app import _safe_next, create_app

SETTINGS = Settings(
    public_url="https://plates.example.test",
    bambuddy_url="https://print.example.test",
    bambuddy_api_url="http://10.0.0.5:3700",
    bambuddy_key="bb_testkey",
    client_id="bba_client",
    client_secret="bbs_secret",
    session_key="x" * 40,
    cookie_secure=False,
    session_days=30,
    badge_interval=3600,
    badge_count=4,
)

USER = {
    "id": 7,
    "username": "chris",
    "email": "c@example.test",
    "is_admin": True,
    "groups": ["Administrators"],
    "permissions": ["a:read", "b:update"],
}


class FakeBambuddy:
    """The two endpoints Platewright calls, with Bambuddy v1.3.1's checks."""

    def __init__(self):
        self.challenges: dict[str, str] = {}  # code -> challenge
        self.badges: list[dict] = []
        self.badge_status = 200
        self.token_error: str | None = None

    def issue(self, authorize_url: str) -> tuple[str, str]:
        q = parse_qs(urlsplit(authorize_url).query)
        code = f"code-{len(self.challenges)}"
        self.challenges[code] = q["code_challenge"][0]
        return code, q["state"][0]

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/connect/token":
            body = json.loads(request.content)
            if self.token_error:
                return httpx.Response(400, json={"detail": {"error": self.token_error}})
            assert body["grant_type"] == "authorization_code"
            assert body["redirect_uri"] == "https://plates.example.test/auth/callback"
            assert body["client_secret"] == "bbs_secret"
            challenge = self.challenges.pop(body["code"], None)
            if challenge is None or s256(body["code_verifier"]) != challenge:
                return httpx.Response(400, json={"detail": {"error": "invalid_grant"}})
            return httpx.Response(200, json={"user": USER, "issued_at": "2026-11-20T00:00:00Z"})
        if request.url.path == "/api/v1/external-links/badge":
            assert request.headers["X-API-Key"] == "bb_testkey"
            self.badges.append(json.loads(request.content))
            if self.badge_status != 200:
                return httpx.Response(self.badge_status, json={"detail": "nope"})
            return httpx.Response(200, json={"links": 1})
        return httpx.Response(404)


@pytest.fixture
def fake():
    return FakeBambuddy()


@pytest.fixture
def client(fake):
    app = create_app(SETTINGS, transport=httpx.MockTransport(fake.handler))
    with TestClient(app, base_url="https://plates.example.test", follow_redirects=False) as c:
        yield c


def sign_in(client, fake):
    r = client.get("/auth/login")
    code, state = fake.issue(r.headers["location"])
    return client.get("/auth/callback", params={"code": code, "state": state})


def test_home_without_session_goes_straight_to_sign_in(client):
    r = client.get("/")
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/login"


def test_login_redirects_to_bambuddy_with_pkce(client):
    r = client.get("/auth/login")
    assert r.status_code == 303
    url = urlsplit(r.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://print.example.test/connect/authorize"
    q = parse_qs(url.query)
    assert q["client_id"] == ["bba_client"]
    assert q["redirect_uri"] == ["https://plates.example.test/auth/callback"]
    assert q["code_challenge_method"] == ["S256"]
    assert len(q["code_challenge"][0]) == 43
    cookie = r.headers["set-cookie"]
    assert "platewright_auth=" in cookie and "SameSite=lax" in cookie and "HttpOnly" in cookie


def test_full_sign_in_shows_user(client, fake):
    r = sign_in(client, fake)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "platewright_session=" in r.headers["set-cookie"]
    page = client.get("/")
    assert page.status_code == 200
    assert "chris" in page.text
    assert "Administrators" in page.text
    assert "<dt>is_admin</dt><dd>true</dd>" in page.text
    assert client.get("/whoami").text.strip().startswith('<span class="good">cookie sent: chris')


def test_callback_rejects_wrong_state(client, fake):
    r = client.get("/auth/login")
    code, _ = fake.issue(r.headers["location"])
    r = client.get("/auth/callback", params={"code": code, "state": "forged"})
    assert r.status_code == 400
    assert "doesn" in r.text


def test_callback_without_auth_cookie_explains(client):
    r = client.get("/auth/callback", params={"code": "c", "state": "s"})
    assert r.status_code == 400
    assert "cookie" in r.text.lower()


def test_callback_reports_bambuddy_refusal(client, fake):
    fake.token_error = "invalid_client"
    r = sign_in(client, fake)
    assert r.status_code == 502
    assert "invalid_client" in r.text
    assert client.get("/").status_code == 303  # still signed out


def test_denied_consent(client):
    client.get("/auth/login")
    r = client.get("/auth/callback", params={"error": "access_denied", "state": "x"})
    assert r.status_code == 400
    assert "access_denied" in r.text


def test_frame_ancestors_only_bambuddy(client):
    for path in ("/", "/health", "/auth/callback"):
        csp = client.get(path).headers["content-security-policy"]
        assert csp.endswith("frame-ancestors https://print.example.test")
        assert "x-frame-options" not in client.get(path).headers


def test_badge_reported_on_start_and_on_demand(client, fake):
    assert fake.badges[0] == {"url": "https://plates.example.test", "count": 4}
    sign_in(client, fake)
    r = client.post("/badge", data={"count": "9"})
    assert r.status_code == 200
    assert fake.badges[-1] == {"url": "https://plates.example.test", "count": 9}
    assert "ok" in r.text


def test_badge_set_needs_admin_session(client):
    assert client.post("/badge", data={"count": "2"}).status_code == 403


def test_badge_404_is_explained(client, fake):
    fake.badge_status = 404
    sign_in(client, fake)
    r = client.post("/badge", data={"count": "1"})
    assert "no sidebar link" in r.text


def test_cross_origin_post_refused(client, fake):
    sign_in(client, fake)
    r = client.post("/badge", data={"count": "1"}, headers={"Origin": "https://evil.example.test"})
    assert r.status_code == 403


def test_logout(client, fake):
    sign_in(client, fake)
    r = client.post("/auth/logout")
    assert r.status_code == 200
    assert client.get("/").status_code == 303


@pytest.mark.parametrize(
    "value,expected",
    [(None, "/"), ("/x?y=1", "/x?y=1"), ("//evil", "/"), ("/\\evil", "/"), ("https://evil", "/")],
)
def test_safe_next(value, expected):
    assert _safe_next(value) == expected


def test_config_requires_urls_outside_dev(monkeypatch):
    from platewright import config

    for name in ("PLATEWRIGHT_DEV", "PLATEWRIGHT_PUBLIC_URL", "PLATEWRIGHT_BAMBUDDY_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PLATEWRIGHT_SESSION_KEY", "k" * 40)
    with pytest.raises(config.ConfigError, match="required"):
        config.load()
    monkeypatch.setenv("PLATEWRIGHT_DEV", "1")
    assert config.load().public_url == "http://localhost:3710"
