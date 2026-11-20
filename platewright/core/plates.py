"""Checks a sliced plate must pass before it is locked for production."""

from __future__ import annotations

from dataclasses import dataclass, field

from platewright.core.mix import UNLINKED, distribute
from platewright.core.names import ModelNames, base_object_name, build_index, lookup_object
from platewright.core.palette import match_palette, norm_hex
from platewright.core.settings import Settings
from platewright.core.threemf import SlicedPlate


@dataclass(frozen=True)
class Model:
    """What the checks need to know about a registered model."""

    slug: str
    name: str
    status: str = "new"  # new | qualified | failed-test
    distribution_ok: str = "unknown"  # yes | no | unknown
    parts_per_unit: int = 1
    ref: str = ""
    aliases: tuple[str, ...] = ()

    @property
    def names(self) -> ModelNames:
        return ModelNames(self.slug, self.name, self.ref, self.aliases)


def license_warning(m: Model) -> str | None:
    if m.distribution_ok != "yes":
        return (f"{m.name}: the license isn't confirmed to allow handing out prints "
                f"(distribution: {m.distribution_ok or 'unknown'}). Check the model page.")
    return None


def printer_mismatch(plate_printer: str, settings: Settings) -> str | None:
    """Explain the problem when a file was set up for a printer model you don't have."""
    known = settings.printer_models
    if not known or not plate_printer or plate_printer in known:
        return None
    return (f"set up for a {plate_printer}, but your printers are {', '.join(known)}. G-code made for one "
            f"printer model doesn't print right on another. Choose your printer ({known[0]}) in the slicer, "
            "check the filament mapping, then slice and export again.")


@dataclass
class PlateCheck:
    units: int
    model_units: dict[str, int]  # parts per model slug (UNLINKED for unlinked objects)
    objects_by_model: dict[str, int]  # slicer objects per model slug
    linked: dict[str, dict[str, int]]  # slug -> slicer base name -> objects
    unmatched: dict[str, int]  # slicer base name -> objects
    filaments: dict[str, float]  # palette name (or hex) -> grams
    hours: float
    grams: float
    color_changes: int
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blockers

    @property
    def min_per_unit(self) -> float:
        return round(self.hours * 60 / self.units, 1) if self.units else 0.0

    @property
    def g_per_unit(self) -> float:
        return round(self.grams / self.units, 2) if self.units else 0.0


def check_plate(s: SlicedPlate, models: list[Model], settings: Settings, *, units: int | None = None,
                allow_untested: bool = False, allow_unmapped: bool = False) -> PlateCheck:
    warnings: list[str] = []
    blockers: list[str] = []
    word = settings.unit(1)

    # colors -> palette names
    filaments: dict[str, float] = {}
    unmapped = []
    for f in s.filaments:
        name, note = match_palette(f.hex, f.type, settings.palette)
        if note:
            warnings.append(note)
        if name is None:
            name = norm_hex(f.hex) or "unknown"
            unmapped.append(f"{name} {f.type}".strip())
        filaments[name] = round(filaments.get(name, 0) + f.grams, 2)
    if unmapped:
        msg = (f"colors not in the palette: {', '.join(unmapped)}. Add them (hex as the slicer shows it) or "
               "re-slice with palette filaments, so filament use stays right.")
        (warnings if allow_unmapped else blockers).append(msg)

    # slicer objects -> registered models
    idx = build_index([m.names for m in models])
    by_slug = {m.slug: m for m in models}
    counts: dict[str, int] = {}
    unmatched: dict[str, int] = {}
    linked: dict[str, dict[str, int]] = {}
    for obj in s.objects:
        slug = lookup_object(obj, idx)
        base = base_object_name(obj)  # numbered copies counted under one name
        if slug:
            counts[slug] = counts.get(slug, 0) + 1
            linked.setdefault(slug, {})
            linked[slug][base] = linked[slug].get(base, 0) + 1
        else:
            unmatched[base] = unmatched.get(base, 0) + 1

    model_units: dict[str, int] = {}
    for slug, n in counts.items():
        ppu = max(by_slug[slug].parts_per_unit, 1)
        if n % ppu:
            warnings.append(f"{by_slug[slug].name}: {n} objects is not a multiple of its {ppu} parts per {word}")
        model_units[slug] = n // ppu
    if unmatched:
        model_units[UNLINKED] = sum(unmatched.values())
    total = units or sum(model_units.values())
    if total <= 0:
        blockers.append(f"could not count the {settings.unit()} on the plate; give the count by hand")
    elif sum(model_units.values()) != total:  # count given by hand: spread it over the same mix
        model_units = distribute(total, model_units) if model_units else {UNLINKED: total}

    untested = sorted(by_slug[sl].name for sl in counts if by_slug[sl].status != "qualified")
    if untested:
        msg = f"not test-printed yet: {', '.join(untested)}"
        (warnings if allow_untested else blockers).append(msg)
    if unmatched:
        names = ", ".join(f"'{n}' x{c}" for n, c in sorted(unmatched.items()))
        msg = f"slicer objects not linked to a registered model: {names} (numbered copies counted together)"
        (warnings if allow_untested else blockers).append(msg)
    for sl in counts:
        w = license_warning(by_slug[sl])
        if w:
            warnings.append(w)
    if s.hours > settings.max_plate_hours:
        warnings.append(f"{s.hours} h is longer than the {settings.max_plate_hours:g} h limit; "
                        "it won't finish overnight")
    bad = printer_mismatch(s.printer_model, settings)
    if bad:
        blockers.append(f"this plate is {bad}")  # nothing overrides this one
    known_ids = {p.get("model_id") for p in settings.printers.values() if p.get("model_id")}
    if not s.printer_model and known_ids and s.printer_model_id and s.printer_model_id not in known_ids:
        warnings.append(f"sliced for printer model {s.printer_model_id}, which none of your printers is")

    return PlateCheck(units=max(total, 0), model_units=model_units, objects_by_model=counts, linked=linked,
                      unmatched=unmatched, filaments=filaments, hours=s.hours, grams=s.grams,
                      color_changes=s.color_changes, warnings=warnings, blockers=blockers)
