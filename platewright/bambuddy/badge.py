"""Report the To do count on Platewright's sidebar link, every few minutes.

Bambuddy hides a count it hasn't heard about for 10 minutes, so the report
has to keep coming; the default interval is 5 minutes. In phase 0 the count
is a stand-in number an admin can change from the hello page.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from platewright.bambuddy.client import Bambuddy

logger = logging.getLogger(__name__)


@dataclass
class BadgeState:
    count: int
    last_attempt: datetime | None = None
    last_ok: datetime | None = None
    links: int | None = None
    error: str | None = None


class BadgeReporter:
    def __init__(self, bambuddy: Bambuddy, count: int, interval: int):
        self.bambuddy = bambuddy
        self.interval = interval
        self.state = BadgeState(count=count)
        self._task: asyncio.Task | None = None

    @property
    def configured(self) -> bool:
        return bool(self.bambuddy.settings.bambuddy_key)

    async def report(self) -> None:
        if not self.configured:
            self.state.error = "PLATEWRIGHT_BAMBUDDY_KEY not set"
            return
        self.state.last_attempt = datetime.now(timezone.utc)
        try:
            self.state.links = await self.bambuddy.set_badge(self.state.count)
        except httpx.HTTPStatusError as exc:
            self.state.error = _explain(exc.response)
            logger.warning("Badge report refused: %s", self.state.error)
        except httpx.HTTPError as exc:
            self.state.error = f"Bambuddy unreachable: {type(exc).__name__}"
            logger.warning("Badge report failed: %s", self.state.error)
        else:
            self.state.error = None
            self.state.last_ok = self.state.last_attempt

    def set_count(self, count: int) -> None:
        # The caller reports right away; the loop keeps repeating it.
        self.state.count = count

    async def _run(self) -> None:
        while True:
            await self.report()
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="badge-reporter")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


def _explain(response: httpx.Response) -> str:
    status = response.status_code
    if status == 404:
        return "404: no sidebar link has Platewright's address (check the link URL)"
    if status in (401, 403):
        return f"{status}: API key refused (needs Send notifications)"
    return f"HTTP {status}"
