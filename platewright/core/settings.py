"""Deployment settings: wording, targets, printers, palette. Stored as JSON values."""

from __future__ import annotations

from dataclasses import dataclass, field

from platewright.core.palette import validate_palette

DEFAULTS: dict = {
    "unit_name": "part",
    "period_target": 0,  # good parts per month; 0 = no target
    "max_plate_hours": 14,
    "assumed_fail_rate": 0.03,
    "reorder_days": 21,
    "operator_group": "Platewright operators",
    "library_folder": "Production plates",
    "retired_folder": "Retired plates",
    "printers": {},
    "palette": {},
}


@dataclass(frozen=True)
class Settings:
    unit_name: str = "part"
    period_target: int = 0
    max_plate_hours: float = 14
    assumed_fail_rate: float = 0.03
    reorder_days: float = 21
    operator_group: str = "Platewright operators"
    library_folder: str = "Production plates"
    retired_folder: str = "Retired plates"
    printers: dict = field(default_factory=dict)
    palette: dict = field(default_factory=dict)

    @classmethod
    def from_values(cls, values: dict) -> Settings:
        v = {**DEFAULTS, **{k: values[k] for k in DEFAULTS if k in values}}
        return cls(
            unit_name=str(v["unit_name"]) or "part",
            period_target=int(v["period_target"] or 0),
            max_plate_hours=float(v["max_plate_hours"]),
            assumed_fail_rate=float(v["assumed_fail_rate"]),
            reorder_days=float(v["reorder_days"]),
            operator_group=str(v["operator_group"]),
            library_folder=str(v["library_folder"]),
            retired_folder=str(v["retired_folder"]),
            printers=dict(v["printers"]),
            palette=dict(v["palette"]),
        )

    def unit(self, n: int | None = None) -> str:
        return self.unit_name if n == 1 else self.unit_name + "s"

    @property
    def plates_per_day(self) -> float:
        return sum(float(p.get("plates_per_day", 1)) for p in self.printers.values()) or 1.0

    @property
    def printer_models(self) -> list[str]:
        return sorted({p["model"] for p in self.printers.values() if p.get("model")})

    def problems(self) -> list[str]:
        out = validate_palette(self.palette)
        if not self.printers:
            out.append("no printers set up")
        return out

    def printer_key(self, bambuddy_name: str | None) -> str:
        """The settings' printer for a printer name Bambuddy reports."""
        n = (bambuddy_name or "").lower()
        for key, info in self.printers.items():
            if n and n in (key.lower(), (info.get("bambuddy_name") or "").lower()):
                return key
        if len(self.printers) == 1:
            return next(iter(self.printers))
        return bambuddy_name or "?"


def from_toys_config(cfg: dict) -> dict:
    """Settings values from a toys.py config.json."""
    out = {k: cfg[k] for k in ("unit_name", "max_plate_hours", "assumed_fail_rate", "reorder_days",
                               "printers", "palette") if k in cfg}
    if "monthly_target" in cfg:
        out["period_target"] = cfg["monthly_target"]
    return out
