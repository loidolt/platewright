"""Labels: bag labels for a demand's finished parts, spool labels, and the test label in Settings."""

from __future__ import annotations

import datetime as dt
import io
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from PIL import Image

from platewright import labels
from platewright.bambuddy.client import BambuddyError
from platewright.core.settings import Settings
from platewright.web.context import Ctx, to_int

MIXED = "_mixed"


def bag_content(d, model, qty: int | None, settings: Settings, today: dt.date) -> dict:
    """What a bag label says: the demand, what's inside, when it was packed, and the model's
    safety labels when it has them."""
    if model is not None:
        what = model["name"]
    else:
        what = f"mixed {settings.unit()}"
    headline = f"{qty} × {what}" if qty else what[:1].upper() + what[1:]
    due = dt.date.fromisoformat(d["due"]).strftime("%b %-d, %Y")
    lines = [f"{d['kind'].capitalize()} · due {due}", f"Packed {today.strftime('%b %-d, %Y')}"]
    if model is not None:
        safety = []
        if model["min_age"] is not None:
            safety.append(f"Ages {model['min_age']}+")
        if model["small_parts"] == "yes":
            safety.append("Small parts")
        if safety:
            lines.append(" · ".join(safety))
    return {"title": d["name"], "headline": headline, "lines": lines}


def register(app: FastAPI, ctx: Ctx) -> None:
    store = ctx.store

    def label_size(settings: Settings) -> labels.LabelSize:
        try:
            return labels.size(settings.label_size)
        except labels.LabelError:
            return labels.size("29x90")

    def bag_image(slug: str, model_slug: str, qty_text: str) -> Image.Image:
        d = store.demand(slug)
        if d is None:
            raise HTTPException(404, "No such demand")
        model = None
        if model_slug and model_slug != MIXED:
            model = store.model(model_slug)
            if model is None:
                raise HTTPException(400, "Unknown model")
        qty = to_int(qty_text) if str(qty_text or "").strip() else None
        if str(qty_text or "").strip() and (qty is None or qty > 100000):
            raise HTTPException(400, "How many is a whole number")
        settings = store.settings()
        if d["bambuddy_project_id"]:
            qr = f"{ctx.cfg.bambuddy_url}/projects/{d['bambuddy_project_id']}"
        else:
            qr = f"{ctx.cfg.public_url}/demand/{slug}"
        return labels.bag_label(label_size(settings), qr_url=qr,
                                **bag_content(d, model, qty, settings, dt.date.today()))

    async def send(img: Image.Image, copies: int = 1) -> None:
        s = store.settings()
        if not s.labels_on:
            raise labels.LabelError("no label printer is set up (Settings → Label printer)")
        await labels.print_label(img, s.label_printer_host, s.label_printer_model, s.label_size, copies,
                                 s.label_printer_protocol)

    # -- bag labels -------------------------------------------------------------------------------

    @app.get("/demand/{slug}/label.png")
    async def bag_label_png(request: Request, slug: str, model: str = "", qty: str = ""):
        ctx.user(request)
        return Response(labels.png(bag_image(slug, model, qty)), media_type="image/png",
                        headers={"Cache-Control": "no-store"})

    @app.get("/demand/{slug}/label/preview", response_class=HTMLResponse)
    async def bag_label_preview(request: Request, slug: str, model: str = "", qty: str = ""):
        """The preview picture for the form's current values (htmx swaps it in)."""
        ctx.user(request)
        if store.demand(slug) is None:
            raise HTTPException(404, "No such demand")
        query = urlencode({"model": model, "qty": qty})
        return ctx.page(request, "_label_preview.html", src=f"/demand/{slug}/label.png?{query}")

    @app.post("/demand/{slug}/label")
    async def print_bag_label(request: Request, slug: str, model: str = Form(""), qty: str = Form(""),
                              copies: int = Form(1, ge=1, le=labels.MAX_COPIES)):
        ctx.require(request, "operator")
        img = bag_image(slug, model, qty)
        try:
            await send(img, copies)
        except labels.LabelError as exc:
            return ctx.back(f"/demand/{slug}", f"Label not printed: {exc}", "bad")
        return ctx.back(f"/demand/{slug}", f"{copies} bag label{'' if copies == 1 else 's'} sent to the label printer")

    # -- spool labels -----------------------------------------------------------------------------

    @app.post("/filament/spools/{spool_id}/label")
    async def print_spool_label(request: Request, spool_id: int, copies: int = Form(1, ge=1, le=labels.MAX_COPIES)):
        ctx.require(request, "operator")
        spec = label_size(store.settings())
        try:
            data = await ctx.bambuddy.spool_label(spool_id, *spec.mm)
        except BambuddyError as exc:
            return ctx.back("/filament", f"Bambuddy didn't draw the label: {exc}", "bad")
        try:
            await send(Image.open(io.BytesIO(data)), copies)
        except labels.LabelError as exc:
            return ctx.back("/filament", f"Label not printed: {exc}", "bad")
        except OSError as exc:  # not a picture Pillow can read
            return ctx.back("/filament", f"Bambuddy's label couldn't be read: {exc}", "bad")
        return ctx.back("/filament", f"Label for spool #{spool_id} sent to the label printer")

    # -- test label (Settings) --------------------------------------------------------------------

    @app.post("/settings/test-label")
    async def test_label(request: Request):
        ctx.require(request, "admin")
        s = store.settings()
        spec = label_size(s)
        img = labels.bag_label(spec, title="Platewright test label", headline=spec.name,
                               lines=[f"{s.label_printer_model} at {s.label_printer_host}, {labels.PROTOCOLS.get(s.label_printer_protocol, '')}",
                                      dt.datetime.now().strftime("%b %-d, %Y %H:%M")],
                               qr_url=ctx.cfg.public_url)
        try:
            await send(img)
        except labels.LabelError as exc:
            return ctx.back("/settings#labels", f"Label not printed: {exc}", "bad")
        return ctx.back("/settings#labels", "Test label sent to the label printer")
