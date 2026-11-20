"""Bambuddy's webhook provider nudges a sync when a print ends, so review doesn't wait for the poll."""

from __future__ import annotations

import asyncio
import hmac
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from platewright.store.db import now
from platewright.web.context import Ctx

logger = logging.getLogger(__name__)

# Events after which the print log has something new for us. Anything else is acknowledged
# and ignored (the provider may have more events switched on).
SYNC_EVENTS = {"print_complete", "print_failed", "print_stopped", "print_confirm_request", "test"}
DELAY = 15  # seconds: let Bambuddy finish writing the archive and the print log first


def register(app: FastAPI, ctx: Ctx) -> None:
    pending: dict[str, asyncio.Task] = {}

    async def later() -> None:
        await asyncio.sleep(DELAY)
        try:
            await ctx.extra["sync"].run_now()
        except Exception:
            logger.exception("Sync after webhook failed")

    @app.post("/hooks/bambuddy")
    async def bambuddy_hook(request: Request):
        secret = ctx.cfg.webhook_secret
        if not secret:
            raise HTTPException(404, "Not set up")
        auth = request.headers.get("authorization", "")
        if not hmac.compare_digest(auth.encode(), f"Bearer {secret}".encode()):
            raise HTTPException(401, "Wrong or missing secret")
        try:
            body = await request.json()
        except ValueError:
            body = {}
        event = str((body or {}).get("event", "")) if isinstance(body, dict) else ""
        ctx.store.set_state("webhook", {"at": now(), "event": event})
        if event and event not in SYNC_EVENTS:
            return JSONResponse({"ok": True, "sync": False})
        task = pending.get("sync")
        if task is None or task.done():  # several events in a row make one sync
            pending["sync"] = asyncio.create_task(later())
        return JSONResponse({"ok": True, "sync": True}, status_code=202)
