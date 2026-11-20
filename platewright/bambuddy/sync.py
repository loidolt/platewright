"""Bring Bambuddy's finished prints over as runs of locked plates.

Runs every few minutes (and on demand). Matching is by the archive's fingerprint
(``content_hash`` = sha256 of the library file), then by the PLATE-X_ file name. Polling stays
even with webhooks later, because a verdict changed in Bambuddy sends no webhook.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from platewright.bambuddy.client import Bambuddy, BambuddyError
from platewright.core.runs import match_version, synced_run
from platewright.store.db import Store, now

logger = logging.getLogger(__name__)


async def sync_once(bambuddy: Bambuddy, store: Store, today: dt.date | None = None) -> dict:
    """One pass over the print log. Returns (and stores) a summary for the Today page."""
    today = today or dt.date.today()
    settings = store.settings()
    by_hash, current = store.version_refs()
    summary = {"at": now(), "new": 0, "updated": 0, "linked": 0, "unchanged": 0, "printing": 0,
               "by_name": [], "other": {}, "error": None}
    if not by_hash:
        summary["note"] = "no plates registered yet"
    # Prints from before the first plate can't be production runs; with no plates yet, the
    # last 30 days are enough to show what is being printed.
    since = store.first_registration() or (today - dt.timedelta(days=30)).isoformat()
    # An archive's file name and fingerprint never change, so each is fetched once. Missing or
    # not-yet-fingerprinted archives are asked again on the next pass.
    known: dict[str, dict] = store.state("archives", {})
    seen: dict[str, dict] = {}
    try:
        entries = await bambuddy.print_log(since)
        for e in entries:
            status = e.get("status") or ""
            if status == "printing":
                summary["printing"] += 1
            aid = e.get("archive_id")
            arc: dict = {}
            if aid and by_hash:
                arc = known.get(str(aid)) or seen.get(str(aid)) or {}
                if not arc:
                    try:
                        a = await bambuddy.archive(aid)
                        arc = {"content_hash": a.get("content_hash"), "filename": a.get("filename")}
                    except BambuddyError as exc:
                        if exc.status != 404:
                            raise
                if arc.get("content_hash"):
                    seen[str(aid)] = arc
            hit = match_version(e, arc, by_hash, current)
            if not hit:
                if status in ("completed", "failed", "stopped", "cancelled"):
                    label = e.get("print_name") or arc.get("filename") or f"print {e['id']}"
                    summary["other"][label] = summary["other"].get(label, 0) + 1
                continue
            ref, by_name = hit
            run = synced_run(e, ref, settings.printer_key(e.get("printer_name")), by_name, today)
            if run is None:
                continue
            if by_name:
                summary["by_name"].append(f"#{e['id']} {arc.get('filename') or e.get('print_name')}")
            summary[store.upsert_synced(run)] += 1
    except BambuddyError as exc:
        summary["error"] = str(exc)
        logger.warning("Sync failed: %s", exc)
    if not summary["error"] and seen != known:
        store.set_state("archives", seen)  # only archives still in the window
    store.set_state("last_sync", summary)
    if not summary["error"]:
        logger.info("Synced: %d new, %d updated, %d linked, %d not production plates",
                    summary["new"], summary["updated"], summary["linked"], sum(summary["other"].values()))
    return summary


class SyncLoop:
    def __init__(self, bambuddy: Bambuddy, store: Store, interval: int, after=None):
        self.bambuddy = bambuddy
        self.store = store
        self.interval = interval
        self.after = after  # called after each pass (e.g. report the badge)
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def run_now(self) -> dict:
        async with self._lock:
            summary = await sync_once(self.bambuddy, self.store)
        if self.after:
            await self.after()
        return summary

    async def _run(self) -> None:
        while True:
            try:
                await self.run_now()
            except Exception:  # keep the loop alive; the next pass tries again
                logger.exception("Sync pass crashed")
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="sync")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
