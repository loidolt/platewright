"""Daily work: review finished prints (bad parts), stock going out and adjustments."""

from __future__ import annotations

import datetime as dt

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from platewright.core.mix import ANY, UNLINKED
from platewright.core.stock import NotEnough, allocate_out
from platewright.web.context import Ctx


def _day(text: str) -> str:
    try:
        day = dt.date.fromisoformat(text) if text else dt.date.today()
    except ValueError:
        raise HTTPException(400, "Date must look like YYYY-MM-DD")
    if day > dt.date.today():
        raise HTTPException(400, "That date is in the future")
    return day.isoformat()


def register(app: FastAPI, ctx: Ctx) -> None:
    store = ctx.store

    # -- review -------------------------------------------------------------------------------

    @app.get("/runs/{run_id}/review", response_class=HTMLResponse)
    async def review_form(request: Request, run_id: int):
        user = ctx.require(request, "operator")
        run = store.run(run_id)
        if run is None:
            raise HTTPException(404, "No such run")
        return ctx.page(request, "_review.html", user=user, run=run, models=ctx.models_by_slug(), editing=True)

    @app.post("/runs/{run_id}/review", response_class=HTMLResponse)
    async def review(request: Request, run_id: int):
        """All good (no fields), or bad parts per model: fields bad_<slug> and bad_any."""
        user = ctx.require(request, "operator")
        run = store.run(run_id)
        if run is None:
            raise HTTPException(404, "No such run")
        form = await request.form()
        failures: dict[str, int] = {}
        for key in list(run.planned_by_model) + [ANY]:
            raw = str(form.get(f"bad_{key}", "0")).strip() or "0"
            if not raw.isdigit():
                raise HTTPException(400, "Bad parts are whole numbers")
            failures[key] = int(raw)
        for key, n in failures.items():
            if key != ANY and n > run.planned_by_model.get(key, 0):
                raise HTTPException(400, f"The plate only has {run.planned_by_model[key]} of those")
        if sum(failures.values()) > run.planned:
            raise HTTPException(400, f"The plate only has {run.planned}")
        store.review_run(run_id, failures, user["username"])
        run = store.run(run_id)
        if request.headers.get("HX-Request"):
            return ctx.page(request, "_review.html", user=user, run=run, models=ctx.models_by_slug(), editing=False)
        return ctx.back("/", f"Plate {run.plate_code}: {run.good} good")

    # -- stock ----------------------------------------------------------------------------------

    @app.get("/stock", response_class=HTMLResponse)
    async def stock_page(request: Request):
        user = ctx.user(request)
        stock = store.stock()
        models = ctx.models_by_slug()
        keys = sorted(set(stock) | set(models), key=lambda k: (k == UNLINKED, (models.get(k) or {"name": k})["name"].lower()))
        rows = [(k, stock.get(k, {"made": 0, "adjusted": 0, "out": 0, "on_hand": 0})) for k in keys]
        rows = [(k, s) for k, s in rows if any(s.values()) or k in models]
        total = {f: sum(s[f] for _, s in rows) for f in ("made", "adjusted", "out", "on_hand")}
        return ctx.page(request, "stock.html", user=user, nav="stock", rows=rows, total=total, models=models,
                        ledger=store.ledger(30), demands=[d for d in store.demands() if d["status"] == "open"],
                        today=dt.date.today().isoformat())

    @app.post("/stock/out")
    async def stock_out(request: Request, qty: int = Form(..., ge=1, le=100000), model: str = Form(""),
                        demand: str = Form(""), date: str = Form(""), notes: str = Form(""), force: str = Form("")):
        user = ctx.require(request, "operator")
        if model and not store.model(model):
            raise HTTPException(400, "Unknown model")
        if demand and not store.demand(demand):
            raise HTTPException(400, "Unknown demand")
        on_hand = {k: v["on_hand"] for k, v in store.stock().items()}
        try:
            alloc = allocate_out(qty, on_hand, model or None, force=bool(force))
        except NotEnough as exc:
            return ctx.back("/stock", f"Not recorded: {exc}. Add earlier stock with Adjust first, or tick "
                                      "'more than the records show'.")
        day = _day(date)
        note = notes.strip() if model else ("mixed; " + notes.strip()).rstrip("; ")
        for k, n in alloc.items():
            store.add_ledger(day, "out", k, n, demand_slug=demand or None, notes=note, by=user["username"])
        models = ctx.models_by_slug()
        mix = ", ".join(f"{(models.get(k) or {'name': 'unlinked'})['name']} ×{n}" for k, n in alloc.items())
        return ctx.back(f"/demand/{demand}" if demand else "/stock", f"{qty} out: {mix}")

    @app.post("/stock/adjust")
    async def stock_adjust(request: Request, model: str = Form(...), qty: int = Form(...), date: str = Form(""),
                           notes: str = Form(...)):
        user = ctx.require(request, "admin")
        if qty == 0:
            raise HTTPException(400, "The adjustment can't be 0")
        if not store.model(model):
            raise HTTPException(400, "Unknown model")
        if not notes.strip():
            raise HTTPException(400, "Say why")
        store.add_ledger(_day(date), "adjust", model, qty, notes=notes.strip(), by=user["username"])
        return ctx.back("/stock", f"Adjusted {store.model(model)['name']} by {qty:+d}")
