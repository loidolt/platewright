"""Admin commands, run inside the container: ``docker exec platewright platewright <command>``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from platewright import config
from platewright.core.plates import check_plate
from platewright.core.settings import Settings, from_toys_config
from platewright.core.threemf import ThreeMFError, read_sliced
from platewright.store.db import Store


def cmd_import_config(a, store: Store) -> int:
    """Settings from a toys.py config.json (or a Platewright settings JSON)."""
    data = json.loads(Path(a.file).read_text())
    values = from_toys_config(data) if "monthly_target" in data or "_palette_help" in data else data
    problems = Settings.from_values(values).problems()
    if problems and not a.force:
        print("Not imported:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    if store.settings_values() and not a.replace:
        print("Settings already exist; pass --replace to overwrite the keys in this file.", file=sys.stderr)
        return 1
    store.set_settings(values)
    print(f"Imported {', '.join(sorted(values))}")
    return 0


def cmd_sync(a, store: Store) -> int:
    from platewright.bambuddy.client import Bambuddy
    from platewright.bambuddy.sync import sync_once

    async def run():
        bb = Bambuddy(config.load())
        try:
            return await sync_once(bb, store)
        finally:
            await bb.aclose()

    s = asyncio.run(run())
    if s["error"]:
        print(f"Sync failed: {s['error']}", file=sys.stderr)
        return 1
    print(f"{s['new']} new, {s['updated']} updated, {s['linked']} linked; "
          f"{sum(s['other'].values())} prints aren't production plates")
    return 0


def cmd_check_plate(a, store: Store) -> int:
    """Run the plate checks on a sliced file without locking anything."""
    settings = store.settings()
    try:
        with open(a.file, "rb") as f:
            sliced = read_sliced(f, Path(a.file).name, settings.palette)
    except ThreeMFError as exc:
        print(exc, *exc.details, sep="\n  ", file=sys.stderr)
        return 1
    c = check_plate(sliced, store.core_models(), settings, units=a.units)
    print(f"{c.units} {settings.unit(c.units)}, {c.hours} h, {c.grams} g, {c.color_changes} color changes, "
          f"sliced for {sliced.printer_model or sliced.printer_model_id or 'unknown printer'}")
    print(f"  by model: {c.model_units}")
    print(f"  filament: {c.filaments}")
    for w in c.warnings:
        print(f"  warning: {w}")
    for b in c.blockers:
        print(f"  BLOCKED: {b}")
    print("Passes the checks." if c.ok else "Would not be locked.")
    return 0 if c.ok else 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="platewright", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("import-config", help="import settings from a toys.py config.json or settings JSON")
    s.add_argument("file")
    s.add_argument("--replace", action="store_true", help="overwrite settings that already exist")
    s.add_argument("--force", action="store_true", help="import even if the palette or printers have problems")
    s.set_defaults(fn=cmd_import_config)
    s = sub.add_parser("sync", help="sync finished prints from Bambuddy now")
    s.set_defaults(fn=cmd_sync)
    s = sub.add_parser("check-plate", help="run the plate checks on a sliced .gcode.3mf (locks nothing)")
    s.add_argument("file")
    s.add_argument("--units", type=int, help="override the part count")
    s.set_defaults(fn=cmd_check_plate)
    a = ap.parse_args(argv)
    store = Store(config.load().db_path)
    try:
        return a.fn(a, store)
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
