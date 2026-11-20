"""The filament colors kept loaded, and matching slicer / spool colors to them."""

from __future__ import annotations

import re


def norm_hex(value: str | None) -> str:
    v = (value or "").strip().upper()
    if v and not v.startswith("#"):
        v = "#" + v
    v = v[:7]  # slicer and spool colors may carry an alpha byte: #RRGGBBAA
    return v if re.fullmatch(r"#[0-9A-F]{6}", v) else ""


def validate_palette(palette: dict[str, dict]) -> list[str]:
    """Problems that make a palette unusable; empty when it's fine."""
    if not palette:
        return ["the palette is empty: add one entry per filament color you keep loaded"]
    errors, seen = [], {}
    for name, info in palette.items():
        hx = norm_hex(info.get("hex"))
        if not hx:
            errors.append(f"palette color '{name}' needs a hex value like \"#9D432C\"")
            continue
        key = (hx, (info.get("material") or "").upper())
        if key in seen:
            errors.append(f"palette colors '{seen[key]}' and '{name}' have the same hex and material")
        seen[key] = name
    return errors


def match_palette(hex_color: str | None, material: str | None, palette: dict[str, dict]) -> tuple[str | None, str]:
    """(palette name or None, warning). The warning is set when the color matches but the
    material differs from the palette's."""
    hx = norm_hex(hex_color)
    material = material or ""
    cands = [(n, i) for n, i in palette.items() if norm_hex(i.get("hex")) == hx]
    if not hx or not cands:
        return None, ""
    if len(cands) > 1 and material:
        same = [c for c in cands if (c[1].get("material") or "").upper() == material.upper()]
        cands = same or cands
    name, info = cands[0]
    want = (info.get("material") or "").upper()
    if want and material and want != material.upper():
        return name, f"{name} is {want} in the palette but this plate was sliced with {material.upper()}"
    return name, ""
