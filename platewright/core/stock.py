"""Finished parts going out: one model, or a mixed box split by what's on hand."""

from __future__ import annotations

from platewright.core.mix import UNLINKED, distribute


class NotEnough(ValueError):
    def __init__(self, have: int, message: str):
        super().__init__(message)
        self.have = have


def allocate_out(qty: int, on_hand: dict[str, int], model: str | None = None, force: bool = False) -> dict[str, int]:
    """Parts per model for ``qty`` going out. Without a model, a mixed box takes from each model in
    proportion to what's on hand. More than the records show raises NotEnough unless ``force``:
    then the rest is booked as unlinked parts (stock from before the records started)."""
    if qty <= 0:
        raise ValueError("the quantity must be 1 or more")
    if model:
        have = max(on_hand.get(model, 0), 0)
        if qty > have and not force:
            raise NotEnough(have, f"the records show only {have} on hand")
        return {model: qty}
    stock = {k: v for k, v in on_hand.items() if v > 0}
    have = sum(stock.values())
    if qty > have and not force:
        raise NotEnough(have, f"the records show only {have} on hand")
    alloc = {k: n for k, n in distribute(min(qty, have), stock).items() if n}
    if qty > have:
        alloc[UNLINKED] = alloc.get(UNLINKED, 0) + qty - have
    return alloc
