"""What every route needs: config, store, Bambuddy, templates, and the sign-in / role checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from platewright.bambuddy.client import Bambuddy
from platewright.config import Settings as AppConfig
from platewright.config import origin_of
from platewright.core.settings import Settings
from platewright.store.db import Store
from platewright.web.session import Sessions

ROLES = ("viewer", "operator", "admin")


class LoginRequired(Exception):
    def __init__(self, next_path: str):
        self.next_path = next_path


def to_int(text, default: int | None = None) -> int | None:
    """A whole number typed in a form; ``default`` when blank or not plain ASCII digits."""
    text = str(text if text is not None else "").strip()
    return int(text) if text.isascii() and text.isdigit() else default


def safe_next(value: str | None) -> str:
    """Only a plain path on this site; "//host" and "/\\host" are other sites."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def role_of(user: dict | None, settings: Settings) -> str:
    """Admin: Bambuddy admins. Operator: members of the operator group. Viewer: everyone else."""
    if not user:
        return "viewer"
    if user.get("is_admin"):
        return "admin"
    if settings.operator_group and settings.operator_group in (user.get("groups") or []):
        return "operator"
    return "viewer"


def can(user: dict | None, settings: Settings, need: str) -> bool:
    return ROLES.index(role_of(user, settings)) >= ROLES.index(need)


@dataclass
class Ctx:
    cfg: AppConfig
    store: Store
    bambuddy: Bambuddy
    sessions: Sessions
    templates: Jinja2Templates
    extra: dict = field(default_factory=dict)  # sync loop, badge, caches

    def page(self, request: Request, name: str, status_code: int = 200, **context) -> HTMLResponse:
        user = context.setdefault("user", self.sessions.user(request))
        settings = context.setdefault("settings", self.store.settings())
        context.setdefault("nav", "")
        context.setdefault("role", role_of(user, settings))
        context.setdefault("notice", request.query_params.get("notice", "")[:300])
        tone = request.query_params.get("tone", "")
        context.setdefault("notice_tone", tone if tone in ("bad", "warn") else "")
        response = self.templates.TemplateResponse(request, name, context, status_code=status_code)
        response.headers["Cache-Control"] = "no-store"
        return response

    def user(self, request: Request) -> dict:
        user = self.sessions.user(request)
        if user is None:
            query = f"?{request.url.query}" if request.url.query else ""
            raise LoginRequired(safe_next(request.url.path + query))
        return user

    def require(self, request: Request, need: str) -> dict:
        """The signed-in user, if their role allows ``need``. Writes also check the origin."""
        user = self.user(request)
        if request.method != "GET":
            self.same_origin(request)
        if not can(user, self.store.settings(), need):
            raise HTTPException(403, f"Needs the {need} role")
        return user

    def same_origin(self, request: Request) -> None:
        # Writes come from our own pages only. The Lax cookie already keeps other sites'
        # forms out; this also covers sibling subdomains.
        origin = request.headers.get("origin")
        if origin is not None and origin != origin_of(self.cfg.public_url):
            raise HTTPException(403, "Cross-origin request refused")

    def models_by_slug(self) -> dict:
        return {r["slug"]: r for r in self.store.models()}

    @staticmethod
    def back(path: str, notice: str = "", tone: str = "") -> RedirectResponse:
        """Post / redirect / get, with a one-line notice for the next page: done (the default),
        or tone "warn" / "bad" when it didn't go as asked."""
        query = {"notice": notice, **({"tone": tone} if tone else {})}
        return RedirectResponse(f"{path}?{urlencode(query)}" if notice else path, status_code=303)
