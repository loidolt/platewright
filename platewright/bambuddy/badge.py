"""Report the To do count on Platewright's sidebar link, every few minutes.

Bambuddy hides a count it hasn't heard about for 10 minutes, so the report has to keep
coming: the sync loop reports it after every pass (default every 5 minutes). The count is
what needs someone: finished production prints with no good / reject verdict in Bambuddy yet,
plus open demands at risk.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
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
    def __init__(self, bambuddy: Bambuddy, count: Callable[[], int]):
        self.bambuddy = bambuddy
        self.count = count
        self.state = BadgeState(count=0)

    @property
    def configured(self) -> bool:
        return bool(self.bambuddy.settings.bambuddy_key)

    async def report(self) -> None:
        if not self.configured:
            self.state.error = "PLATEWRIGHT_BAMBUDDY_KEY not set"
            return
        self.state.last_attempt = datetime.now(timezone.utc)
        try:
            self.state.count = self.count()
        except Exception:  # a broken count must not stop the report
            logger.exception("Badge count failed")
        try:
            self.state.links = await self.bambuddy.set_badge(self.state.count)
        except httpx.HTTPStatusError as exc:
            self.state.error = _explain(exc.response)
            logger.warning("Badge report refused: %s", self.state.error)
        except httpx.HTTPError as exc:
            self.state.error = f"can't reach Bambuddy ({type(exc).__name__})"
            logger.warning("Badge report failed: %s", self.state.error)
        else:
            self.state.error = None
            self.state.last_ok = self.state.last_attempt


def _explain(response: httpx.Response) -> str:
    status = response.status_code
    if status == 404:
        return "404: no sidebar link has Platewright's address (check the link URL)"
    if status in (401, 403):
        return f"{status}: API key refused (needs Send notifications)"
    return f"HTTP {status}"
