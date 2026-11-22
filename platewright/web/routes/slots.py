"""AMS slots (Filament page): each printer's slots with their live RFID tag and assigned spool, and
"Move tag to spool" for a tag Bambuddy linked to the wrong spool by itself."""

from __future__ import annotations

import time

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse

from platewright.bambuddy.client import BambuddyError
from platewright.bambuddy.tags import MoveFailed, move_tag
from platewright.core.slots import PrinterSlots, find_slot, printer_slots
from platewright.web.context import Ctx, can

ACCESS_TTL = 300  # seconds the Manage Inventory answer is kept


def register(app: FastAPI, ctx: Ctx) -> None:
    bambuddy = ctx.bambuddy
    access: dict = {"at": None, "value": (None, "")}

    async def load() -> list[PrinterSlots]:
        """Fresh from Bambuddy, no cache: the panel is read once per page and after each move."""
        printers = await bambuddy.printers()
        assignments = await bambuddy.assignments()
        spools = await bambuddy.spools()
        out = []
        for p in printers:
            try:
                status = await bambuddy.printer_status(p["id"])
            except BambuddyError:
                status = {}
            out.append(printer_slots(p, status, assignments, spools))
        return out

    async def write_access(fresh: bool = False) -> tuple[bool | None, str]:
        if fresh or access["at"] is None or time.monotonic() - access["at"] > ACCESS_TTL:
            access["value"], access["at"] = await bambuddy.inventory_write_access(), time.monotonic()
        return access["value"]

    async def panel(request: Request, status_code: int = 200, message: str = "", tone: str = "",
                    fresh_access: bool = False) -> HTMLResponse:
        user = ctx.user(request)
        try:
            printers, error = await load(), None
        except BambuddyError as exc:
            printers, error = [], str(exc)
        operator = can(user, ctx.store.settings(), "operator")
        write, write_error = await write_access(fresh_access) if operator else (None, "")
        return ctx.page(request, "_slots.html", status_code, user=user, printers=printers, error=error,
                        operator=operator, write=write, write_error=write_error, message=message, tone=tone)

    def done(message: str, tone: str = ""):
        response = ctx.back("/filament", message, tone)
        response.headers["location"] += "#slots"
        return response

    @app.get("/partials/slots", response_class=HTMLResponse)
    async def slots_partial(request: Request):
        return await panel(request)

    @app.post("/filament/slots/move")
    async def move(request: Request, printer_id: int = Form(...), ams_id: int = Form(...),
                   tray_id: int = Form(...), tag: str = Form(""), spool_id: int | None = Form(None)):
        ctx.require(request, "operator")
        htmx = bool(request.headers.get("HX-Request"))

        async def answer(message: str, tone: str = "", fresh_access: bool = False):
            if htmx:
                return await panel(request, message=message, tone=tone, fresh_access=fresh_access)
            return done(message, tone)

        if spool_id is None:
            return await answer("Choose the spool the tag belongs to.", "warn")
        allowed, why = await write_access()
        if allowed is False:
            return await answer(f"The API key can't change Bambuddy's inventory: {why}", "bad")
        try:
            printers = await load()
        except BambuddyError as exc:
            return await answer(f"Bambuddy couldn't be read: {exc}", "bad")
        slot = find_slot(printers, printer_id, ams_id, tray_id)
        if slot is None or f"{slot.tag_uid}:{slot.tray_uuid}" != tag:
            return await answer("This slot changed since the page was loaded. Have another look.", "warn")
        if spool_id not in {c["id"] for c in slot.candidates}:
            return await answer(f"Spool #{spool_id} isn't one this tag can go to.", "warn")
        where = f"{next(p.name for p in printers if p.id == printer_id)} {slot.label}"
        try:
            await move_tag(bambuddy, slot, spool_id)
        except MoveFailed as exc:
            why = str(exc).rstrip(".")
            left = f" Done before that: {'; '.join(exc.done)}." if exc.done else " Nothing was changed."
            return await answer(f"{why}.{left}", "bad", fresh_access=exc.error.status in (401, 403))
        before = f" from spool #{slot.holder['id']}" if slot.holder else ""
        return await answer(f"Tag in {where} moved{before} to spool #{spool_id}")
