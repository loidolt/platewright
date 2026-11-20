"""Platewright's own session: a signed cookie, SameSite=Lax.

Run Platewright on a sibling host of Bambuddy (plates.example.com next to
print.example.com): the two then share a site, so inside Bambuddy's frame this
cookie is first-party and Lax is enough. On unrelated domains browsers treat
it as third-party and drop it. Nothing in it is secret (it is signed, not encrypted): who the
user is, as Bambuddy said at sign-in.
"""

from __future__ import annotations

import time
from dataclasses import asdict

from fastapi import Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer

from platewright.bambuddy.client import BambuddyUser
from platewright.config import Settings

SESSION_COOKIE = "platewright_session"
AUTH_COOKIE = "platewright_auth"
AUTH_MAX_AGE = 600  # the sign-in round trip; Bambuddy's code itself lives 60 s


class Sessions:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._session = URLSafeTimedSerializer(settings.session_key, salt="platewright.session")
        self._auth = URLSafeTimedSerializer(settings.session_key, salt="platewright.auth")

    def _set(self, response: Response, name: str, value: str, max_age: int, path: str = "/") -> None:
        response.set_cookie(
            name,
            value,
            max_age=max_age,
            path=path,
            secure=self.settings.cookie_secure,
            httponly=True,
            samesite="lax",
        )

    # Signed-in user -------------------------------------------------------

    def user(self, request: Request) -> dict | None:
        raw = request.cookies.get(SESSION_COOKIE)
        if not raw:
            return None
        try:
            data = self._session.loads(raw, max_age=self.settings.session_days * 86400)
        except BadSignature:
            return None
        return data if isinstance(data, dict) and "username" in data else None

    def sign_in(self, response: Response, user: BambuddyUser) -> None:
        data = asdict(user) | {"signed_in_at": int(time.time())}
        self._set(response, SESSION_COOKIE, self._session.dumps(data), self.settings.session_days * 86400)

    def sign_out(self, response: Response) -> None:
        response.delete_cookie(SESSION_COOKIE, path="/", secure=self.settings.cookie_secure, samesite="lax")

    # Sign-in round trip ---------------------------------------------------

    def start_auth(self, response: Response, *, state: str, verifier: str, next_path: str) -> None:
        value = self._auth.dumps({"state": state, "verifier": verifier, "next": next_path})
        self._set(response, AUTH_COOKIE, value, AUTH_MAX_AGE, path="/auth")

    def pending_auth(self, request: Request) -> dict | None:
        raw = request.cookies.get(AUTH_COOKIE)
        if not raw:
            return None
        try:
            return self._auth.loads(raw, max_age=AUTH_MAX_AGE)
        except BadSignature:
            return None

    def end_auth(self, response: Response) -> None:
        response.delete_cookie(AUTH_COOKIE, path="/auth", secure=self.settings.cookie_secure, samesite="lax")
