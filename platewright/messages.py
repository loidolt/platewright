"""Sending app messages through Bambuddy: demand that falls at risk, and a weekly summary.

They go through the channels that have "Messages from connected apps" on (here: the Home
Assistant webhook, which pushes them to the phone). Checked after every sync pass.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from platewright.bambuddy.client import Bambuddy, BambuddyError
from platewright.core.messages import demand_alerts, weekly_summary
from platewright.core.plan import demand_plan, fail_rate, month_status
from platewright.store.db import Store, now

logger = logging.getLogger(__name__)


class Messenger:
    def __init__(self, bambuddy: Bambuddy, store: Store, public_url: str):
        self.bambuddy = bambuddy
        self.store = store
        self.public_url = public_url
        self._lock = asyncio.Lock()  # the webhook's sync and the poll's must not both send

    async def send(self, title: str, message: str, path: str = "/") -> int:
        """Hand one message to Bambuddy; returns how many channels took it."""
        res = await self.bambuddy.send("POST", "/notifications/app-message",
                                       json={"title": title[:120], "message": message[:2000],
                                             "url": f"{self.public_url}{path}"})
        channels = int(res.get("channels", 0)) if isinstance(res, dict) else 0
        state = self.store.state("messages", {})
        state.update({"last_at": now(), "last_title": title, "channels": channels, "error": None})
        if channels == 0:
            state["error"] = ("no Bambuddy channel takes app messages: turn on \"Messages from connected apps\" "
                              "on a notification provider")
        self.store.set_state("messages", state)
        return channels

    def _plan(self, today: dt.date):
        settings = self.store.settings()
        return demand_plan(self.store.open_demands(), self.store.delivered(),
                           {k: v["on_hand"] for k, v in self.store.stock().items()}, self.store.active_plates(),
                           fail_rate(self.store.run_totals(), settings), settings, today)

    async def tick(self, when: dt.datetime | None = None) -> list[str]:
        """Send what is due. Returns the titles sent (for logs and tests). Each alert is remembered
        as soon as a channel took it; nothing is remembered when no channel takes app messages, so
        it goes out once one does. A refusal (e.g. Bambuddy's rate limit) ends the pass; the next
        sync picks up where it stopped."""
        async with self._lock:
            settings = self.store.settings()
            if not settings.messages:
                return []
            when = when or dt.datetime.now().astimezone()
            today = when.date()
            sent: list[str] = []
            try:
                plans = self._plan(today)
                alerts, state = demand_alerts(plans, self.store.state("alerted", {}), settings)
                self.store.set_state("alerted", state)  # recovered demands drop out
                for a in alerts:
                    if not await self.send(a["title"], a["message"], f"/demand/{a['slug']}"):
                        return sent
                    state[a["slug"]] = a["kind"]
                    self.store.set_state("alerted", state)
                    sent.append(a["title"])

                week = f"{today.isocalendar().year}-W{today.isocalendar().week:02d}"
                due = today.weekday() == settings.summary_weekday and when.hour >= settings.summary_hour
                if due and self.store.state("summary_week") != week:
                    totals = self.store.run_totals()
                    s = weekly_summary(today, totals, month_status(totals, self.store.active_plates(), settings, today),
                                       len(self.store.to_review(today=today)), plans, settings)
                    if await self.send(s["title"], s["message"]):
                        self.store.set_state("summary_week", week)
                        sent.append(s["title"])
            except BambuddyError as exc:
                state = self.store.state("messages", {})
                state.update({"error": str(exc), "error_at": now()})
                self.store.set_state("messages", state)
                logger.warning("App message failed: %s", exc)
            return sent
