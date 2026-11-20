"""Reading sliced plates (.gcode.3mf exported from Bambu Studio or OrcaSlicer)."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import IO

from platewright.core.names import base_object_name
from platewright.core.palette import match_palette, norm_hex


class ThreeMFError(Exception):
    """The file can't be used as a production plate. ``details`` adds lines worth showing."""

    def __init__(self, message: str, details: list[str] | None = None):
        super().__init__(message)
        self.details = details or []


@dataclass(frozen=True)
class Filament:
    hex: str
    type: str
    grams: float


@dataclass(frozen=True)
class SlicedPlate:
    objects: list[str]
    hours: float
    grams: float
    filaments: list[Filament]
    color_changes: int
    printer_model: str = ""
    printer_model_id: str = ""
    nozzle: str = ""
    extra: dict = field(default_factory=dict)


def _num(text, default: float = 0.0) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _meta(el) -> dict[str, str]:
    return {m.get("key"): m.get("value") for m in el.findall("metadata")}


def project_summary(z: zipfile.ZipFile, palette: dict | None = None) -> list[str]:
    """Best-effort description of an unsliced project, to show what's in it."""
    lines = []
    try:
        root = ET.fromstring(z.read("Metadata/model_settings.config"))
        names = {o.get("id"): _meta(o).get("name", "?") for o in root.findall("object")}
        for p in root.findall("plate"):
            groups: dict[str, int] = {}
            for inst in p.findall("model_instance"):
                base = base_object_name(names.get(_meta(inst).get("object_id"), "?"))
                groups[base] = groups.get(base, 0) + 1
            mix = ", ".join(f"'{n}' x{c}" for n, c in groups.items())
            lines.append(f"plate {_meta(p).get('plater_id', '?')}: {sum(groups.values())} objects ({mix})")
    except (KeyError, ET.ParseError):
        pass
    try:
        ps = json.loads(z.read("Metadata/project_settings.config"))
        if ps.get("printer_settings_id"):
            lines.append(f"printer: {ps['printer_settings_id']}")
        colors = [(norm_hex(c), t) for c, t in zip(ps.get("filament_colour") or [], ps.get("filament_type") or [])]
        if colors:
            lines.append("filaments: " + ", ".join(f"{c} {t}" for c, t in colors))
            if palette:
                missing = [f"{c} {t}" for c, t in colors if match_palette(c, t, palette)[0] is None]
                if missing:
                    lines.append(f"not in the palette yet: {', '.join(missing)}")
    except (KeyError, ValueError):
        pass
    return lines


def _not_sliced(name: str, z: zipfile.ZipFile, palette: dict | None) -> ThreeMFError:
    details = [
        "To make a production plate: open it in the slicer, slice the plate, then use "
        "File > Export > Export plate sliced file. That saves a .gcode.3mf.",
        "A designer's project file like this one belongs to a model, not a plate.",
    ]
    summary = project_summary(z, palette)
    if summary:
        details += ["What's in this project:"] + summary
    return ThreeMFError(f"{name} is a slicer project, not a sliced plate: it has no slice data or G-code.", details)


def read_sliced(source: str | IO[bytes], name: str = "plate", palette: dict | None = None) -> SlicedPlate:
    """Time, weight, filament use, objects, printer and color-change count of one sliced plate."""
    try:
        z = zipfile.ZipFile(source)
    except zipfile.BadZipFile:
        raise ThreeMFError(f"{name} is not a 3MF file. In the slicer use File > Export > Export plate sliced file.")
    with z:
        names = z.namelist()
        if "Metadata/slice_info.config" not in names:
            raise _not_sliced(name, z, palette)
        for info in z.infolist():
            if info.file_size > 2 * 1024 * 1024 * 1024:
                raise ThreeMFError(f"{name} holds a file over 2 GB unpacked; that isn't a plate.")
        if z.getinfo("Metadata/slice_info.config").file_size > 50 * 1024 * 1024:
            raise ThreeMFError(f"{name}: its slice info is implausibly large.")
        root = ET.fromstring(z.read("Metadata/slice_info.config"))
        plates = root.findall("plate")
        if not plates:
            raise _not_sliced(name, z, palette)
        if len(plates) > 1:
            raise ThreeMFError(f"{name} holds {len(plates)} sliced plates. Export one plate per file, so each "
                               "production plate is locked on its own.")
        plate = plates[0]
        meta = _meta(plate)
        objects = [o.get("name", "?") for o in plate.findall("object") if o.get("skipped", "false") != "true"]
        filaments = [Filament(f.get("color", ""), f.get("type", ""), _num(f.get("used_g")))
                     for f in plate.findall("filament") if _num(f.get("used_g")) > 0]

        gname = f"Metadata/plate_{meta.get('index', '1')}.gcode"
        if gname not in names:
            gname = next((n for n in names if re.fullmatch(r"Metadata/plate_\d+\.gcode", n)), None)
        if not gname:
            raise ThreeMFError(f"{name} has no G-code inside, so the printer can't run it. Use "
                               "File > Export > Export plate sliced file, not Save Project.")
        # Filament changes: M620 S<slot> (S255 = unload); the first one is the initial load.
        # The G-code's config block names the printer it was made for.
        changes, gcode_printer = 0, ""
        with z.open(gname) as g:
            for raw in g:
                if raw.startswith(b"M620 S") and not raw.startswith(b"M620 S255"):
                    changes += 1
                elif raw.startswith(b"; printer_model ="):
                    gcode_printer = raw.split(b"=", 1)[1].strip().decode("utf-8", "replace")
        printer_model = ""
        if "Metadata/project_settings.config" in names:
            try:
                printer_model = json.loads(z.read("Metadata/project_settings.config")).get("printer_model") or ""
            except ValueError:
                pass
    return SlicedPlate(
        objects=objects,
        hours=round(_num(meta.get("prediction")) / 3600, 2),
        grams=round(_num(meta.get("weight")), 1),
        filaments=filaments,
        color_changes=max(changes - 1, 0),
        printer_model=printer_model or gcode_printer,
        printer_model_id=meta.get("printer_model_id") or "",
        nozzle=meta.get("nozzle_diameters") or "",
    )
