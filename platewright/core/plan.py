"""Pace against the monthly target, demand coverage and filament days left."""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass, field

from platewright.core.palette import match_palette
from platewright.core.settings import Settings


@dataclass(frozen=True)
class RunTotals:
    date: str
    planned: int
    good: int
    hours: float = 0.0
    filaments: dict = field(default_factory=dict)  # palette name -> grams used


@dataclass(frozen=True)
class ActivePlate:
    code: str
    units: int
    hours: float
    mix: dict  # model slug -> parts


def fail_rate(runs: list[RunTotals], settings: Settings, min_parts: int = 50) -> float:
    """The measured fail rate once there's enough to go on, else the assumed one."""
    planned = sum(r.planned for r in runs)
    good = sum(r.good for r in runs)
    return (planned - good) / planned if planned >= min_parts else settings.assumed_fail_rate


def plates_for(n: int, per_plate: int, fail: float) -> int:
    return int(-(-n // max(per_plate * (1 - fail), 1))) if n > 0 else 0


@dataclass
class MonthStatus:
    month: str
    target: int
    good: int
    planned: int
    hours: float
    days_left: int
    slots: float
    remaining: int
    best_plate: ActivePlate | None
    plates_needed: int

    @property
    def failed(self) -> int:
        return self.planned - self.good

    @property
    def fail_rate(self) -> float | None:
        return self.failed / self.planned if self.planned else None

    @property
    def behind(self) -> bool:
        return bool(self.remaining and self.days_left and self.plates_needed > self.slots)

    @property
    def high_fail(self) -> bool:
        return self.planned >= 50 and self.failed / self.planned > 0.05

    @property
    def progress(self) -> float:
        return min(self.good / self.target, 1.0) if self.target else 0.0


def month_status(runs: list[RunTotals], plates: list[ActivePlate], settings: Settings, today: dt.date,
                 month: str | None = None) -> MonthStatus:
    this_month = today.strftime("%Y-%m")
    month = month or this_month
    in_month = [r for r in runs if r.date.startswith(month)]
    good = sum(r.good for r in in_month)
    planned = sum(r.planned for r in in_month)
    y, m = map(int, month.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    if month == this_month:
        days_left = last_day - today.day + 1
    else:
        days_left = last_day if month > this_month else 0
    remaining = max(settings.period_target - good, 0)
    best = max(plates, key=lambda p: p.units, default=None)
    rate = fail_rate(in_month, settings)
    needed = plates_for(remaining, best.units, rate) if best else 0
    return MonthStatus(month=month, target=settings.period_target, good=good, planned=planned,
                       hours=round(sum(r.hours for r in in_month), 1), days_left=days_left,
                       slots=days_left * settings.plates_per_day, remaining=remaining, best_plate=best,
                       plates_needed=needed)


@dataclass(frozen=True)
class Demand:
    id: int
    slug: str
    name: str
    due: str  # YYYY-MM-DD
    target: int  # 0 = the sum of needs
    needs: dict  # model slug -> parts
    kind: str = "event"

    @property
    def total(self) -> int:
        return self.target or sum(self.needs.values())


@dataclass
class DemandPlan:
    demand: Demand
    delivered: int
    covered: int
    short: int
    short_by_model: dict
    plates_by_model: dict  # slug -> (plates, plate code) or None when no active plate makes it
    plates: int
    cumulative_plates: int
    days: int
    slots: float

    @property
    def no_plate(self) -> list[str]:
        return [k for k, v in self.plates_by_model.items() if v is None]

    @property
    def at_risk(self) -> bool:
        return bool(self.short) and (self.days < 0 or bool(self.no_plate) or self.cumulative_plates > self.slots)


def demand_plan(demands: list[Demand], delivered: dict[str, dict[str, int]], on_hand: dict[str, int],
                plates: list[ActivePlate], fail: float, settings: Settings, today: dt.date) -> list[DemandPlan]:
    """Open demands in date order with stock coverage, shortfall and plate estimates. Parts on
    hand are promised to the earliest demand first: its named models, then any model."""
    best_all = max(((p.units, p.code) for p in plates), default=None)
    avail = {k: v for k, v in on_hand.items() if v > 0}
    cum = 0
    out = []
    for d in sorted(demands, key=lambda d: (d.due, d.slug)):
        dlv = delivered.get(d.slug, {})
        rem_total = max(d.total - sum(dlv.values()), 0)
        covered = 0
        short_m: dict[str, int] = {}
        model_plates: dict = {}
        for k, n in d.needs.items():
            rem = max(n - dlv.get(k, 0), 0)
            c = min(avail.get(k, 0), rem)
            avail[k] = avail.get(k, 0) - c
            covered += c
            short_m[k] = rem - c
            if short_m[k]:
                cands = [(p.mix[k], p.code) for p in plates if p.mix.get(k, 0) > 0]
                if cands:
                    per, code = max(cands)
                    model_plates[k] = (plates_for(short_m[k], per, fail), code)
                else:
                    model_plates[k] = None
        any_need = max(rem_total - sum(max(n - dlv.get(k, 0), 0) for k, n in d.needs.items()), 0)
        took = 0
        for k in sorted(avail, key=avail.get, reverse=True):
            t = min(avail[k], any_need - took)
            if t <= 0:
                break
            avail[k] -= t
            took += t
        covered += took
        short = sum(short_m.values()) + any_need - took
        # Models on the same plate share its runs; different plates add up. Never fewer than
        # the whole shortfall on the biggest plate.
        by_code: dict[str, int] = {}
        for v in model_plates.values():
            if v:
                by_code[v[1]] = max(by_code.get(v[1], 0), v[0])
        n_plates = max(sum(by_code.values()), plates_for(short, best_all[0], fail) if short and best_all else 0)
        cum += n_plates
        days = (dt.date.fromisoformat(d.due) - today).days
        out.append(DemandPlan(demand=d, delivered=sum(dlv.values()), covered=covered, short=short,
                              short_by_model=short_m, plates_by_model=model_plates, plates=n_plates,
                              cumulative_plates=cum, days=days, slots=max(days, 0) * settings.plates_per_day))
    return out


@dataclass
class FilamentRow:
    color: str
    hex: str
    material: str
    slot: str
    grams: float
    per_day: float

    def days_left(self) -> float | None:
        return self.grams / self.per_day if self.per_day else None

    def reorder(self, reorder_days: float) -> bool:
        d = self.days_left()
        return self.grams <= 0 or (d is not None and d < reorder_days)


def filament(spools: list[dict], runs: list[RunTotals], settings: Settings,
             today: dt.date) -> tuple[list[FilamentRow], list[dict]]:
    """Grams on hand per palette color from Bambuddy's spools, use per day over the last 30 days,
    and the spools that match no palette color."""
    on_hand: dict[str, float] = {}
    other = []
    for s in spools:
        if s.get("archived_at"):
            continue
        left = float(s.get("label_weight") or 1000) - float(s.get("weight_used") or 0)
        name, _ = match_palette("#" + (s.get("rgba") or "")[:6], s.get("material") or "", settings.palette)
        if name:
            on_hand[name] = on_hand.get(name, 0) + left
        else:
            other.append({**s, "grams_left": left})
    cutoff = (today - dt.timedelta(days=30)).isoformat()
    recent = [r for r in runs if r.date >= cutoff]
    # With only a few days of history, divide by those days, not by 30.
    span = (today - dt.date.fromisoformat(min(r.date for r in recent))).days + 1 if recent else 30
    span = min(max(span, 1), 30)
    burn: dict[str, float] = {}
    for r in recent:
        for c, g in r.filaments.items():
            burn[c] = burn.get(c, 0) + float(g) / span
    rows = [FilamentRow(color=name, hex=info.get("hex", ""), material=info.get("material", ""),
                        slot=info.get("slot", ""), grams=round(on_hand.get(name, 0), 0),
                        per_day=round(burn.get(name, 0), 1))
            for name, info in settings.palette.items()]
    return rows, other
