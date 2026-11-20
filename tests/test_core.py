"""Behaviors carried over from toys.py."""

import datetime as dt
import io

import pytest

from platewright.core.mix import ANY, UNLINKED, distribute, fit_mix, good_by_model
from platewright.core.names import (ModelNames, base_object_name, build_index, lookup, lookup_object,
                                    model_slug, norm_key)
from platewright.core.palette import match_palette, norm_hex, validate_palette
from platewright.core.plan import ActivePlate, Demand, RunTotals, demand_plan, filament, month_status
from platewright.core.plates import Model, check_plate
from platewright.core.runs import VersionRef, match_version, synced_run
from platewright.core.settings import Settings, from_toys_config
from platewright.core.threemf import ThreeMFError, read_sliced
from tests.threemf_factory import project_3mf, sliced_3mf

PALETTE = {
    "White": {"hex": "#FFFFFF", "material": "PLA"},
    "Body-Brown": {"hex": "#9D432C", "material": "PLA", "slot": "A3"},
    "Black": {"hex": "#000000", "material": "PLA"},
}
SETTINGS = Settings.from_values({
    "unit_name": "toy", "period_target": 500, "max_plate_hours": 24,
    "printers": {"X1C": {"model": "Bambu Lab X1 Carbon", "plates_per_day": 1}},
    "palette": PALETTE,
})
NUNU = Model("nunu", "Nunu", status="qualified", distribution_ok="yes")


# -- names --------------------------------------------------------------------------------

def test_copy_numbered_slicer_names():
    assert base_object_name("gecko cut.stl_A 7 3") == "gecko cut.stl_A"
    idx = build_index([ModelNames("gecko", "Gecko", aliases=("gecko cut.stl_A",))])
    assert lookup_object("gecko cut.stl_A 7 3", idx) == "gecko"
    assert lookup_object("gecko cut.stl_A", idx) == "gecko"
    assert lookup_object("lizard", idx) is None


def test_refs_match_without_leading_zeros():
    assert norm_key("091") == norm_key("91") == "91"
    assert model_slug("Rowan", "91") == "091-rowan"
    idx = build_index([ModelNames("091-rowan", "Rowan", "91")])
    assert lookup("91", idx) == lookup("rowan", idx) == "091-rowan"


def test_ambiguous_key_matches_nothing():
    idx = build_index([ModelNames("a", "Bear"), ModelNames("b", "Other", aliases=("Bear",))])
    assert lookup("bear", idx) is None


# -- palette ----------------------------------------------------------------------------

def test_palette_matching():
    assert norm_hex("9d432cff") == "#9D432C"
    assert match_palette("#9D432CFF", "PLA", PALETTE) == ("Body-Brown", "")
    name, warn = match_palette("#9D432C", "PETG", PALETTE)
    assert name == "Body-Brown" and "PETG" in warn
    assert match_palette("#123456", "PLA", PALETTE) == (None, "")


def test_palette_duplicates_refused():
    errors = validate_palette({"A": {"hex": "#FFFFFF", "material": "PLA"}, "B": {"hex": "#ffffff", "material": "pla"}})
    assert errors and "same hex" in errors[0]


def test_toys_config_import():
    v = from_toys_config({"monthly_target": 500, "unit_name": "toy", "palette": PALETTE})
    assert v["period_target"] == 500 and Settings.from_values(v).unit(2) == "toys"


# -- mix ------------------------------------------------------------------------------------

def test_largest_remainder_split():
    assert distribute(10, {"a": 1, "b": 1, "c": 1}) == {"a": 4, "b": 3, "c": 3}
    assert sum(distribute(44, {"a": 30, "b": 14}).values()) == 44
    assert distribute(0, {"a": 1}) == {"a": 0}
    assert fit_mix({}, 5) == {UNLINKED: 5}


def test_good_parts_by_model():
    planned = {"nunu": 30, "rowan": 14}
    assert good_by_model(planned, "ok") == planned
    assert good_by_model(planned, "rejected") == {"nunu": 0, "rowan": 0}
    assert good_by_model(planned, "ok", {"rowan": 2}) == {"nunu": 30, "rowan": 12}
    spread = good_by_model(planned, "ok", {ANY: 4})
    assert sum(spread.values()) == 40
    assert good_by_model(planned, "ok", {"rowan": 99})["rowan"] == 0


# -- 3MF ------------------------------------------------------------------------------------

def test_read_sliced_plate():
    s = read_sliced(io.BytesIO(sliced_3mf(changes=3)))
    assert s.objects == ["Nunu", "Nunu 2", "Nunu 3"]
    assert s.hours == 5.0 and s.grams == 40.0
    assert s.color_changes == 3
    assert s.printer_model == "Bambu Lab X1 Carbon" and s.printer_model_id == "BL-P001"


def test_project_file_explained():
    with pytest.raises(ThreeMFError) as e:
        read_sliced(io.BytesIO(project_3mf()), "nunu.3mf", PALETTE)
    assert "not a sliced plate" in str(e.value)
    assert any("'Nunu' x2" in line for line in e.value.details)
    assert any("not in the palette" in line for line in e.value.details)


def test_multiple_plates_and_missing_gcode_refused():
    with pytest.raises(ThreeMFError, match="2 sliced plates"):
        read_sliced(io.BytesIO(sliced_3mf(plates=2)))
    with pytest.raises(ThreeMFError, match="no G-code"):
        read_sliced(io.BytesIO(sliced_3mf(gcode=False)))
    with pytest.raises(ThreeMFError, match="not a 3MF"):
        read_sliced(io.BytesIO(b"nope"))


# -- plate checks ---------------------------------------------------------------------------

def test_plate_check_passes():
    s = read_sliced(io.BytesIO(sliced_3mf()))
    c = check_plate(s, [NUNU], SETTINGS)
    assert c.ok, c.blockers
    assert c.units == 3 and c.model_units == {"nunu": 3}
    assert c.filaments == {"Body-Brown": 30.0}
    assert c.linked == {"nunu": {"Nunu": 3}}


def test_plate_check_blocks_untested_unlinked_unmapped_and_wrong_printer():
    s = read_sliced(io.BytesIO(sliced_3mf(objects=("Nunu", "Mystery"), filaments=(("#123456", "PLA", 5),),
                                          printer_model="Bambu Lab H2C")))
    c = check_plate(s, [Model("nunu", "Nunu", status="new")], SETTINGS)
    text = " ".join(c.blockers)
    assert "not test-printed" in text and "'Mystery' x1" in text and "#123456" in text and "H2C" in text
    assert c.model_units == {"nunu": 1, UNLINKED: 1}
    relaxed = check_plate(s, [Model("nunu", "Nunu", status="new")], SETTINGS, allow_untested=True,
                          allow_unmapped=True)
    assert len(relaxed.blockers) == 1 and "H2C" in relaxed.blockers[0]  # nothing overrides the printer


def test_parts_per_unit_and_count_override():
    s = read_sliced(io.BytesIO(sliced_3mf(objects=("Bear head", "Bear head 2", "Bear head 3"))))
    bear = Model("bear", "Bear", status="qualified", distribution_ok="yes", parts_per_unit=2, aliases=("Bear head",))
    c = check_plate(s, [bear], SETTINGS)
    assert c.model_units == {"bear": 1} and any("not a multiple" in w for w in c.warnings)
    assert check_plate(s, [bear], SETTINGS, units=4).model_units == {"bear": 4}


def test_license_and_hours_warnings():
    s = read_sliced(io.BytesIO(sliced_3mf(prediction=3600 * 30)))
    c = check_plate(s, [Model("nunu", "Nunu", status="qualified", distribution_ok="unknown")], SETTINGS)
    assert c.ok and any("license" in w for w in c.warnings) and any("30.0 h" in w for w in c.warnings)


# -- sync matching ------------------------------------------------------------------------

REF = VersionRef(version_id=7, plate_code="N", version=2, sha256="abc", hours=5.0, grams=40.0,
                 filaments={"Body-Brown": 30.0, "White": 10.0})


def test_match_by_fingerprint_then_name():
    by_hash = {"abc": REF}
    assert match_version({}, {"content_hash": "abc"}, by_hash, {}) == (REF, False)
    assert match_version({"print_name": "PLATE-N_44up"}, {}, {}, {"N": REF}) == (REF, True)
    assert match_version({"print_name": "benchy"}, {"content_hash": "zzz"}, by_hash, {"N": REF}) is None


def test_synced_run_outcomes():
    today = dt.date(2026, 11, 20)
    done = {"id": 5, "status": "completed", "completed_at": "2026-11-19T06:00:00Z", "duration_seconds": 18000}
    r = synced_run(done, REF, "X1C", False, today)
    assert r.outcome == "ok" and r.verdict == "" and r.hours == 5.0 and r.aborted_pct is None
    assert synced_run({**done, "user_verdict": "reject"}, REF, "X1C", False, today).outcome == "rejected"
    stopped = synced_run({"id": 6, "status": "stopped", "filament_used_grams": 10}, REF, "X1C", False, today)
    assert stopped.outcome == "stopped" and stopped.aborted_pct == 25 and stopped.filaments["Body-Brown"] == 7.5
    assert synced_run({"id": 7, "status": "printing"}, REF, "X1C", False, today) is None


# -- planning -----------------------------------------------------------------------------

def test_month_pace():
    today = dt.date(2026, 11, 20)
    runs = [RunTotals("2026-11-02", 44, 42, 23.0), RunTotals("2026-10-30", 44, 44)]
    st = month_status(runs, [ActivePlate("N", 44, 23.0, {"nunu": 44})], SETTINGS, today)
    assert (st.good, st.planned, st.days_left, st.remaining) == (42, 44, 11, 458)
    # 2 of 44 failed, so 42 good per plate: 458 / 42 -> 11 plates in 11 slots
    assert st.plates_needed == 11 and not st.behind
    late = month_status(runs, [ActivePlate("N", 44, 23.0, {"nunu": 44})], SETTINGS, dt.date(2026, 11, 25))
    assert late.days_left == 6 and late.behind


def test_demands_served_earliest_first():
    today = dt.date(2026, 11, 20)
    plates = [ActivePlate("N", 44, 23.0, {"nunu": 44})]
    late = Demand(2, "spring", "Spring", "2027-03-01", 0, {"nunu": 30})
    early = Demand(1, "xmas", "Christmas", "2026-12-20", 50, {"nunu": 20})
    plan = demand_plan([late, early], {}, {"nunu": 40}, plates, 0.03, SETTINGS, today)
    assert [p.demand.slug for p in plan] == ["xmas", "spring"]
    xmas, spring = plan
    assert xmas.covered == 40 and xmas.short == 10 and xmas.plates == 1
    assert spring.covered == 0 and spring.short == 30 and spring.cumulative_plates == 2
    assert not xmas.at_risk


def test_demand_with_no_plate_is_at_risk():
    d = Demand(1, "fair", "Fair", "2026-12-01", 0, {"rowan": 10})
    (p,) = demand_plan([d], {}, {}, [ActivePlate("N", 44, 23.0, {"nunu": 44})], 0.03, SETTINGS, dt.date(2026, 11, 20))
    assert p.no_plate == ["rowan"] and p.at_risk


def test_filament_days_left():
    spools = [{"rgba": "9D432CFF", "material": "PLA", "label_weight": 1000, "weight_used": 400},
              {"rgba": "FF00FFFF", "material": "PLA", "label_weight": 1000, "weight_used": 0},
              {"rgba": "FFFFFFFF", "material": "PLA", "archived_at": "2026-01-01"}]
    runs = [RunTotals("2026-10-22", 44, 44, filaments={}), RunTotals("2026-11-10", 44, 44, filaments={"Body-Brown": 300.0})]
    rows, other = filament(spools, runs, SETTINGS, dt.date(2026, 11, 20))
    brown = next(r for r in rows if r.color == "Body-Brown")
    assert brown.grams == 600 and brown.per_day == 10.0 and brown.days_left() == 60
    white = next(r for r in rows if r.color == "White")
    assert white.grams == 0 and white.reorder(21)
    assert len(other) == 1 and other[0]["grams_left"] == 1000
    # three days of history: use per day over those three days, not over 30
    short = [RunTotals("2026-11-18", 44, 44, filaments={"Body-Brown": 300.0})]
    rows, _ = filament(spools, short, SETTINGS, dt.date(2026, 11, 20))
    assert next(r for r in rows if r.color == "Body-Brown").per_day == 100.0


def test_pace_ignores_the_fail_rate_of_a_tiny_sample():
    runs = [RunTotals("2026-11-02", 1, 0)]
    st = month_status(runs, [ActivePlate("N", 44, 23.0, {"nunu": 44})], SETTINGS, dt.date(2026, 11, 20))
    assert st.plates_needed == 12  # 500 / (44 * 0.97), not 500 / 1


def test_plates_for_different_models_add_up():
    plates = [ActivePlate("X", 10, 5.0, {"a": 10}), ActivePlate("Y", 10, 5.0, {"b": 10})]
    d = Demand(1, "fair", "Fair", "2027-01-01", 0, {"a": 30, "b": 20})
    (p,) = demand_plan([d], {}, {}, plates, 0.0, SETTINGS, dt.date(2026, 11, 20))
    assert p.plates_by_model == {"a": (3, "X"), "b": (2, "Y")} and p.plates == 5


def test_zero_grams_on_a_finished_print_means_unknown():
    r = synced_run({"id": 8, "status": "completed", "filament_used_grams": 0}, REF, "X1C", False, dt.date(2026, 11, 20))
    assert r.grams == 40.0 and r.filaments == {"Body-Brown": 30.0, "White": 10.0}
