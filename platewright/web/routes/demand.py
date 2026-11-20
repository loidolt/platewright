"""Demand: orders, events and restock targets, their coverage, and Queue N plates in Bambuddy."""

from __future__ import annotations

import datetime as dt

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from platewright.bambuddy.client import BambuddyError
from platewright.core.names import slugify
from platewright.core.plan import demand_plan, fail_rate
from platewright.web.context import Ctx, to_int

KINDS = ("order", "event", "restock")


def register(app: FastAPI, ctx: Ctx) -> None:
    store = ctx.store

    def plan(today: dt.date | None = None):
        settings = store.settings()
        return demand_plan(store.open_demands(), store.delivered(),
                           {k: v["on_hand"] for k, v in store.stock().items()}, store.active_plates(),
                           fail_rate(store.run_totals(), settings), settings, today or dt.date.today())

    @app.get("/demand", response_class=HTMLResponse)
    async def demand_page(request: Request):
        user = ctx.user(request)
        done = [d for d in store.demands() if d["status"] == "done"]
        return ctx.page(request, "demand.html", user=user, nav="demand", plan=plan(), done=done[:20],
                        models=ctx.models_by_slug())

    @app.get("/demand/new", response_class=HTMLResponse)
    async def new_demand(request: Request):
        user = ctx.require(request, "admin")
        return ctx.page(request, "demand_form.html", user=user, nav="demand", d=None, needs={},
                        models=store.models())

    @app.get("/demand/{slug}", response_class=HTMLResponse)
    async def demand_detail(request: Request, slug: str):
        user = ctx.user(request)
        d = store.demand(slug)
        if d is None:
            raise HTTPException(404, "No such demand")
        p = next((x for x in plan() if x.demand.slug == slug), None)
        ledger = [r for r in store.ledger(500) if r["demand_slug"] == slug]
        active = store.plates(active_only=True)
        suggest = next((v[1] for v in (p.plates_by_model.values() if p else []) if v), None) or \
            (max(active, key=lambda x: x.units).code if active else None)
        return ctx.page(request, "demand_detail.html", user=user, nav="demand", d=d, p=p, suggest=suggest,
                        needs=store.demand_needs(slug), delivered=store.delivered().get(slug, {}), ledger=ledger,
                        models=ctx.models_by_slug(), plates=store.plates(active_only=True),
                        printers=list(store.settings().printers), today=dt.date.today().isoformat())

    @app.get("/demand/{slug}/edit", response_class=HTMLResponse)
    async def edit_demand(request: Request, slug: str):
        user = ctx.require(request, "admin")
        d = store.demand(slug)
        if d is None:
            raise HTTPException(404, "No such demand")
        return ctx.page(request, "demand_form.html", user=user, nav="demand", d=d, needs=store.demand_needs(slug),
                        models=store.models())

    async def save(request: Request, slug: str | None):
        user = ctx.require(request, "admin")
        form = await request.form()
        name = str(form.get("name", "")).strip()
        kind = str(form.get("kind", "event"))
        due = str(form.get("due", ""))
        if not name or kind not in KINDS:
            raise HTTPException(400, "Give it a name and a kind")
        try:
            dt.date.fromisoformat(due)
        except ValueError:
            raise HTTPException(400, "Due date must look like YYYY-MM-DD")
        target = to_int(form.get("target", "") or "0")
        if target is None:
            raise HTTPException(400, "Target is a whole number")
        needs = {}
        for m in store.models():
            raw = str(form.get(f"need_{m['slug']}", "")).strip()
            n = to_int(raw, 0)
            if raw and to_int(raw) is None:
                raise HTTPException(400, "Needs are whole numbers")
            if n > 0:
                needs[m["slug"]] = n
        if target and target < sum(needs.values()):
            raise HTTPException(400, f"The total ({target}) is less than the needs added up ({sum(needs.values())})")
        if not target and not needs:
            raise HTTPException(400, "Give a total or what's needed per model")
        creating = slug is None
        slug = slug or slugify(name)
        if not slug:
            raise HTTPException(400, "The name needs letters or numbers")
        if creating:
            if slug == "new":
                slug = "new-1"
            base, n = slug, 2
            while store.demand(slug):  # a second "Spring fair" gets its own record
                slug, n = f"{base}-{n}", n + 1
        is_new = creating
        store.save_demand(slug, name=name, kind=kind, due=due, target=target, notes=str(form.get("notes", "")).strip(),
                          needs=needs, by=user["username"])
        d = store.demand(slug)
        if d["bambuddy_project_id"]:
            try:
                await ctx.bambuddy.update_project(d["bambuddy_project_id"], project_body(slug))
            except BambuddyError:
                pass  # updated again on the next Queue
        return ctx.back(f"/demand/{slug}", "Added" if is_new else "Saved")

    @app.post("/demand")
    async def create_demand(request: Request):
        return await save(request, None)

    @app.post("/demand/{slug}/edit")
    async def update_demand(request: Request, slug: str):
        ctx.require(request, "admin")
        if store.demand(slug) is None:
            raise HTTPException(404, "No such demand")
        return await save(request, slug)

    @app.post("/demand/{slug}/status")
    async def set_status(request: Request, slug: str, status: str = Form(...)):
        ctx.require(request, "admin")
        if status not in ("open", "done") or store.demand(slug) is None:
            raise HTTPException(400, "Unknown")
        store.set_demand_status(slug, status)
        return ctx.back(f"/demand/{slug}", "Marked done" if status == "done" else "Reopened")

    def project_body(slug: str) -> dict:
        d = store.demand(slug)
        total = d["target"] or sum(store.demand_needs(slug).values())
        return {"name": d["name"], "target_parts_count": total, "due_date": f"{d['due']}T12:00:00",
                "notes": f"Platewright {d['kind']} {slug}"}

    @app.post("/demand/{slug}/queue")
    async def queue_plates(request: Request, slug: str, plate: str = Form(...), times: int = Form(..., ge=1, le=50),
                           printer: str = Form("")):
        """Create or update the Bambuddy project for the demand and queue N runs of a plate, each
        waiting for a manual start (clear the bed first) and asking for the good / reject verdict."""
        ctx.require(request, "admin")
        d = store.demand(slug)
        p = store.plate(plate.upper())
        if d is None or p is None or not p.active:
            raise HTTPException(400, "Unknown demand or plate")
        if not p.bambuddy_file_id:
            return ctx.back(f"/demand/{slug}", f"Plate {p.code} isn't in File Manager yet; upload it from its page", "bad")
        settings = store.settings()
        key = printer or next(iter(settings.printers), "")
        if settings.printers and key not in settings.printers:
            raise HTTPException(400, "Unknown printer")
        pc = settings.printers.get(key, {})
        if pc.get("model") and p.printer_model and pc["model"] != p.printer_model:
            return ctx.back(f"/demand/{slug}", f"Plate {p.code} was sliced for a {p.printer_model}, not a {pc['model']}", "bad")
        bb = ctx.bambuddy
        try:
            lib = await bb.library_file(p.bambuddy_file_id)
            if lib.get("file_hash") and lib["file_hash"] != p.sha256:
                return ctx.back(f"/demand/{slug}", f"File Manager's file {p.bambuddy_file_id} isn't the locked "
                                                   f"plate {p.code}; upload it again from the plate page", "bad")
            want = (pc.get("bambuddy_name") or key).lower()
            printers = await bb.printers()
            hit = [x for x in printers if (x.get("name") or "").lower() == want]
            if not hit and len(printers) == 1:
                hit = printers
            if not hit:
                return ctx.back(f"/demand/{slug}", f"No Bambuddy printer called '{want}'", "bad")
            project_id = d["bambuddy_project_id"]
            body = project_body(slug)
            if project_id:
                try:
                    await bb.update_project(project_id, body)
                except BambuddyError as exc:
                    if exc.status != 404:
                        raise
                    project_id = None
            if not project_id:
                project_id = int((await bb.create_project(body))["id"])
                store.set_demand_project(slug, project_id)
            res = await bb.add_to_queue({
                "library_file_id": p.bambuddy_file_id, "printer_id": hit[0]["id"], "quantity": times,
                "project_id": project_id, "confirm_outcome": True, "require_previous_success": True,
                "manual_start": True})
        except BambuddyError as exc:
            return ctx.back(f"/demand/{slug}", f"Bambuddy refused: {exc}", "bad")
        return ctx.back(f"/demand/{slug}", f"Queued plate {p.code} {times} time{'' if times == 1 else 's'} "
                                           f"({times * p.units} {settings.unit()}). Each run waits in the Print Queue "
                                           "until you clear the bed and press start.")
