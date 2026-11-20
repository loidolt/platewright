"""The database: one SQLite file, migrated on open."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from platewright.core.mix import ANY, UNLINKED, fit_mix, good_by_model
from platewright.core.plan import ActivePlate, Demand, RunTotals
from platewright.core.plates import Model, PlateCheck
from platewright.core.runs import SyncedRun, VersionRef
from platewright.core.settings import Settings

MIGRATIONS = ["schema_001.sql"]


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class PlateRow:
    id: int
    code: str
    description: str
    active: bool
    version_id: int
    version: int
    sha256: str
    bambuddy_file_id: int | None
    units: int
    objects: int
    hours: float
    grams: float
    color_changes: int
    filaments: dict
    printer_model: str
    nozzle: str
    warnings: list
    registered_at: str
    registered_by: str
    mix: dict  # model slug (UNLINKED) -> parts

    @property
    def min_per_unit(self) -> float:
        return round(self.hours * 60 / self.units, 1) if self.units else 0.0

    @property
    def g_per_unit(self) -> float:
        return round(self.grams / self.units, 2) if self.units else 0.0


@dataclass(frozen=True)
class RunRow:
    id: int
    bambuddy_run_id: str | None
    archive_id: int | None
    plate_code: str
    version: int
    date: str
    printer: str
    outcome: str
    source: str
    verdict: str
    hours: float
    grams: float
    planned: int
    good_by_model: dict
    matched_by_name: bool
    notes: str

    @property
    def good(self) -> int:
        return sum(self.good_by_model.values())


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._db.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode = WAL")
        self.migrate()

    def close(self) -> None:
        self._db.close()

    # -- plumbing ---------------------------------------------------------------------------

    def q(self, sql: str, *args) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def one(self, sql: str, *args) -> sqlite3.Row | None:
        rows = self.q(sql, *args)
        return rows[0] if rows else None

    def tx(self):
        store = self

        class _Tx:
            def __enter__(self):
                store._lock.acquire()
                try:
                    store._db.execute("BEGIN IMMEDIATE")
                except BaseException:
                    store._lock.release()
                    raise
                return store._db

            def __exit__(self, exc_type, *_):
                try:
                    if exc_type:
                        store._db.execute("ROLLBACK")
                    else:
                        try:
                            store._db.execute("COMMIT")
                        except BaseException:
                            if store._db.in_transaction:
                                store._db.execute("ROLLBACK")
                            raise
                finally:
                    store._lock.release()

        return _Tx()

    def migrate(self) -> None:
        with self._lock:
            current = self._db.execute("PRAGMA user_version").fetchone()[0]
            for n, name in enumerate(MIGRATIONS, start=1):
                if n <= current:
                    continue
                sql = resources.files("platewright.store").joinpath(name).read_text()
                self._db.executescript(f"BEGIN; {sql}; PRAGMA user_version = {n}; COMMIT;")

    # -- settings and sync state ------------------------------------------------------------

    def settings_values(self) -> dict:
        return {r["key"]: json.loads(r["value"]) for r in self.q("SELECT key, value FROM settings")}

    def settings(self) -> Settings:
        return Settings.from_values(self.settings_values())

    def set_settings(self, values: dict) -> None:
        with self.tx() as db:
            for k, v in values.items():
                db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                           "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (k, json.dumps(v)))

    def seed_settings(self, values: dict) -> bool:
        """Settings from a seed file, only into an empty table (the Settings page owns them after)."""
        if self.one("SELECT 1 FROM settings LIMIT 1"):
            return False
        self.set_settings(values)
        return True

    def state(self, key: str, default=None):
        r = self.one("SELECT value FROM sync_state WHERE key = ?", key)
        return json.loads(r["value"]) if r else default

    def set_state(self, key: str, value) -> None:
        self.q("INSERT INTO sync_state (key, value) VALUES (?, ?) "
               "ON CONFLICT (key) DO UPDATE SET value = excluded.value", key, json.dumps(value))

    # -- models -------------------------------------------------------------------------------

    def models(self) -> list[sqlite3.Row]:
        return self.q("SELECT m.*, (SELECT group_concat(alias, char(31)) FROM model_aliases a "
                      "WHERE a.model_id = m.id) AS aliases FROM models m ORDER BY lower(m.name)")

    def core_models(self) -> list[Model]:
        return [Model(slug=r["slug"], name=r["name"], status=r["status"], distribution_ok=r["distribution_ok"],
                      parts_per_unit=r["parts_per_unit"], ref=r["ref"],
                      aliases=tuple((r["aliases"] or "").split("\x1f")) if r["aliases"] else ())
                for r in self.models()]

    def add_model(self, slug: str, name: str, *, by: str = "", aliases: tuple[str, ...] = (), **fields) -> int:
        cols = ["slug", "name", "created_at", "created_by", *fields]
        with self.tx() as db:
            cur = db.execute(f"INSERT INTO models ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                             (slug, name, now(), by, *fields.values()))
            for a in aliases:
                db.execute("INSERT INTO model_aliases (model_id, alias) VALUES (?, ?)", (cur.lastrowid, a))
            return cur.lastrowid

    # -- plates -------------------------------------------------------------------------------

    def _mix(self, version_id: int) -> dict:
        rows = self.q("SELECT m.slug, x.units FROM plate_mix x LEFT JOIN models m ON m.id = x.model_id "
                      "WHERE x.plate_version_id = ?", version_id)
        mix: dict[str, int] = {}
        for r in rows:
            k = r["slug"] or UNLINKED
            mix[k] = mix.get(k, 0) + r["units"]
        return mix

    def _plate_row(self, r: sqlite3.Row) -> PlateRow:
        return PlateRow(
            id=r["plate_id"], code=r["code"], description=r["description"], active=bool(r["active"]),
            version_id=r["id"], version=r["version"], sha256=r["sha256"], bambuddy_file_id=r["bambuddy_file_id"],
            units=r["units"], objects=r["objects"], hours=r["hours"], grams=r["grams"],
            color_changes=r["color_changes"], filaments=json.loads(r["filaments"]),
            printer_model=r["printer_model"], nozzle=r["nozzle"], warnings=json.loads(r["warnings"]),
            registered_at=r["registered_at"], registered_by=r["registered_by"],
            mix=fit_mix(self._mix(r["id"]), r["units"]))

    def plates(self, active_only: bool = False) -> list[PlateRow]:
        rows = self.q("SELECT v.*, p.code, p.description, p.active FROM plates p "
                      "JOIN plate_versions v ON v.id = p.current_version_id "
                      + ("WHERE p.active = 1 " if active_only else "") + "ORDER BY p.active DESC, p.code")
        return [self._plate_row(r) for r in rows]

    def plate_versions(self, code: str) -> list[PlateRow]:
        rows = self.q("SELECT v.*, p.code, p.description, p.active FROM plates p "
                      "JOIN plate_versions v ON v.plate_id = p.id WHERE p.code = ? ORDER BY v.version DESC", code)
        return [self._plate_row(r) for r in rows]

    def register_plate(self, code: str, check: PlateCheck, *, sha256: str, printer_model: str = "",
                       printer_model_id: str = "", nozzle: str = "", objects: int | None = None,
                       file_path: str = "", bambuddy_file_id: int | None = None, description: str | None = None,
                       by: str = "") -> int:
        """Lock a checked plate as the next version of ``code``. Returns the version id."""
        if not check.ok:
            raise ValueError("a plate with blockers can't be locked")
        model_ids = {r["slug"]: r["id"] for r in self.q("SELECT id, slug FROM models")}
        with self.tx() as db:
            p = db.execute("SELECT id FROM plates WHERE code = ?", (code,)).fetchone()
            if p is None:
                pid = db.execute("INSERT INTO plates (code, description) VALUES (?, ?)",
                                 (code, description or "")).lastrowid
            else:
                pid = p["id"]
                if description is not None:
                    db.execute("UPDATE plates SET description = ? WHERE id = ?", (description, pid))
            version = db.execute("SELECT coalesce(max(version), 0) + 1 FROM plate_versions WHERE plate_id = ?",
                                 (pid,)).fetchone()[0]
            vid = db.execute(
                "INSERT INTO plate_versions (plate_id, version, sha256, bambuddy_file_id, file_path, units, "
                "objects, hours, grams, color_changes, filaments, printer_model, printer_model_id, nozzle, "
                "warnings, registered_at, registered_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, version, sha256, bambuddy_file_id, file_path, check.units,
                 objects if objects is not None else sum(check.objects_by_model.values()) + sum(check.unmatched.values()),
                 check.hours, check.grams, check.color_changes, json.dumps(check.filaments), printer_model,
                 printer_model_id, nozzle, json.dumps(check.warnings), now(), by)).lastrowid
            for slug, units in check.model_units.items():
                objs = check.objects_by_model.get(slug, 0) if slug != UNLINKED else sum(check.unmatched.values())
                db.execute("INSERT INTO plate_mix (plate_version_id, model_id, units, objects) VALUES (?, ?, ?, ?)",
                           (vid, model_ids.get(slug), units, objs))
            db.execute("UPDATE plates SET current_version_id = ?, active = 1 WHERE id = ?", (vid, pid))
            return vid

    def version_refs(self) -> tuple[dict[str, VersionRef], dict[str, VersionRef]]:
        """For sync: every version by fingerprint, and each active plate's current version by code."""
        rows = self.q("SELECT v.*, p.code, p.active, p.current_version_id FROM plate_versions v "
                      "JOIN plates p ON p.id = v.plate_id")
        by_hash, current = {}, {}
        for r in rows:
            ref = VersionRef(version_id=r["id"], plate_code=r["code"], version=r["version"], sha256=r["sha256"],
                             hours=r["hours"], grams=r["grams"], filaments=json.loads(r["filaments"]))
            by_hash[r["sha256"]] = ref
            if r["active"] and r["current_version_id"] == r["id"]:
                current[r["code"]] = ref
        return by_hash, current

    def first_registration(self) -> str | None:
        r = self.one("SELECT min(registered_at) AS d FROM plate_versions")
        return r["d"][:10] if r and r["d"] else None

    # -- runs ---------------------------------------------------------------------------------

    def upsert_synced(self, run: SyncedRun) -> str:
        """Store a run from Bambuddy's print log: 'new', 'updated', 'linked' (to the same print
        entered by hand) or 'unchanged'. Hand-entered counts are never overwritten."""
        with self.tx() as db:
            row = db.execute("SELECT * FROM runs WHERE bambuddy_run_id = ?", (run.bambuddy_run_id,)).fetchone()
            if row is None:
                twin = db.execute(
                    "SELECT r.id FROM runs r JOIN plate_versions v ON v.id = r.plate_version_id "
                    "WHERE r.bambuddy_run_id IS NULL AND r.source = 'manual' AND r.date = ? AND r.printer = ? "
                    "AND v.plate_id = (SELECT plate_id FROM plate_versions WHERE id = ?) ORDER BY r.id LIMIT 1",
                    (run.date, run.printer, run.version_id)).fetchone()
                if twin:
                    db.execute("UPDATE runs SET bambuddy_run_id = ?, bambuddy_archive_id = ? WHERE id = ?",
                               (run.bambuddy_run_id, run.archive_id, twin["id"]))
                    return "linked"
                db.execute(
                    "INSERT INTO runs (bambuddy_run_id, bambuddy_archive_id, plate_version_id, date, printer, "
                    "outcome, source, hours, grams, filaments, aborted_pct, verdict, matched_by_name, notes, synced_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'bambuddy', ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run.bambuddy_run_id, run.archive_id, run.version_id, run.date, run.printer, run.outcome, run.hours, run.grams,
                     json.dumps(run.filaments), run.aborted_pct, run.verdict, int(run.matched_by_name), run.notes,
                     now()))
                return "new"
            if row["source"] != "bambuddy":
                return "unchanged"
            # Matched by file name, a print resolves to the plate's *current* version; once a newer
            # version is locked that would move old prints to it. Keep the version it got first.
            version_id = row["plate_version_id"] if run.matched_by_name else run.version_id
            new = (version_id, run.date, run.printer, run.outcome, run.hours, run.grams,
                   json.dumps(run.filaments), run.aborted_pct, run.verdict, int(run.matched_by_name), run.archive_id)
            old = (row["plate_version_id"], row["date"], row["printer"], row["outcome"], row["hours"], row["grams"],
                   row["filaments"], row["aborted_pct"], row["verdict"], row["matched_by_name"],
                   row["bambuddy_archive_id"])
            if new == old:
                return "unchanged"
            db.execute("UPDATE runs SET plate_version_id = ?, date = ?, printer = ?, outcome = ?, hours = ?, "
                       "grams = ?, filaments = ?, aborted_pct = ?, verdict = ?, matched_by_name = ?, bambuddy_archive_id = ?, "
                       "synced_at = ? "
                       "WHERE id = ?", (*new, now(), row["id"]))
            if not row["notes"] and run.notes:
                db.execute("UPDATE runs SET notes = ? WHERE id = ?", (run.notes, row["id"]))
            return "updated"

    def _failures(self) -> dict[int, dict[str, int]]:
        out: dict[int, dict[str, int]] = {}
        for r in self.q("SELECT f.run_id, f.scope, f.count, m.slug FROM run_failures f "
                        "LEFT JOIN models m ON m.id = f.model_id"):
            key = {"any": ANY, "unlinked": UNLINKED}.get(r["scope"]) or r["slug"]
            out.setdefault(r["run_id"], {})[key] = out.get(r["run_id"], {}).get(key, 0) + r["count"]
        return out

    def runs(self, since: str | None = None, limit: int | None = None, plate_code: str | None = None) -> list[RunRow]:
        sql = ("SELECT r.*, v.version, v.units, v.id AS vid, p.code FROM runs r "
               "JOIN plate_versions v ON v.id = r.plate_version_id JOIN plates p ON p.id = v.plate_id")
        where, args = [], []
        if since:
            where.append("r.date >= ?")
            args.append(since)
        if plate_code:
            where.append("p.code = ?")
            args.append(plate_code)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY r.date DESC, r.id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        failures = self._failures()
        mixes: dict[int, dict] = {}
        out = []
        for r in self.q(sql, *args):
            if r["vid"] not in mixes:
                mixes[r["vid"]] = fit_mix(self._mix(r["vid"]), r["units"])
            planned = mixes[r["vid"]]
            out.append(RunRow(
                id=r["id"], bambuddy_run_id=r["bambuddy_run_id"], archive_id=r["bambuddy_archive_id"],
                plate_code=r["code"], version=r["version"],
                date=r["date"], printer=r["printer"], outcome=r["outcome"], source=r["source"], verdict=r["verdict"],
                hours=r["hours"], grams=r["grams"], planned=r["units"],
                good_by_model=good_by_model(planned, r["outcome"], failures.get(r["id"])),
                matched_by_name=bool(r["matched_by_name"]), notes=r["notes"]))
        return out

    def run_totals(self, since: str | None = None) -> list[RunTotals]:
        rows = self.q("SELECT id, filaments FROM runs" + (" WHERE date >= ?" if since else ""), *([since] if since else []))
        fil = {r["id"]: json.loads(r["filaments"]) for r in rows}
        return [RunTotals(r.date, r.planned, r.good, r.hours, fil.get(r.id, {})) for r in self.runs(since)]

    def awaiting_verdict(self, days: int = 30, today: dt.date | None = None) -> list[RunRow]:
        """Finished production prints Bambuddy has no good / reject for yet."""
        since = ((today or dt.date.today()) - dt.timedelta(days=days)).isoformat()
        return [r for r in self.runs(since) if r.source == "bambuddy" and r.outcome == "ok" and not r.verdict]

    # -- stock and demand -----------------------------------------------------------------

    def stock(self) -> dict[str, dict[str, int]]:
        """Per model slug: made (good parts from runs), adjusted, out, on_hand."""
        made: dict[str, int] = {}
        for r in self.runs():
            for k, n in r.good_by_model.items():
                made[k] = made.get(k, 0) + n
        adjusted: dict[str, int] = {}
        out: dict[str, int] = {}
        for r in self.q("SELECT l.kind, l.qty, m.slug FROM ledger l LEFT JOIN models m ON m.id = l.model_id"):
            k = r["slug"] or UNLINKED
            target = out if r["kind"] == "out" else adjusted
            target[k] = target.get(k, 0) + r["qty"]
        keys = set(made) | set(adjusted) | set(out)
        return {k: {"made": made.get(k, 0), "adjusted": adjusted.get(k, 0), "out": out.get(k, 0),
                    "on_hand": made.get(k, 0) + adjusted.get(k, 0) - out.get(k, 0)} for k in keys}

    def open_demands(self) -> list[Demand]:
        needs: dict[int, dict] = {}
        for r in self.q("SELECT n.demand_id, n.qty, m.slug FROM demand_needs n JOIN models m ON m.id = n.model_id"):
            needs.setdefault(r["demand_id"], {})[r["slug"]] = r["qty"]
        return [Demand(id=r["id"], slug=r["slug"], name=r["name"], due=r["due"], target=r["target"],
                       needs=needs.get(r["id"], {}), kind=r["kind"])
                for r in self.q("SELECT * FROM demands WHERE status = 'open'")]

    def delivered(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for r in self.q("SELECT d.slug AS demand, m.slug, l.qty FROM ledger l JOIN demands d ON d.id = l.demand_id "
                        "LEFT JOIN models m ON m.id = l.model_id WHERE l.kind = 'out'"):
            d = out.setdefault(r["demand"], {})
            k = r["slug"] or UNLINKED
            d[k] = d.get(k, 0) + r["qty"]
        return out

    def active_plates(self) -> list[ActivePlate]:
        return [ActivePlate(p.code, p.units, p.hours, p.mix) for p in self.plates(active_only=True)]
