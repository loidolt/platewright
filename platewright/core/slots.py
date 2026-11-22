"""AMS slots: what each printer's slots hold, and the RFID links worth a second look.

When an AMS reads a Bambu RFID tag that no spool carries, Bambuddy (v1.3.1,
``services/spool_tag_matcher.py``, ``find_matching_untagged_spool``) links the tag to an existing
untagged spool on its own: the oldest active one with the same material and color, a Bambu or empty
brand, and the same subtype or none. With a sealed and an opened spool of the same color in the
inventory it often picks the wrong one. Such a spool has ``data_origin == "rfid_linked"``; the other
spools the same rule would have accepted are the candidates for the tag.

Pure functions over Bambuddy's JSON: printer status (``ams[].tray[]``, ``vt_tray[]``), spool
assignments and spools.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

EXTERNAL_AMS_ID = 255  # Bambuddy stores the external spool holder as ams_id 255
ZERO_TAG_UID = "0" * 16
ZERO_TRAY_UUID = "0" * 32


def norm_tag(value, length: int | None = None) -> str:
    """A tag id as Bambuddy compares it: hex digits only, upper case; "" for none or all zeros.
    ``length`` keeps the last that many digits (tag UIDs are stored as at most 16)."""
    text = re.sub(r"[^0-9A-Fa-f]", "", str(value or "")).upper()
    if length and len(text) > length:
        text = text[-length:]
    return "" if not text or set(text) == {"0"} else text


def spool_tags(spool: dict) -> tuple[str, str]:
    return norm_tag(spool.get("tag_uid"), 16), norm_tag(spool.get("tray_uuid"))


def tray_match(tray: dict) -> tuple[str, str | None, str] | None:
    """(material, subtype, rgba) the way Bambuddy reads them off an AMS tray to look for an untagged
    spool; None when the tray says too little (no type or no color)."""
    tray_type = (tray.get("tray_type") or "").strip()
    color = (tray.get("tray_color") or "").strip()
    if not tray_type or not color:
        return None
    sub_brands = (tray.get("tray_sub_brands") or "").strip()
    material, subtype = tray_type, None
    if sub_brands and " " in sub_brands:
        first, rest = sub_brands.split(" ", 1)
        if first.upper() == material.upper():
            subtype = rest
        else:
            material = sub_brands
    elif sub_brands and sub_brands.upper() != material.upper():
        material = sub_brands
    id_name = tray.get("tray_id_name") or ""
    if "-" in id_name:
        prefix, code = id_name.split("-", 1)
        if code[:1] == "M":
            subtype = "Dual Color" if prefix == "A05" else "Gradient"
        elif code[:1] == "T":
            subtype = "Tri Color"
    return material, subtype, color


def spool_match(spool: dict) -> tuple[str, str | None, str] | None:
    """The same key read off a spool, for a slot whose tray says too little."""
    if not spool.get("material") or not spool.get("rgba"):
        return None
    return spool["material"], spool.get("subtype") or None, spool["rgba"]


def candidates(spools: list[dict], key: tuple[str, str | None, str]) -> list[dict]:
    """Active untagged spools Bambuddy's matcher accepts for ``key``, in the order it tries them:
    exact subtype before none, then oldest first."""
    material, subtype, rgba = key
    out = []
    for s in spools:
        if s.get("archived_at") or any(spool_tags(s)):
            continue
        if (s.get("material") or "").upper() != material.upper() or (s.get("rgba") or "").upper() != rgba.upper():
            continue
        brand = (s.get("brand") or "").strip()
        if brand and "bambu" not in brand.lower():
            continue
        own = s.get("subtype") or None
        if own is not None and (subtype is None or own.upper() != subtype.upper()):
            continue
        out.append(s)
    return sorted(out, key=lambda s: (0 if subtype and s.get("subtype") else 1, s.get("created_at") or "", s["id"]))


def slot_label(ams_id: int, tray_id: int, *, ht: bool = False, module_type: str = "") -> str:
    """The slot's name as Bambuddy shows it: A1-D4, HT-A, 3H-1, Ext."""
    if ams_id == EXTERNAL_AMS_ID:
        return "Ext" if tray_id == 0 else f"Ext-{tray_id + 1}"
    if module_type == "n10a" and not ht:
        return f"3H-{tray_id + 1}"
    letter = chr(65 + (ams_id - 128 if ams_id >= 128 else ams_id) % 26)
    return f"HT-{letter}" if ht else f"{letter}{tray_id + 1}"


def external_slot(vt_id: int) -> int:
    """The tray_id Bambuddy stores for an external input: 254 -> 0, 255 -> 1, rack 247-253 -> 2-8."""
    return vt_id - 254 if vt_id in (254, 255) else vt_id - 247 + 2


@dataclass
class Slot:
    printer_id: int
    ams_id: int
    tray_id: int
    label: str
    tray: dict
    tag_uid: str = ""
    tray_uuid: str = ""
    spool: dict | None = None  # the assigned spool
    holder: dict | None = None  # the active spool carrying this tray's tag
    candidates: list[dict] = field(default_factory=list)
    reason: str = ""  # why the slot is worth a look; "" when it isn't

    @property
    def tagged(self) -> bool:
        return bool(self.tag_uid or self.tray_uuid)

    @property
    def bambu_tag(self) -> bool:
        """Bambu spools carry a tray UUID next to the tag's own UID."""
        return bool(self.tray_uuid)

    @property
    def check(self) -> bool:
        return bool(self.reason)

    @property
    def empty(self) -> bool:
        return not (self.tray.get("tray_type") or self.tagged) or self.tray.get("state") == 9


@dataclass
class PrinterSlots:
    id: int
    name: str
    connected: bool
    slots: list[Slot]

    @property
    def checks(self) -> int:
        return sum(s.check for s in self.slots)


def holder_of(spools: list[dict], tag_uid: str, tray_uuid: str) -> dict | None:
    """The active spool carrying a tag, by its UID or its tray UUID (lowest id first, as Bambuddy)."""
    for s in sorted(spools, key=lambda s: s["id"]):
        if s.get("archived_at"):
            continue
        uid, uuid = spool_tags(s)
        if (tag_uid and uid == tag_uid) or (tray_uuid and uuid == tray_uuid):
            return s
    return None


def build_slot(printer_id: int, ams_id: int, tray_id: int, label: str, tray: dict,
               assigned: dict | None, spools: list[dict]) -> Slot:
    slot = Slot(printer_id, ams_id, tray_id, label, tray, tag_uid=norm_tag(tray.get("tag_uid"), 16),
                tray_uuid=norm_tag(tray.get("tray_uuid")), spool=assigned)
    if not slot.tagged:
        return slot
    slot.holder = holder_of(spools, slot.tag_uid, slot.tray_uuid)
    key = tray_match(tray) or (spool_match(assigned) if assigned else None)
    if key is None:
        return slot
    slot.candidates = [s for s in candidates(spools, key) if not slot.holder or s["id"] != slot.holder["id"]]
    if not slot.candidates:
        return slot
    if slot.holder is not None and slot.holder.get("data_origin") == "rfid_linked":
        n = len(slot.candidates)
        slot.reason = (f"Bambuddy linked this tag to spool #{slot.holder['id']} by itself; "
                       f"{n} other spool{'' if n == 1 else 's'} match{'es' if n == 1 else ''} it too.")
    elif slot.holder is None:
        slot.reason = "No spool carries this tag."
    return slot


def printer_slots(printer: dict, status: dict, assignments: list[dict], spools: list[dict]) -> PrinterSlots:
    """Every AMS and external slot of one printer, with what is assigned and what to check."""
    pid = int(printer["id"])
    by_slot = {(a.get("ams_id"), a.get("tray_id")): a for a in assignments if a.get("printer_id") == pid}
    by_id = {s["id"]: s for s in spools}

    def assigned(ams_id: int, tray_id: int) -> dict | None:
        a = by_slot.get((ams_id, tray_id))
        if a is None:
            return None
        return by_id.get(a.get("spool_id")) or a.get("spool") or {"id": a.get("spool_id")}

    out = []
    for unit in sorted(status.get("ams") or [], key=lambda u: int(u.get("id", 0))):
        ams_id = int(unit.get("id", 0))
        for tray in sorted(unit.get("tray") or [], key=lambda t: int(t.get("id", 0))):
            tray_id = int(tray.get("id", 0))
            label = slot_label(ams_id, tray_id, ht=bool(unit.get("is_ams_ht")), module_type=unit.get("module_type") or "")
            out.append(build_slot(pid, ams_id, tray_id, label, tray, assigned(ams_id, tray_id), spools))
    for vt in sorted(status.get("vt_tray") or [], key=lambda t: int(t.get("id", 254))):
        tray_id = external_slot(int(vt.get("id", 254)))
        out.append(build_slot(pid, EXTERNAL_AMS_ID, tray_id, slot_label(EXTERNAL_AMS_ID, tray_id), vt,
                              assigned(EXTERNAL_AMS_ID, tray_id), spools))
    return PrinterSlots(pid, status.get("name") or printer.get("name") or f"printer {pid}",
                        bool(status.get("connected")), out)


def find_slot(printers: list[PrinterSlots], printer_id: int, ams_id: int, tray_id: int) -> Slot | None:
    for p in printers:
        if p.id == printer_id:
            for s in p.slots:
                if (s.ams_id, s.tray_id) == (ams_id, tray_id):
                    return s
    return None
