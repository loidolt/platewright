"""Turning Bambuddy's print log into runs of locked plate versions."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

# Bambuddy print-log status -> run outcome. Anything else (printing, skipped) isn't finished.
OUTCOMES = {"completed": "ok", "failed": "failed", "stopped": "stopped", "cancelled": "cancelled"}
PLATE_NAME_RE = re.compile(r"PLATE-([A-Z0-9]+)[_.]", re.I)


@dataclass(frozen=True)
class VersionRef:
    """A plate version as the matcher needs it."""

    version_id: int
    plate_code: str
    version: int
    sha256: str
    hours: float
    grams: float
    filaments: dict  # palette name -> grams for the whole plate


@dataclass(frozen=True)
class SyncedRun:
    bambuddy_run_id: str
    version_id: int
    archive_id: int | None
    date: str
    printer: str
    outcome: str
    verdict: str  # good | reject | "" (none yet)
    hours: float
    grams: float
    filaments: dict
    aborted_pct: int | None
    notes: str
    matched_by_name: bool


def parse_ts(text: str | None) -> dt.datetime | None:
    """ISO timestamp from the API (UTC 'Z', an offset, or naive UTC) -> local time."""
    if not text:
        return None
    try:
        t = dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone()


def match_version(entry: dict, archive: dict, by_hash: dict[str, VersionRef],
                  current_by_code: dict[str, VersionRef]) -> tuple[VersionRef, bool] | None:
    """The plate version a print was, by the archive's fingerprint, else by the PLATE-X_ file
    name (then the plate's current version). (version, matched by name) or None."""
    hit = by_hash.get(archive.get("content_hash") or "")
    if hit:
        return hit, False
    for text in (archive.get("filename"), entry.get("print_name")):
        m = PLATE_NAME_RE.search(text or "")
        if m and m.group(1).upper() in current_by_code:
            return current_by_code[m.group(1).upper()], True
    return None


def synced_run(entry: dict, ref: VersionRef, printer: str, matched_by_name: bool, today: dt.date) -> SyncedRun | None:
    """A run from one finished print-log entry; None while it's still printing."""
    status = entry.get("status") or ""
    if status not in OUTCOMES:
        return None
    outcome = OUTCOMES[status]
    verdict = (entry.get("user_verdict") or "").lower()
    if outcome == "ok" and verdict == "reject":
        outcome = "rejected"
    when = parse_ts(entry.get("completed_at")) or parse_ts(entry.get("started_at")) or parse_ts(entry.get("created_at"))
    grams = entry.get("filament_used_grams")
    if outcome in ("ok", "rejected") and not grams:  # Bambuddy logs 0 when it has no usage data
        grams = ref.grams
    grams = float(grams or 0)
    frac = min(grams / ref.grams, 1.0) if ref.grams else 1.0
    secs = entry.get("duration_seconds")
    return SyncedRun(
        bambuddy_run_id=str(entry["id"]),
        version_id=ref.version_id,
        archive_id=entry.get("archive_id"),
        date=(when.date() if when else today).isoformat(),
        printer=printer,
        outcome=outcome,
        verdict=verdict if verdict in ("good", "reject") else "",
        hours=round(float(secs) / 3600, 2) if secs else round(ref.hours * frac, 2),
        grams=round(grams, 1),
        filaments={c: round(float(g) * frac, 1) for c, g in ref.filaments.items()},
        aborted_pct=None if status == "completed" else round(frac * 100),
        notes=entry.get("failure_reason") or "",
        matched_by_name=matched_by_name,
    )
