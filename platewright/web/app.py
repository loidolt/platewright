"""Phase 0 spike: a hello page that proves the sidebar-app setup.

- sign in with Bambuddy inside Bambuddy's frame (connected app, PKCE);
- follow Bambuddy's theme (postMessage, see static/app.js);
- ask Bambuddy to open its queue (bambuddy:navigate);
- report a badge count every few minutes;
- refuse to be framed by anything but Bambuddy.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from platewright import config
from platewright.bambuddy.badge import BadgeReporter
from platewright.bambuddy.client import Bambuddy, SignInError, authorize_url, new_verifier
from platewright.config import Settings, origin_of
from platewright.web.session import Sessions

logger = logging.getLogger(__name__)

HERE = Path(__file__).parent


def _safe_next(value: str | None) -> str:
    """Only a plain path on this site; "//host" and "/\\host" are other sites."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


class RedactCallbackQuery(logging.Filter):
    """Keep the sign-in code and state out of the access log. uvicorn logs
    (client, method, path?query, http version, status)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) \
                and args[2].startswith("/auth/callback?"):
            record.args = (*args[:2], "/auth/callback?…", *args[3:])
        return True


def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
    access_log = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactCallbackQuery) for f in access_log.filters):
        access_log.addFilter(RedactCallbackQuery())
    settings = settings or config.load()
    bambuddy = Bambuddy(settings, transport=transport)
    badge = BadgeReporter(bambuddy, count=settings.badge_count, interval=settings.badge_interval)
    sessions = Sessions(settings)
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(settings=settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        badge.start()
        try:
            yield
        finally:
            await badge.stop()
            await bambuddy.aclose()

    app = FastAPI(title="Platewright", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.badge = badge
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    csp = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "object-src 'none'; "
        f"frame-ancestors {settings.bambuddy_origin}"
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        # Only Bambuddy may frame Platewright. frame-ancestors replaces
        # X-Frame-Options, which can't name another origin.
        response.headers["Content-Security-Policy"] = csp
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

    def page(request: Request, name: str, status_code: int = 200, **context) -> HTMLResponse:
        response = templates.TemplateResponse(request, name, context, status_code=status_code)
        response.headers["Cache-Control"] = "no-store"
        return response

    def require_same_origin(request: Request) -> None:
        # POSTs come from our own pages only. The Lax cookie already keeps
        # other sites' forms out; this also covers sibling subdomains.
        origin = request.headers.get("origin")
        if origin is not None and origin != origin_of(settings.public_url):
            raise HTTPException(403, "Cross-origin request refused")

    # -- pages ------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        user = sessions.user(request)
        if user is None:
            if not settings.sign_in_configured:
                return page(request, "message.html", 503, title="Sign-in not set up",
                            message="PLATEWRIGHT_CLIENT_ID and PLATEWRIGHT_CLIENT_SECRET are not set.")
            # Opened from the sidebar: go straight to Bambuddy, no button.
            # After the first consent Bambuddy answers without a prompt.
            return RedirectResponse("/auth/login", status_code=303)
        return page(request, "hello.html", user=user, badge=badge.state, badge_configured=badge.configured)

    @app.get("/whoami", response_class=HTMLResponse)
    async def whoami(request: Request):
        """htmx probe: does an XHR from inside the frame carry the cookie?"""
        user = sessions.user(request)
        return page(request, "_whoami.html", user=user)

    # -- sign in with Bambuddy -------------------------------------------

    @app.get("/auth/login")
    async def login(request: Request, next: str | None = None):
        if not settings.sign_in_configured:
            raise HTTPException(503, "Sign-in not set up")
        state = secrets.token_urlsafe(24)
        verifier = new_verifier()
        response = RedirectResponse(authorize_url(settings, state=state, verifier=verifier), status_code=303)
        sessions.start_auth(response, state=state, verifier=verifier, next_path=_safe_next(next))
        return response

    @app.get("/auth/callback", response_class=HTMLResponse)
    async def callback(request: Request, code: str | None = None, state: str | None = None,
                       error: str | None = None):
        pending = sessions.pending_auth(request)

        def fail(title: str, message: str, status: int = 400) -> HTMLResponse:
            response = page(request, "message.html", status, title=title, message=message, retry=True)
            sessions.end_auth(response)
            return response

        if error:
            return fail("Not signed in", f"Bambuddy answered: {error}.")
        if pending is None:
            # Missing means the cookie set before going to Bambuddy didn't
            # come back: blocked, expired (10 min) or a different browser.
            return fail("Sign-in cookie missing",
                        "The cookie Platewright set before going to Bambuddy did not come back. "
                        "If this is inside Bambuddy's frame, the browser is blocking Platewright's cookies.")
        if not code or not state or not hmac.compare_digest(state, pending["state"]):
            return fail("Sign-in mismatch", "The answer from Bambuddy doesn't belong to this sign-in.")
        try:
            user = await bambuddy.exchange_code(code, pending["verifier"])
        except SignInError as exc:
            logger.warning("Token exchange failed: %s (HTTP %s)", exc.error, exc.status)
            return fail("Bambuddy refused the sign-in", f"{exc.error}", 502)
        logger.info("Signed in %s (admin=%s)", user.username, user.is_admin)
        response = RedirectResponse(_safe_next(pending.get("next")), status_code=303)
        sessions.sign_in(response, user)
        sessions.end_auth(response)
        return response

    @app.post("/auth/logout", response_class=HTMLResponse)
    async def logout(request: Request):
        require_same_origin(request)
        response = page(request, "message.html", title="Signed out",
                        message="You are signed out of Platewright (not of Bambuddy).", retry=True)
        sessions.sign_out(response)
        return response

    # -- badge --------------------------------------------------------------

    @app.get("/badge", response_class=HTMLResponse)
    async def badge_status(request: Request):
        return page(request, "_badge.html", badge=badge.state, badge_configured=badge.configured,
                    user=sessions.user(request))

    @app.post("/badge", response_class=HTMLResponse)
    async def badge_set(request: Request, count: int = Form(..., ge=0, le=999)):
        require_same_origin(request)
        user = sessions.user(request)
        if user is None or not user.get("is_admin"):
            raise HTTPException(403, "Admins only")
        badge.set_count(count)
        await badge.report()
        return page(request, "_badge.html", badge=badge.state, badge_configured=badge.configured, user=user)

    @app.get("/health")
    async def health():
        return JSONResponse({"status": "ok"})

    @app.get("/favicon.ico")
    async def favicon():
        return Response(status_code=204)

    return app
