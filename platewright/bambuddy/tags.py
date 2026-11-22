"""Move an AMS tray's RFID tag from the spool Bambuddy linked it to onto the right spool.

Bambuddy v1.3.1 has no single call for this, and in built-in inventory mode its UI can't link or
move a tag. These are the inventory API calls that do it by hand, in this order:

1. ``PATCH /inventory/spools/{wrong}``: clear its tag_uid, tray_uuid, data_origin and tag_type;
2. ``POST /inventory/assignments``: put the right spool in the slot (and out of any other slot);
3. ``PATCH /inventory/spools/{right}/link-tag``: give it the tray's tag_uid and tray_uuid;
4. ``PATCH /inventory/spools/{right}``: tag_type "bambulab", for a Bambu tag.

The tag is cleared first because link-tag refuses a tag another active spool carries (409). When
no spool carries the tag (a move that stopped half way), step 1 is skipped. Every call needs the
API key's Manage Inventory permission.

When Bambuddy picked the right spool after all, ``confirm_tag`` keeps it there: it links the tag
to that spool again through ``link-tag``, which records it the way a link made by hand is recorded
(``data_origin`` "nfc_link", link-tag's default), so the slot is no longer flagged.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from platewright.bambuddy.client import Bambuddy, BambuddyError
from platewright.core.slots import Slot

Step = tuple[str, str, str, dict]  # what it does (for messages), method, path, JSON body


@dataclass
class MoveFailed(Exception):
    step: str  # what Bambuddy refused to do
    error: BambuddyError
    done: list[str] = field(default_factory=list)  # what was done before it

    def __str__(self) -> str:
        # The panel itself says which permission a refusal needs; keep Bambuddy's own words.
        why = str(self.error).replace(" (does the API key have this permission?)", "")
        return f"Bambuddy refused to {self.step}: {why}"


async def move_tag(bambuddy: Bambuddy, slot: Slot, to_spool: int) -> list[str]:
    """Move the slot's tag to spool ``to_spool``. Returns what was done, step by step; raises
    MoveFailed at the first refusal, with the steps done before it."""
    steps: list[Step] = []
    if slot.holder is not None and slot.holder["id"] != to_spool:
        steps.append((f"clear the tag from spool #{slot.holder['id']}", "PATCH",
                      f"/inventory/spools/{slot.holder['id']}",
                      {"tag_uid": None, "tray_uuid": None, "data_origin": None, "tag_type": None}))
    steps.append(_assign(slot, to_spool))
    steps.append((f"link the tag to spool #{to_spool}", "PATCH", f"/inventory/spools/{to_spool}/link-tag",
                  {"tag_uid": slot.tag_uid or None, "tray_uuid": slot.tray_uuid or None}))
    if slot.bambu_tag:
        steps.append((f"mark spool #{to_spool}'s tag as a Bambu tag", "PATCH", f"/inventory/spools/{to_spool}",
                      {"tag_type": "bambulab"}))
    return await _run(bambuddy, steps)


async def confirm_tag(bambuddy: Bambuddy, slot: Slot) -> list[str]:
    """Keep the slot's tag on the spool that carries it, and put that spool in the slot if another
    one is assigned there. Returns what was done; raises MoveFailed like ``move_tag``."""
    if slot.holder is None:
        raise ValueError("no spool carries this tag")
    keep = slot.holder["id"]
    steps: list[Step] = []
    if not slot.spool or slot.spool.get("id") != keep:
        steps.append(_assign(slot, keep))
    body = {"tag_uid": slot.tag_uid or None, "tray_uuid": slot.tray_uuid or None}
    if slot.bambu_tag:
        body["tag_type"] = "bambulab"
    steps.append((f"keep the tag on spool #{keep}", "PATCH", f"/inventory/spools/{keep}/link-tag", body))
    return await _run(bambuddy, steps)


def _assign(slot: Slot, spool_id: int) -> Step:
    return (f"assign spool #{spool_id} to slot {slot.label}", "POST", "/inventory/assignments",
            {"spool_id": spool_id, "printer_id": slot.printer_id, "ams_id": slot.ams_id,
             "tray_id": slot.tray_id, "move": True})


async def _run(bambuddy: Bambuddy, steps: list[Step]) -> list[str]:
    done: list[str] = []
    for what, method, path, body in steps:
        try:
            await bambuddy.send(method, path, json=body)
        except BambuddyError as exc:
            raise MoveFailed(what, exc, done) from exc
        done.append(what)
    return done
