"""Settings (admins): wording, targets, printers, palette; and the Bambuddy connection."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from platewright.core.palette import norm_hex
from platewright.core.settings import Settings
from platewright.web.context import Ctx

SCALARS = {
    "unit_name": str, "period_target": int, "max_plate_hours": float, "assumed_fail_rate": float,
    "reorder_days": float, "operator_group": str, "library_folder": str, "retired_folder": str,
    "summary_weekday": int, "summary_hour": int,
}


BACKUP_MAX_AGE = dt.timedelta(hours=36)


def backup_status(data_dir: str) -> dict | None:
    """What the nightly backup (outside the container) last wrote, if anything."""
    try:
        status = json.loads((Path(data_dir) / "backup-status.json").read_text())
    except (OSError, ValueError):
        return None
    try:
        at = dt.datetime.fromisoformat(status["at"].replace("Z", "+00:00"))
        status["fresh"] = bool(status.get("ok")) and dt.datetime.now(dt.timezone.utc) - at < BACKUP_MAX_AGE
    except (KeyError, ValueError):
        status["fresh"] = False
    return status


def register(app: FastAPI, ctx: Ctx) -> None:
    store = ctx.store

    def status_extras() -> dict:
        return {"messages_state": store.state("messages"), "webhook": store.state("webhook"),
                "webhook_set_up": bool(ctx.cfg.webhook_secret)}

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        user = ctx.require(request, "admin")
        return ctx.page(request, "settings.html", user=user, nav="settings", s=store.settings(), errors=[],
                        last_sync=store.state("last_sync"), badge=ctx.extra["badge"].state,
                            backup=backup_status(ctx.cfg.data_dir), **status_extras())

    @app.post("/settings", response_class=HTMLResponse)
    async def save_settings(request: Request):
        user = ctx.require(request, "admin")
        form = await request.form()
        values: dict = {}
        errors: list[str] = []
        for key, kind in SCALARS.items():
            if key not in form:  # not on this form: keep what is saved
                continue
            raw = str(form.get(key, "")).strip()
            try:
                values[key] = kind(raw) if kind is not str else raw
            except ValueError:
                errors.append(f"{key.replace('_', ' ')}: '{raw}' isn't a number")
        if "summary_hour" in form:  # the Messages section is on the form
            values["messages"] = bool(form.get("messages"))
        if "assumed_fail_rate" in values:
            values["assumed_fail_rate"] = values["assumed_fail_rate"] / 100  # entered as percent
        printers = {}
        for key, model, model_id, per_day, bb_name in zip(form.getlist("printer_key"), form.getlist("printer_model"),
                                                           form.getlist("printer_model_id"), form.getlist("printer_per_day"),
                                                           form.getlist("printer_bambuddy_name")):
            key = str(key).strip()
            if not key:
                continue
            try:
                per = float(str(per_day).strip() or 1)
            except ValueError:
                errors.append(f"printer {key}: plates per day isn't a number")
                continue
            entry = {"model": str(model).strip(), "plates_per_day": per}
            if str(model_id).strip():
                entry["model_id"] = str(model_id).strip()
            if str(bb_name).strip():
                entry["bambuddy_name"] = str(bb_name).strip()
            printers[key] = entry
        palette = {}
        for name, hx, material, slot, backup in zip(form.getlist("color_name"), form.getlist("color_hex"),
                                                    form.getlist("color_material"), form.getlist("color_slot"),
                                                    form.getlist("color_backup")):
            name = str(name).strip()
            if not name:
                continue
            palette[name] = {"hex": norm_hex(str(hx)) or str(hx).strip(), "material": str(material).strip().upper(),
                             "slot": str(slot).strip().upper(), "backup_slot": str(backup).strip().upper()}
        values["printers"] = printers
        values["palette"] = palette
        if not values.get("unit_name"):
            errors.append("unit name can't be empty")
        if not errors:
            errors = Settings.from_values({**store.settings_values(), **values}).problems()
        if errors:
            draft = Settings.from_values({**store.settings_values(), **{k: v for k, v in values.items()}})
            return ctx.page(request, "settings.html", 400, user=user, nav="settings", s=draft, errors=errors,
                            last_sync=store.state("last_sync"), badge=ctx.extra["badge"].state,
                            backup=backup_status(ctx.cfg.data_dir), **status_extras())
        store.set_settings(values)
        return ctx.back("/settings", "Saved")

    @app.get("/settings/export")
    async def export_settings(request: Request):
        ctx.require(request, "admin")
        return JSONResponse(store.settings_values(),
                            headers={"Content-Disposition": "attachment; filename=platewright-settings.json"})

    @app.get("/health/backup")
    async def backup_health():
        """For monitoring: 200 while the last backup succeeded less than 36 hours ago."""
        status = backup_status(ctx.cfg.data_dir)
        ok = bool(status and status.get("fresh"))
        return JSONResponse({"status": "ok" if ok else "stale", "last": status}, status_code=200 if ok else 503)

    @app.post("/settings/test-message")
    async def test_message(request: Request):
        ctx.require(request, "admin")
        from platewright.bambuddy.client import BambuddyError

        try:
            n = await ctx.extra["messenger"].send("Platewright test", "If you read this on your phone, app messages "
                                                  "reach you through Bambuddy.", "/settings")
        except BambuddyError as exc:
            return ctx.back("/settings", f"Bambuddy refused: {exc}", "bad")
        if not n:
            return ctx.back("/settings", "Bambuddy has no channel with \"Messages from connected apps\" on", "warn")
        return ctx.back("/settings", f"Sent to {n} channel{'' if n == 1 else 's'}")

