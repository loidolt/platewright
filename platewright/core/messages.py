"""The words of the messages Platewright sends through Bambuddy's notification channels."""

from __future__ import annotations

import datetime as dt

from platewright.core.plan import DemandPlan, MonthStatus, RunTotals
from platewright.core.settings import Settings


def risk_reason(p: DemandPlan, settings: Settings) -> str:
    if p.days < 0:
        return f"its date passed {-p.days} days ago"
    if p.no_plate:
        return "no active plate makes some of what it needs"
    return f"it needs ≈ {p.cumulative_plates} plates with earlier demand, and only {p.slots:g} plate slots are left"


def demand_alerts(plans: list[DemandPlan], sent: dict[str, str], settings: Settings) -> tuple[list[dict], dict[str, str]]:
    """Messages for demand that became at risk since the last check, and the new state.
    A demand is announced once when it becomes at risk (again after it recovered, or when the
    reason changes); nothing is repeated while it stays the same."""
    out, state = [], {}
    for p in plans:
        if not p.at_risk:
            continue
        reason = risk_reason(p, settings)
        key = reason.split(",")[0].split(" with ")[0]
        state[p.demand.slug] = key
        if sent.get(p.demand.slug) == key:
            continue
        out.append({
            "slug": p.demand.slug,
            "title": f"{p.demand.name}: {p.short} {settings.unit(p.short)} short",
            "message": f"{p.demand.name} on {p.demand.due} needs {p.demand.total} {settings.unit()}; "
                       f"{p.covered} are on hand for it and {p.delivered} went out. It is at risk: {reason}.",
        })
    return out, state


def weekly_summary(today: dt.date, runs: list[RunTotals], month: MonthStatus, to_review: int,
                   plans: list[DemandPlan], settings: Settings) -> dict:
    """The week in a few lines: what was made, where the month stands, what needs someone."""
    since = (today - dt.timedelta(days=7)).isoformat()
    week = [r for r in runs if r.date >= since]
    good = sum(r.good for r in week)
    bad = sum(r.planned - r.good for r in week)
    lines = [f"Last 7 days: {good} good {settings.unit(good)} from {len(week)} plate{'' if len(week) == 1 else 's'}"
             + (f", {bad} bad" if bad else "") + "."]
    if month.target:
        line = f"{month.month}: {month.good} of {month.target}"
        if month.remaining and month.best_plate and month.days_left:
            line += f"; ≈ {month.plates_needed} more plates of {month.best_plate.code} in {month.days_left} days"
            if month.behind:
                line += " (behind pace)"
        elif not month.remaining:
            line += " (target met)"
        lines.append(line + ".")
    if to_review:
        lines.append(f"{to_review} finished print{'' if to_review == 1 else 's'} to review.")
    for p in plans[:5]:
        state = "covered" if not p.short else f"short {p.short}" + (" (at risk)" if p.at_risk else "")
        lines.append(f"{p.demand.name} ({p.demand.due}): {state}.")
    return {"title": "Platewright: the week", "message": "\n".join(lines)[:2000]}
