"""Models: what may go on a production plate. Add, edit, test-print pass / fail."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from platewright.bambuddy.client import BambuddyError
from platewright.core.names import model_slug
from platewright.core.plates import license_warning
from platewright.web.context import Ctx, to_int

MODEL_EXTS = (".3mf", ".stl", ".step", ".stp", ".obj")


def _clean_url(url: str) -> str:
    url = url.strip()
    if url and urlsplit(url).scheme not in ("http", "https"):
        raise HTTPException(400, "The source link must start with http:// or https://")
    return url


def is_makerworld(url: str) -> bool:
    host = (urlsplit(url.strip()).hostname or "").lower()
    return host == "makerworld.com" or host.endswith(".makerworld.com")


def _int_or_none(text: str) -> int | None:
    return to_int(text)


def register(app: FastAPI, ctx: Ctx) -> None:
    store = ctx.store

    async def library_models() -> tuple[list[dict], str | None]:
        """Model source files in Bambuddy's File Manager, for linking a model to one. Bambuddy lists
        files per folder (``recursive`` only works with a folder), so this walks the top-level
        folders, leaving out the ones Platewright keeps plates in."""
        settings = store.settings()
        skip = {settings.library_folder, settings.retired_folder}
        try:
            tree = await ctx.bambuddy.get("/library/folders") or []
            names: dict[int, str] = {}

            def walk(nodes, path=""):
                for n in nodes:
                    names[n["id"]] = f"{path}{n['name']}"
                    walk(n.get("children") or [], f"{path}{n['name']} / ")
            walk([n for n in tree if not n.get("parent_id")])  # a flat list too: start at the top
            files = list(await ctx.bambuddy.get("/library/files") or [])  # files outside any folder
            for top in tree:
                if top.get("parent_id") or top.get("name") in skip:
                    continue
                files += await ctx.bambuddy.get("/library/files", folder_id=top["id"], recursive=True) or []
        except BambuddyError as exc:
            return [], str(exc)
        out, seen = [], set()
        for f in files:
            name = (f.get("filename") or "").lower()
            if f.get("id") in seen or not name.endswith(MODEL_EXTS) or name.endswith(".gcode.3mf"):
                continue
            seen.add(f["id"])
            out.append({**f, "folder_name": names.get(f.get("folder_id"), "")})
        return sorted(out, key=lambda f: (f["folder_name"].lower(), (f.get("filename") or "").lower()))[:300], None

    async def from_makerworld(fields: dict) -> dict:
        """Fill a MakerWorld model's blank designer, license and source file from the model page,
        through Bambuddy. What was typed is kept; if MakerWorld can't be asked, nothing changes."""
        if not is_makerworld(fields["url"]) or (fields["designer"] and fields["license"] and fields["bambuddy_file_id"]):
            return fields
        try:
            mw = await ctx.bambuddy.makerworld(fields["url"])
        except BambuddyError:
            return fields
        fields["designer"] = fields["designer"] or mw["designer"]
        fields["license"] = fields["license"] or mw["license"]
        if fields["bambuddy_file_id"] is None and mw["file_ids"]:
            fields["bambuddy_file_id"] = mw["file_ids"][0]
        return fields

    def form_fields(name, ref, url, designer, license, distribution_ok, min_age, small_parts, parts_per_unit,
                    bambuddy_file_id, notes) -> dict:
        if distribution_ok not in ("yes", "no", "unknown") or small_parts not in ("yes", "no", "unknown"):
            raise HTTPException(400, "Unknown choice")
        return {"name": name.strip(), "ref": ref.strip(), "url": _clean_url(url), "designer": designer.strip(),
                "license": license.strip(), "distribution_ok": distribution_ok, "min_age": _int_or_none(min_age),
                "small_parts": small_parts, "parts_per_unit": max(_int_or_none(parts_per_unit) or 1, 1),
                "bambuddy_file_id": _int_or_none(bambuddy_file_id), "notes": notes.strip()}

    @app.get("/models", response_class=HTMLResponse)
    async def models_page(request: Request):
        user = ctx.user(request)
        stock = store.stock()
        core = {m.slug: m for m in store.core_models()}
        on_plates: dict[str, list[str]] = {}
        for p in store.plates(active_only=True):
            for slug in p.mix:
                on_plates.setdefault(slug, []).append(p.code)
        return ctx.page(request, "models.html", user=user, nav="models", models=store.models(), stock=stock,
                        on_plates=on_plates, warnings={s: license_warning(m) for s, m in core.items()})

    @app.get("/models/new", response_class=HTMLResponse)
    async def new_model_page(request: Request):
        user = ctx.require(request, "admin")
        files, error = await library_models()
        return ctx.page(request, "model_form.html", user=user, nav="models", model=None, files=files,
                        files_error=error)

    @app.post("/makerworld/lookup", response_class=HTMLResponse)
    async def makerworld_lookup(request: Request, url: str = Form(""), designer: str = Form(""),
                                license: str = Form(""), bambuddy_file_id: str = Form("")):
        """The model form asks this when the source link changes: a note under the link, and the
        blank designer, license and source file filled in (out-of-band swaps)."""
        user = ctx.require(request, "admin")
        url = url.strip()
        context = {"mw": None, "error": None, "designer": None, "license": None, "file_id": None, "files": []}
        if url and is_makerworld(url) and urlsplit(url).scheme in ("http", "https"):
            try:
                mw = await ctx.bambuddy.makerworld(url)
            except BambuddyError as exc:
                context["error"] = str(exc)
            else:
                context["mw"] = mw
                if not designer.strip() and mw["designer"]:
                    context["designer"] = mw["designer"]
                if not license.strip() and mw["license"]:
                    context["license"] = mw["license"]
                if not to_int(bambuddy_file_id) and mw["file_ids"]:
                    files, _ = await library_models()
                    if any(f["id"] == mw["file_ids"][0] for f in files):
                        context.update(file_id=mw["file_ids"][0], files=files)
        return ctx.page(request, "_makerworld.html", user=user, **context)

    @app.get("/models/{slug}", response_class=HTMLResponse)
    async def model_page(request: Request, slug: str):
        user = ctx.user(request)
        m = store.model(slug)
        if m is None:
            raise HTTPException(404, "No such model")
        core = next(c for c in store.core_models() if c.slug == slug)
        plates = [p for p in store.plates() if slug in p.mix]
        return ctx.page(request, "model.html", user=user, nav="models", model=m, warning=license_warning(core),
                        tests=store.qualifications(slug), plates=plates, stock=store.stock().get(slug, {}))

    @app.get("/models/{slug}/edit", response_class=HTMLResponse)
    async def edit_model_page(request: Request, slug: str):
        user = ctx.require(request, "admin")
        m = store.model(slug)
        if m is None:
            raise HTTPException(404, "No such model")
        files, error = await library_models()
        return ctx.page(request, "model_form.html", user=user, nav="models", model=m, files=files,
                        files_error=error)

    @app.post("/models")
    async def create_model(request: Request, name: str = Form(...), ref: str = Form(""), url: str = Form(""),
                           designer: str = Form(""), license: str = Form(""), distribution_ok: str = Form("unknown"),
                           min_age: str = Form(""), small_parts: str = Form("unknown"),
                           parts_per_unit: str = Form("1"), aliases: str = Form(""),
                           bambuddy_file_id: str = Form(""), notes: str = Form("")):
        user = ctx.require(request, "admin")
        try:
            slug = model_slug(name, ref.strip() or None)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if store.model(slug):
            return ctx.back(f"/models/{slug}", f"{name} is already registered")
        fields = await from_makerworld(form_fields(name, ref, url, designer, license, distribution_ok, min_age,
                                                   small_parts, parts_per_unit, bambuddy_file_id, notes))
        name = fields.pop("name")
        store.add_model(slug, name, by=user["username"], aliases=tuple(dict.fromkeys(a.strip() for a in aliases.splitlines() if a.strip())),
                        **fields)
        return ctx.back(f"/models/{slug}", f"{name} added. Test-print it before it goes on a production plate.")

    @app.post("/models/{slug}")
    async def update_model(request: Request, slug: str, name: str = Form(...), ref: str = Form(""),
                           url: str = Form(""), designer: str = Form(""), license: str = Form(""),
                           distribution_ok: str = Form("unknown"), min_age: str = Form(""),
                           small_parts: str = Form("unknown"), parts_per_unit: str = Form("1"),
                           aliases: str = Form(""), bambuddy_file_id: str = Form(""), notes: str = Form("")):
        ctx.require(request, "admin")
        if store.model(slug) is None:
            raise HTTPException(404, "No such model")
        fields = await from_makerworld(form_fields(name, ref, url, designer, license, distribution_ok, min_age,
                                                   small_parts, parts_per_unit, bambuddy_file_id, notes))
        store.update_model(slug, aliases=aliases.splitlines(), **fields)
        return ctx.back(f"/models/{slug}", "Saved")

    @app.post("/models/{slug}/test")
    async def test_print(request: Request, slug: str, result: str = Form(...), notes: str = Form("")):
        user = ctx.require(request, "admin")
        if result not in ("pass", "fail"):
            raise HTTPException(400, "pass or fail")
        if store.model(slug) is None:
            raise HTTPException(404, "No such model")
        store.qualify(slug, result, notes.strip(), user["username"])
        return ctx.back(f"/models/{slug}", "Test print passed" if result == "pass" else "Test print failed")

    @app.get("/thumb/{file_id}")
    async def thumbnail(request: Request, file_id: int):
        """Bambuddy's thumbnail of a library file, through the API key (the browser has none)."""
        ctx.user(request)
        known = {r["bambuddy_file_id"] for r in store.models() if r["bambuddy_file_id"]}
        known |= {p.bambuddy_file_id for p in store.plates() if p.bambuddy_file_id}
        if file_id not in known:
            return Response(status_code=404)
        try:
            r = await ctx.bambuddy._http.get(f"/library/files/{file_id}/thumbnail",
                                             headers={"X-API-Key": ctx.cfg.bambuddy_key})
        except Exception:
            return Response(status_code=502)
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
            return Response(status_code=404)
        return Response(r.content, media_type=r.headers["content-type"],
                        headers={"Cache-Control": "private, max-age=3600"})
