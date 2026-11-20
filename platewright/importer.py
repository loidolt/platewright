"""Import a toys.py folder: config.json and the CSVs in 01_models, 02_plates and 03_runs.

    platewright import-csv /data/import/3d-printing [--dry-run] [--replace-settings]

Everything goes in one transaction. Rows already in Platewright (same model slug, plate code
and version, Bambuddy print id, event id) are skipped, so a second run imports nothing twice.
Locked plate files are found by fingerprint in 02_plates/ or 04_archive/ and copied under the
data directory. Runs from toys.py count as reviewed: their bad toys were recorded there.
"""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
from pathlib import Path

from platewright.core.mix import ANY, UNLINKED, fit_mix
from platewright.core.settings import Settings, from_toys_config
from platewright.store.db import Store, now


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        return [{k: (v or "") for k, v in r.items() if k is not None} for r in csv.DictReader(f)]


def _json(text) -> dict:
    if isinstance(text, dict):
        return text
    try:
        return json.loads(text) if text else {}
    except json.JSONDecodeError:
        return {}


def _num(text, default: float = 0.0) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _choice(text: str, allowed: tuple[str, ...], default: str) -> str:
    return text if text in allowed else default


class ImportReport(dict):
    def line(self) -> str:
        return ", ".join(f"{k}: {v}" for k, v in self.items() if k != "notes")


def import_folder(root: str | Path, store: Store, data_dir: str | Path, *, dry_run: bool = False,
                  replace_settings: bool = False) -> ImportReport:
    root, data_dir = Path(root), Path(data_dir)
    rep = ImportReport(settings=0, models=0, test_prints=0, plates=0, versions=0, plate_files=0, runs=0,
                       demands=0, stock_moves=0, skipped=0, notes=[])
    copies: list[tuple[Path, Path]] = []

    settings_values: dict = {}
    cfg_path = root / "config.json"
    if cfg_path.exists():
        values = from_toys_config(json.loads(cfg_path.read_text()))
        problems = Settings.from_values(values).problems()
        if problems:
            rep["notes"].append("config.json not imported: " + "; ".join(problems))
        elif store.settings_values() and not replace_settings:
            rep["notes"].append("settings already exist; config.json skipped (--replace-settings to overwrite)")
        else:
            settings_values = values
            rep["settings"] = len(values)

    # Plate files by fingerprint, wherever toys.py kept them.
    files: dict[str, Path] = {}
    for d in ("02_plates", "04_archive"):
        for f in sorted((root / d).glob("*.gcode.3mf")) if (root / d).exists() else []:
            files[hashlib.sha256(f.read_bytes()).hexdigest()] = f

    with store.tx() as db:
        db.execute("SAVEPOINT import")
        stamp = now()
        for k, v in settings_values.items():
            db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                       (k, json.dumps(v)))
        # CSV rows already imported, by content and how often each occurs (two identical runs on one
        # day are two runs). Rows appended to toys.py's CSVs later still come in.
        done_rows = _json((db.execute("SELECT value FROM sync_state WHERE key = 'toys_import'").fetchone() or ["{}"])[0])
        seen = {"runs": dict(done_rows.get("runs", {})), "ledger": dict(done_rows.get("ledger", {}))}
        counted: dict[str, dict[str, int]] = {"runs": {}, "ledger": {}}

        def first_time(table: str, row: dict) -> bool:
            h = hashlib.sha1(json.dumps(row, sort_keys=True).encode()).hexdigest()[:16]
            counted[table][h] = counted[table].get(h, 0) + 1
            if counted[table][h] <= seen[table].get(h, 0):
                return False
            seen[table][h] = counted[table][h]
            return True

        model_ids = {r["slug"]: r["id"] for r in db.execute("SELECT id, slug FROM models")}
        ppu: dict[str, int] = {}
        for r in _rows(root / "01_models" / "models.csv"):
            slug = r["slug"]
            ppu[slug] = max(int(_num(r.get("parts_per_unit"), 1)), 1)
            if not slug or slug in model_ids:
                rep["skipped"] += 1
                continue
            min_age = int(_num(r["min_age"], -1)) if r.get("min_age") else None
            cur = db.execute(
                "INSERT INTO models (slug, name, ref, url, designer, license, distribution_ok, min_age, small_parts, "
                "parts_per_unit, status, notes, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (slug, r.get("name") or slug, r.get("ref", ""), r.get("url", ""), r.get("designer", ""),
                 r.get("license", ""), _choice(r.get("giveaway_ok", ""), ("yes", "no", "unknown"), "unknown"),
                 min_age if min_age is None or min_age >= 0 else None,
                 _choice(r.get("small_parts", ""), ("yes", "no"), "unknown"), ppu[slug],
                 _choice(r.get("status", ""), ("new", "qualified", "failed-test"), "new"), r.get("notes", ""),
                 (r.get("added") or stamp[:10]), "toys.py"))
            model_ids[slug] = cur.lastrowid
            for a in dict.fromkeys(a.strip() for a in (r.get("aliases") or "").split(";") if a.strip()):
                db.execute("INSERT OR IGNORE INTO model_aliases (model_id, alias) VALUES (?, ?)", (cur.lastrowid, a))
            if r.get("status") in ("qualified", "failed-test") and r.get("qualified"):
                db.execute("INSERT INTO qualifications (model_id, result, notes, at, by) VALUES (?, ?, ?, ?, ?)",
                           (cur.lastrowid, "pass" if r["status"] == "qualified" else "fail",
                            "imported from toys.py", r["qualified"], "toys.py"))
                rep["test_prints"] += 1
            rep["models"] += 1

        def mix_of(snap: dict) -> dict[str, int]:
            mu = {k: int(_num(v)) for k, v in _json(snap.get("model_units")).items()}
            if not mu:  # plates registered before toys.py kept model_units
                mu = {k: int(_num(n)) // ppu.get(k, 1) for k, n in _json(snap.get("models")).items()}
                unl = sum(int(_num(v)) for v in _json(snap.get("unmatched")).values())
                if unl:
                    mu[UNLINKED] = unl
            return fit_mix(mu, int(_num(snap.get("units"))))

        versions: dict[tuple[str, int], tuple[int, dict]] = {}
        for p in _rows(root / "02_plates" / "plates.csv"):
            code = (p.get("plate_id") or "").upper()
            if not code:
                continue
            row = db.execute("SELECT p.id, v.version FROM plates p LEFT JOIN plate_versions v "
                             "ON v.id = p.current_version_id WHERE p.code = ?", (code,)).fetchone()
            if row is None:
                pid = db.execute("INSERT INTO plates (code, description, active) VALUES (?, ?, ?)",
                                 (code, p.get("description", ""), int(p.get("active") == "yes"))).lastrowid
                rep["plates"] += 1
                current_here = 0
            else:
                pid, current_here = row["id"], row["version"] or 0
            current_v = int(_num(p.get("version"), 1))
            snaps = {int(_num(v, 1)): s for v, s in _json(p.get("history")).items()}
            snaps[current_v] = p
            current_id = None
            for v in sorted(snaps):
                s = snaps[v]
                existing = db.execute("SELECT id, file_path FROM plate_versions WHERE plate_id = ? AND version = ?",
                                      (pid, v)).fetchone()
                mix = mix_of(s)
                sha = s.get("sha256", "")
                if existing:
                    versions[(code, v)] = (existing["id"], mix)
                    if existing["file_path"] and sha in files and not (data_dir / existing["file_path"]).exists():
                        copies.append((files[sha], data_dir / existing["file_path"]))  # a copy lost last time
                    rep["skipped"] += 1
                    continue
                file_path = ""
                if sha in files:
                    rel = f"plates/PLATE-{code}_v{v}_{int(_num(s.get('units')))}up.gcode.3mf"
                    copies.append((files[sha], data_dir / rel))
                    file_path = rel
                    rep["plate_files"] += 1
                objects = int(_num(s.get("objects"))) or (sum(int(_num(n)) for n in _json(s.get("models")).values())
                                                           + sum(int(_num(n)) for n in _json(s.get("unmatched")).values()))
                same = v == current_v
                vid = db.execute(
                    "INSERT INTO plate_versions (plate_id, version, sha256, bambuddy_file_id, file_path, units, objects, "
                    "hours, grams, color_changes, filaments, printer_model, printer_model_id, nozzle, warnings, "
                    "registered_at, registered_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, 'toys.py')",
                    (pid, v, sha, int(p["bambuddy_file_id"]) if same and p.get("bambuddy_file_id", "").isdigit() else None,
                     file_path, int(_num(s.get("units"))), objects, _num(s.get("hours")), _num(s.get("grams")),
                     int(_num(p.get("color_changes"))) if same else 0, json.dumps(_json(s.get("filaments"))),
                     p.get("printer_model", ""), p.get("printer_model_id", ""), p.get("nozzle", ""),
                     p.get("registered") or stamp[:10])).lastrowid
                for slug, n in mix.items():
                    db.execute("INSERT INTO plate_mix (plate_version_id, model_id, units, objects) VALUES (?, ?, ?, ?)",
                               (vid, model_ids.get(slug), n, n * ppu.get(slug, 1)))
                versions[(code, v)] = (vid, mix)
                current_id = vid if same else current_id
                rep["versions"] += 1
            if current_id and current_v > current_here:  # never back to an older version
                db.execute("UPDATE plates SET current_version_id = ? WHERE id = ?", (current_id, pid))

        for r in _rows(root / "03_runs" / "runs.csv"):
            key = ((r.get("plate_id") or "").upper(), int(_num(r.get("plate_version"), 1)))
            if key not in versions:
                rep["notes"].append(f"run of {key[0]} v{key[1]} on {r.get('date')} skipped: unknown plate version")
                rep["skipped"] += 1
                continue
            vid, mix = versions[key]
            bb = r.get("bambuddy_run") or None
            if (bb and db.execute("SELECT 1 FROM runs WHERE bambuddy_run_id = ?", (bb,)).fetchone()) \
                    or not first_time("runs", r):
                rep["skipped"] += 1
                continue
            outcome = _choice(r.get("outcome") or "ok", ("ok", "rejected", "failed", "stopped", "cancelled", "aborted"), "ok")
            good = {k: int(_num(v)) for k, v in _json(r.get("models_good")).items()}
            pct = int(_num(r["aborted_pct"])) if r.get("aborted_pct") else None
            if outcome == "aborted" and sum(good.values()) > 0:
                outcome = "ok"  # stopped early, but some came out good (toys.py log --aborted-pct --good)
            failures: dict[str, int] = {}
            if outcome == "ok":
                detail = {k: int(_num(v)) for k, v in _json(r.get("failed_detail")).items()}
                if detail:
                    failures = detail
                elif good:  # hand-logged: the bad ones are the plate's mix minus what came out good
                    failures = {k: max(n - good.get(k, n), 0) for k, n in mix.items()}
                elif int(_num(r.get("units_failed"))):  # older rows: only the total
                    failures = {ANY: int(_num(r.get("units_failed")))}
            rid = db.execute(
                "INSERT INTO runs (bambuddy_run_id, plate_version_id, date, printer, outcome, source, hours, grams, "
                "filaments, aborted_pct, verdict, notes, reviewed_at, reviewed_by, synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, 'toys.py', 'toys.py')",
                (bb, vid, r.get("date"), r.get("printer", ""), outcome, "bambuddy" if r.get("source") == "bambuddy" else "manual",
                 _num(r.get("hours")), _num(r.get("grams")), json.dumps(_json(r.get("filaments"))), pct,
                 r.get("notes", ""), r.get("date"))).lastrowid
            for k, n in failures.items():
                if n <= 0:
                    continue
                scope = "any" if k == ANY else "unlinked" if k == UNLINKED else "model"
                if scope == "model" and k not in model_ids:
                    rep["notes"].append(f"run {r.get('date')} of {key[0]}: bad parts of unknown model '{k}' counted as any")
                    scope = "any"
                db.execute("INSERT INTO run_failures (run_id, model_id, scope, count) VALUES (?, ?, ?, ?)",
                           (rid, model_ids.get(k) if scope == "model" else None, scope, n))
            rep["runs"] += 1

        demand_ids = {r["slug"]: r["id"] for r in db.execute("SELECT id, slug FROM demands")}
        for e in _rows(root / "03_runs" / "events.csv"):
            slug = e.get("event_id")
            if not slug or slug in demand_ids:
                rep["skipped"] += 1
                continue
            did = db.execute(
                "INSERT INTO demands (slug, kind, name, due, target, status, notes, bambuddy_project_id, created_at, created_by) "
                "VALUES (?, 'event', ?, ?, ?, ?, ?, ?, ?, 'toys.py')",
                (slug, e.get("name") or slug, e.get("date"), int(_num(e.get("target"))),
                 _choice(e.get("status", ""), ("open", "done"), "open"), e.get("notes", ""),
                 int(e["bambuddy_project_id"]) if e.get("bambuddy_project_id", "").isdigit() else None,
                 e.get("created") or stamp[:10])).lastrowid
            demand_ids[slug] = did
            for m, n in _json(e.get("needs")).items():
                if m not in model_ids:
                    rep["notes"].append(f"event {slug}: need of unknown model '{m}' left out")
                elif int(_num(n)) > 0:
                    db.execute("INSERT INTO demand_needs (demand_id, model_id, qty) VALUES (?, ?, ?)",
                               (did, model_ids[m], int(_num(n))))
            rep["demands"] += 1

        for r in _rows(root / "03_runs" / "inventory.csv"):
            kind = {"give": "out", "adjust": "adjust"}.get(r.get("kind", ""))
            qty = int(_num(r.get("qty")))
            if not kind or not qty or not first_time("ledger", r):
                rep["skipped"] += 1
                continue
            model = r.get("model", "")
            if model and model != UNLINKED and model not in model_ids:
                rep["notes"].append(f"stock move {r.get('date')} of unknown model '{model}' booked as unlinked")
            db.execute("INSERT INTO ledger (date, kind, model_id, qty, demand_id, notes, by) VALUES (?, ?, ?, ?, ?, ?, 'toys.py')",
                       (r.get("date"), kind, model_ids.get(model), qty, demand_ids.get(r.get("event") or ""),
                        r.get("notes", "")))
            rep["stock_moves"] += 1

        db.execute("INSERT INTO sync_state (key, value) VALUES ('toys_import', ?) "
                   "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (json.dumps(seen),))
        if dry_run:
            db.execute("ROLLBACK TO import")
        db.execute("RELEASE import")

    if not dry_run:
        for src, dest in copies:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                shutil.copyfile(src, dest)
    return rep
