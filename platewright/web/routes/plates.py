"""Plates: locked production files. New plate (upload, checks, lock), retire, log a run by hand."""

from __future__ import annotations

import datetime as dt

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse

from platewright.core.mix import ANY
from platewright.core.threemf import ThreeMFError
from platewright.plates import CODE_RE, MAX_UPLOAD, PlateFiles
from platewright.web.context import Ctx, to_int


def register(app: FastAPI, ctx: Ctx, files: PlateFiles) -> None:
    store = ctx.store

    @app.get("/plates", response_class=HTMLResponse)
    async def plates_page(request: Request):
        user = ctx.user(request)
        return ctx.page(request, "plates.html", user=user, nav="plates", plates=store.plates(),
                        models=ctx.models_by_slug())

    @app.get("/plates/new", response_class=HTMLResponse)
    async def new_plate_page(request: Request):
        user = ctx.require(request, "admin")
        return ctx.page(request, "plate_new.html", user=user, nav="plates", error=None, details=[])

    @app.post("/plates/new", response_class=HTMLResponse)
    async def upload_plate(request: Request, file: UploadFile = File(...)):
        user = ctx.require(request, "admin")
        data = await file.read(MAX_UPLOAD + 1)
        try:
            sha = files.stage(data, file.filename or "plate.gcode.3mf")
            files.staged(sha)  # parse now, so a project file is explained right away
        except ThreeMFError as exc:
            return ctx.page(request, "plate_new.html", 400, user=user, nav="plates", error=str(exc),
                            details=exc.details)
        return ctx.back(f"/plates/new/{sha}")

    def check_page(request: Request, user: dict, sha: str, code: str = "", description: str = "",
                   units: int | None = None, allow_untested: bool = False, allow_unmapped: bool = False,
                   status: int = 200, error: str | None = None):
        try:
            st = files.staged(sha, units=units, allow_untested=allow_untested, allow_unmapped=allow_unmapped)
        except ThreeMFError as exc:
            return ctx.page(request, "plate_new.html", 400, user=user, nav="plates", error=str(exc),
                            details=exc.details)
        existing = store.plate(code.upper()) if code else None
        same_file = next((p for p in store.plates() if p.sha256 == sha), None)
        return ctx.page(request, "plate_check.html", status, user=user, nav="plates", st=st, code=code,
                        description=description, units=units, allow_untested=allow_untested,
                        allow_unmapped=allow_unmapped, existing=existing, same_file=same_file, error=error,
                        models=ctx.models_by_slug(), plates=store.plates())

    @app.get("/plates/new/{sha}", response_class=HTMLResponse)
    async def staged_page(request: Request, sha: str, code: str = "", description: str = "", units: str = "",
                          allow_untested: str = "", allow_unmapped: str = ""):
        user = ctx.require(request, "admin")
        return check_page(request, user, sha, code, description, to_int(units),
                          bool(allow_untested), bool(allow_unmapped))

    @app.post("/plates/new/{sha}/alias")
    async def link_alias(request: Request, sha: str, name: str = Form(...), model: str = Form(...)):
        ctx.require(request, "admin")
        if not store.model(model):
            raise HTTPException(400, "Unknown model")
        store.add_alias(model, name)
        return ctx.back(f"/plates/new/{sha}", f"'{name}' now counts as {store.model(model)['name']}")

    @app.post("/plates/new/{sha}/lock", response_class=HTMLResponse)
    async def lock_plate(request: Request, sha: str, code: str = Form(...), description: str = Form(""),
                         units: str = Form(""), allow_untested: str = Form(""), allow_unmapped: str = Form("")):
        user = ctx.require(request, "admin")
        n = to_int(units)
        try:
            st = files.staged(sha, units=n, allow_untested=bool(allow_untested), allow_unmapped=bool(allow_unmapped))
        except ThreeMFError as exc:
            raise HTTPException(400, str(exc))
        code = code.strip().upper()
        if not CODE_RE.match(code):
            return check_page(request, user, sha, code, description, n, bool(allow_untested), bool(allow_unmapped),
                              400, "A plate code is 1 to 8 letters or digits, e.g. N or H1.")
        if not st.check.ok:
            return check_page(request, user, sha, code, description, n, bool(allow_untested), bool(allow_unmapped),
                              400, "Fix the problems first.")
        _, problem = await files.lock(st, code, description=description.strip() or None, by=user["username"])
        p = store.plate(code)
        msg = f"Plate {code} v{p.version} locked"
        msg += f", but not in File Manager yet: {problem}" if problem else " and in File Manager"
        return ctx.back(f"/plates/{code}", msg, "warn" if problem else "")

    @app.get("/plates/{code}", response_class=HTMLResponse)
    async def plate_page(request: Request, code: str):
        user = ctx.user(request)
        versions = store.plate_versions(code.upper())
        if not versions:
            raise HTTPException(404, "No such plate")
        runs = store.runs(plate_code=versions[0].code, limit=20)
        return ctx.page(request, "plate.html", user=user, nav="plates", versions=versions, runs=runs,
                        models=ctx.models_by_slug(), today=dt.date.today().isoformat())

    @app.post("/plates/{code}/retire")
    async def retire_plate(request: Request, code: str):
        ctx.require(request, "admin")
        code = code.upper()
        if not store.plate(code):
            raise HTTPException(404, "No such plate")
        problem = await files.retire(code)
        return ctx.back(f"/plates/{code}", f"Plate {code} retired" + (f"; {problem}" if problem else ""),
                        "warn" if problem else "")

    @app.post("/plates/{code}/publish")
    async def publish_plate(request: Request, code: str):
        ctx.require(request, "admin")
        if not store.plate(code.upper()):
            raise HTTPException(404, "No such plate")
        problem = await files.publish(code.upper())
        return ctx.back(f"/plates/{code.upper()}", problem or "Uploaded to File Manager", "bad" if problem else "")

    @app.post("/plates/{code}/runs")
    async def log_run(request: Request, code: str, date: str = Form(...), outcome: str = Form("ok"),
                      bad: str = Form("0"), aborted_pct: str = Form(""), notes: str = Form("")):
        """A run Bambuddy didn't see (printed from the SD card or the slicer). Sync links it to the
        same print if Bambuddy reports it after all."""
        user = ctx.require(request, "operator")
        p = store.plate(code.upper())
        if not p or not p.active:
            raise HTTPException(400, "Only active plates are printed")
        try:
            day = dt.date.fromisoformat(date)
        except ValueError:
            raise HTTPException(400, "Date must look like YYYY-MM-DD")
        if day > dt.date.today():
            raise HTTPException(400, "That date is in the future")
        if outcome not in ("ok", "aborted", "failed"):
            raise HTTPException(400, "Unknown outcome")
        bad_n = to_int(bad, 0)
        if bad_n > p.units:
            raise HTTPException(400, f"Plate {p.code} only has {p.units}")
        pct = to_int(aborted_pct)
        if outcome == "aborted" and (pct is None or not 1 <= pct <= 99):
            raise HTTPException(400, "For a run that stopped early, give how far it got: 1 to 99 %")
        printer = next(iter(store.settings().printers), "")
        store.add_manual_run(p.version_id, day.isoformat(), printer, outcome, aborted_pct=pct,
                             failures={ANY: bad_n} if bad_n and outcome == "ok" else None, notes=notes.strip(),
                             by=user["username"])
        return ctx.back(f"/plates/{p.code}", "Run logged")
