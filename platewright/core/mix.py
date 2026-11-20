"""Parts per model on a plate, and how many came out good."""

from __future__ import annotations

UNLINKED = "_unlinked"  # parts on a plate that aren't linked to a registered model
ANY = "_any"  # failed parts recorded without saying which model

GOOD_OUTCOMES = ("ok",)
OUTCOMES = ("ok", "rejected", "failed", "stopped", "cancelled", "aborted")


def distribute(total: int, weights: dict[str, float]) -> dict[str, int]:
    """Split a whole number across keys in proportion to their weights (largest remainder)."""
    wsum = sum(weights.values())
    if total <= 0 or wsum <= 0:
        return {k: 0 for k in weights}
    raw = {k: total * w / wsum for k, w in weights.items()}
    out = {k: int(v) for k, v in raw.items()}
    left = total - sum(out.values())
    for k in sorted(raw, key=lambda k: (raw[k] - out[k], weights[k]), reverse=True)[:left]:
        out[k] += 1
    return out


def fit_mix(mix: dict[str, int], units: int) -> dict[str, int]:
    """A plate's parts per model, made to sum to its unit count."""
    mix = {k: int(v) for k, v in mix.items() if int(v) > 0}
    if sum(mix.values()) == units:
        return mix
    return distribute(units, mix) if mix else {UNLINKED: units}


def good_by_model(planned: dict[str, int], outcome: str, failures: dict[str, int] | None = None) -> dict[str, int]:
    """Good parts per model for one run. ``failures`` holds bad parts per model; under ANY,
    bad parts of unknown model, spread over the plate's mix. Anything but an ok run
    yields nothing usable."""
    if outcome not in GOOD_OUTCOMES:
        return {k: 0 for k in planned}
    failures = {k: int(v) for k, v in (failures or {}).items()}
    total = sum(planned.values())
    bad = {k: min(failures.get(k, 0), n) for k, n in planned.items()}
    if failures.get(ANY):
        left = {k: n - bad[k] for k, n in planned.items()}
        extra = distribute(min(failures[ANY], sum(left.values())), left)
        bad = {k: bad[k] + extra.get(k, 0) for k in planned}
    good = {k: n - bad.get(k, 0) for k, n in planned.items()}
    assert sum(good.values()) <= total
    return good
