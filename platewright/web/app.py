"""Platewright's web app: sign-in with Bambuddy and the read-only pages (phase 1).

- sign in with Bambuddy inside Bambuddy's frame (connected app, PKCE);
- follow Bambuddy's theme and open its pages (postMessage, static/app.js);
- sync finished prints every few minutes and report the badge count after each pass;
- Today, Plates, Models and Filament pages;
- refuse to be framed by anything but Bambuddy.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from platewright import config
from platewright.bambuddy.badge import BadgeReporter
from platewright.bambuddy.client import Bambuddy, BambuddyError, SignInError, authorize_url, new_verifier
from platewright.bambuddy.sync import SyncLoop
from platewright.config import Settings as AppConfig
from platewright.config import origin_of
from platewright.core.mix import UNLINKED
from platewright.core.palette import norm_hex
from platewright.core.plan import demand_plan, fail_rate, filament, month_status
from platewright.core.plates import license_warning
from platewright.store.db import Store
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


class LoginRequired(Exception):
    def __init__(self, next_path: str):
        self.next_path = next_path


class Cached:
    """A Bambuddy read kept for a few seconds, so page loads don't hammer it."""

    def __init__(self, ttl: float):
        self.ttl = ttl
        self._at: float | None = None  # not 0: monotonic() can be below ttl right after boot
        self._value = None
        self._error: str | None = None

    async def get(self, fetch):
        if self._at is None or time.monotonic() - self._at > self.ttl:
            try:
                self._value, self._error = await fetch(), None
            except BambuddyError as exc:
                self._error = str(exc)
            self._at = time.monotonic()
        return self._value, self._error


def create_app(app_config: AppConfig | None = None, transport: httpx.AsyncBaseTransport | None = None,
               store: Store | None = None) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
    access_log = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactCallbackQuery) for f in access_log.filters):
        access_log.addFilter(RedactCallbackQuery())

    cfg = app_config or config.load()
    store = store or Store(cfg.db_path)
    if cfg.seed_file and Path(cfg.seed_file).is_file():
        if store.seed_settings(json.loads(Path(cfg.seed_file).read_text())):
            logger.info("Settings seeded from %s", cfg.seed_file)
    bambuddy = Bambuddy(cfg, transport=transport)
    sessions = Sessions(cfg)
    templates = Jinja2Templates(directory=HERE / "templates")
    # Static URLs carry a hash of the file, so a deploy never meets a stale cached copy.
    versions = {f.name: hashlib.sha256(f.read_bytes()).hexdigest()[:10]
                for f in (HERE / "static").iterdir() if f.is_file()}
    templates.env.globals.update(config=cfg, UNLINKED=UNLINKED,
                                 static=lambda name: f"/static/{name}?v={versions.get(name, '0')}")
    templates.env.filters["norm_hex"] = norm_hex
    templates.env.filters["fmt_date"] = lambda d: dt.date.fromisoformat(d[:10]).strftime("%b %-d") if d else ""

    def badge_count() -> int:
        today = dt.date.today()
        plan = demand_plan(store.open_demands(), store.delivered(),
                           {k: v["on_hand"] for k, v in store.stock().items()}, store.active_plates(),
                           fail_rate(store.run_totals(), store.settings()), store.settings(), today)
        return len(store.awaiting_verdict(today=today)) + sum(p.at_risk for p in plan)

    badge = BadgeReporter(bambuddy, badge_count)
    sync = SyncLoop(bambuddy, store, cfg.sync_interval, after=badge.report)
    spools_cache = Cached(60)
    printers_cache = Cached(20)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        sync.start()
        try:
            yield
        finally:
            await sync.stop()
            await bambuddy.aclose()

    app = FastAPI(title="Platewright", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = cfg
    app.state.store = store
    app.state.badge = badge
    app.state.sync = sync
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
        f"frame-ancestors {cfg.bambuddy_origin}"
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

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired):
        if not cfg.sign_in_configured:
            return page(request, "message.html", 503, title="Sign-in not set up",
                        message="PLATEWRIGHT_CLIENT_ID and PLATEWRIGHT_CLIENT_SECRET are not set.")
        if request.headers.get("HX-Request"):
            # An htmx poll or button: reload the page it sits on, which then signs in again.
            current = urlsplit(request.headers.get("HX-Current-URL", ""))
            page_path = current.path + (f"?{current.query}" if current.query else "")
            return Response(status_code=204, headers={"HX-Redirect": _safe_next(page_path)})
        next_path = exc.next_path if request.method == "GET" else "/"
        # Opened from the sidebar: go straight to Bambuddy, no button. After the first
        # consent Bambuddy answers without a prompt.
        return RedirectResponse(f"/auth/login?next={quote(next_path, safe='/')}", status_code=303)

    def page(request: Request, name: str, status_code: int = 200, **context) -> HTMLResponse:
        context.setdefault("user", sessions.user(request))
        context.setdefault("settings", store.settings())
        context.setdefault("nav", "")
        response = templates.TemplateResponse(request, name, context, status_code=status_code)
        response.headers["Cache-Control"] = "no-store"
        return response

    def require_user(request: Request) -> dict:
        user = sessions.user(request)
        if user is None:
            query = f"?{request.url.query}" if request.url.query else ""
            raise LoginRequired(_safe_next(request.url.path + query))
        return user

    def require_same_origin(request: Request) -> None:
        # POSTs come from our own pages only. The Lax cookie already keeps
        # other sites' forms out; this also covers sibling subdomains.
        origin = request.headers.get("origin")
        if origin is not None and origin != origin_of(cfg.public_url):
            raise HTTPException(403, "Cross-origin request refused")

    def models_by_slug() -> dict:
        return {r["slug"]: r for r in store.models()}

    # -- pages ------------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def today_page(request: Request):
        user = require_user(request)
        today = dt.date.today()
        settings = store.settings()
        totals = store.run_totals()
        plates = store.active_plates()
        stock = store.stock()
        plan = demand_plan(store.open_demands(), store.delivered(), {k: v["on_hand"] for k, v in stock.items()},
                           plates, fail_rate(totals, settings), settings, today)
        return page(request, "today.html", user=user, nav="today", month=month_status(totals, plates, settings, today),
                    awaiting=store.awaiting_verdict(today=today), plan=plan, recent=store.runs(limit=10),
                    last_sync=store.state("last_sync"), badge=badge.state, models=models_by_slug(),
                    on_hand=sum(max(v["on_hand"], 0) for v in stock.values()), problems=settings.problems())

    @app.get("/plates", response_class=HTMLResponse)
    async def plates_page(request: Request):
        user = require_user(request)
        return page(request, "plates.html", user=user, nav="plates", plates=store.plates(), models=models_by_slug())

    @app.get("/plates/{code}", response_class=HTMLResponse)
    async def plate_page(request: Request, code: str):
        user = require_user(request)
        versions = store.plate_versions(code.upper())
        if not versions:
            raise HTTPException(404, "No such plate")
        runs = store.runs(plate_code=versions[0].code, limit=20)
        return page(request, "plate.html", user=user, nav="plates", versions=versions, runs=runs,
                    models=models_by_slug())

    @app.get("/models", response_class=HTMLResponse)
    async def models_page(request: Request):
        user = require_user(request)
        stock = store.stock()
        core = {m.slug: m for m in store.core_models()}
        on_plates: dict[str, list[str]] = {}
        for p in store.plates(active_only=True):
            for slug in p.mix:
                on_plates.setdefault(slug, []).append(p.code)
        return page(request, "models.html", user=user, nav="models", models=store.models(), stock=stock,
                    on_plates=on_plates, warnings={s: license_warning(m) for s, m in core.items()})

    @app.get("/filament", response_class=HTMLResponse)
    async def filament_page(request: Request):
        user = require_user(request)
        settings = store.settings()
        spools, error = await spools_cache.get(bambuddy.spools)
        rows, other = filament(spools or [], store.run_totals(), settings, dt.date.today())
        return page(request, "filament.html", user=user, nav="filament", rows=rows, other=other, error=error,
                    loaded=spools is not None)

    @app.get("/partials/printing", response_class=HTMLResponse)
    async def printing_partial(request: Request):
        require_user(request)

        async def fetch():
            out = []
            for p in await bambuddy.printers():
                try:
                    status = await bambuddy.printer_status(p["id"])
                except BambuddyError:
                    status = {}
                out.append({"name": p.get("name") or f"printer {p['id']}", "id": p["id"], **status})
            return out

        printers, error = await printers_cache.get(fetch)
        return page(request, "_printing.html", printers=printers or [], error=error)

    @app.post("/sync", response_class=HTMLResponse)
    async def sync_now(request: Request):
        require_same_origin(request)
        user = require_user(request)
        if not user.get("is_admin"):
            raise HTTPException(403, "Admins only")
        await sync.run_now()
        return page(request, "_sync.html", user=user, last_sync=store.state("last_sync"), badge=badge.state)

    @app.get("/whoami", response_class=HTMLResponse)
    async def whoami(request: Request):
        return page(request, "_whoami.html")

    # -- sign in with Bambuddy ------------------------------------------------------------

    @app.get("/auth/login")
    async def login(request: Request, next: str | None = None):
        if not cfg.sign_in_configured:
            raise HTTPException(503, "Sign-in not set up")
        state = secrets.token_urlsafe(24)
        verifier = new_verifier()
        response = RedirectResponse(authorize_url(cfg, state=state, verifier=verifier), status_code=303)
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
        response = page(request, "message.html", user=None, title="Signed out",
                        message="You are signed out of Platewright (not of Bambuddy).", retry=True)
        sessions.sign_out(response)
        return response

    @app.get("/health")
    async def health():
        return JSONResponse({"status": "ok"})

    @app.get("/favicon.ico")
    async def favicon():
        return Response(status_code=204)

    return app
