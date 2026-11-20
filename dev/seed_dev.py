"""Fill a dev database with sample settings, models, plates, runs, demand and stock.

    PLATEWRIGHT_DEV=1 uv run python -m dev.seed_dev
Prints plate N's fingerprint. It also writes ./data/fake_bambuddy.json with every plate's
fingerprint, which dev/fake_bambuddy.py reads so its sample prints match the plates, and
./data/samples/ with sliced files to try "New plate" with.
"""

import datetime as dt
import hashlib
import io
import json
from pathlib import Path

from platewright import config
from platewright.core.plates import check_plate
from platewright.core.settings import from_toys_config
from platewright.core.threemf import read_sliced
from platewright.store.db import Store
from tests.threemf_factory import sliced_3mf

SETTINGS = from_toys_config(json.loads((Path(__file__).parent / "sample_config.json").read_text()))
BROWN, WHITE, RED = "#9D432CFF", "#FFFFFFFF", "#C12E1FFF"

cfg = config.load()
store = Store(cfg.db_path)
store.set_settings(SETTINGS)
today = dt.date.today()


def day(offset: int) -> str:
    return (today + dt.timedelta(days=offset)).isoformat()


PLATES = {
    # code: (objects, filaments, hours, grams, color changes, File Manager id, description)
    "N": (("Nunu",) * 44, ((BROWN, "PLA", 180.0), (WHITE, "PLA", 40.0)), 23, 220, 174, 2,
          "Nunu, 44-up overnight"),
    "MX": (("Nunu",) * 12 + ("Star",) * 12, ((BROWN, "PLA", 60.0), (RED, "PLA", 36.0)), 9, 96, 40, 4,
           "Mixed day plate"),
    "S": (("Star",) * 30, ((RED, "PLA", 75.0),), 7, 75, 0, None, "Stars, 30-up"),
}

if not store.models():
    store.add_model("nunu", "Nunu", status="qualified", distribution_ok="yes", min_age=3, small_parts="yes",
                    designer="@designer", license="Standard Digital File License", aliases=("Nunu body",),
                    url="https://makerworld.com/en/models/123456", bambuddy_file_id=11)
    store.add_model("star", "Star", status="qualified", distribution_ok="yes", min_age=3, small_parts="no",
                    designer="@stargazer", license="CC BY 4.0", bambuddy_file_id=12)
    store.add_model("091-rowan", "Rowan", ref="91", status="new", distribution_ok="unknown")
    store.add_model("fox", "Fox", status="failed-test", distribution_ok="no", license="CC BY-NC-ND 4.0",
                    designer="@foxy", notes="Ears snap off when the supports come away.")
    store.qualify("nunu", "pass", "supports came off clean, colors right", by="devadmin")
    store.qualify("star", "pass", "", by="devadmin")
    store.qualify("fox", "fail", "ears snap off", by="devadmin")

shas: dict[str, str] = {}
for code, (objects, fil, hours, grams, changes, file_id, desc) in PLATES.items():
    data = sliced_3mf(objects=objects, filaments=fil, prediction=hours * 3600, weight=grams, changes=changes)
    shas[code] = hashlib.sha256(data).hexdigest()
    if store.plate(code) is None:
        s = read_sliced(io.BytesIO(data))
        store.register_plate(code, check_plate(s, store.core_models(), store.settings()), sha256=shas[code],
                             printer_model=s.printer_model, nozzle="0.4", bambuddy_file_id=file_id,
                             description=desc, by="devadmin")

if store.plate("OLD") is None:
    old = sliced_3mf(objects=("Nunu",) * 36, filaments=((BROWN, "PLA", 150.0),), prediction=20 * 3600, weight=150)
    s = read_sliced(io.BytesIO(old))
    vid = store.register_plate("OLD", check_plate(s, store.core_models(), store.settings()),
                               sha256=hashlib.sha256(old).hexdigest(), printer_model=s.printer_model,
                               nozzle="0.4", description="First Nunu plate, 36-up", by="devadmin")
    store.add_manual_run(vid, day(-40), "X1C", "ok", failures={"nunu": 2}, by="devadmin")
    store.set_plate_active("OLD", False)

if not store.demands():
    store.save_demand("spring-fair", name="Spring fair", kind="event", due=day(9), target=300, notes="Booth 4",
                      needs={"nunu": 200, "star": 60}, by="devadmin")
    store.save_demand("school-order", name="School order", kind="order", due=day(30), target=0, notes="",
                      needs={"star": 40}, by="devadmin")
    store.save_demand("restock", name="Shelf restock", kind="restock", due=day(60), target=50, notes="",
                      needs={}, by="devadmin")
    store.save_demand("winter-market", name="Winter market", kind="event", due=day(-20), target=80, notes="",
                      needs={}, by="devadmin")
    store.set_demand_status("winter-market", "done")

if not store.ledger():
    n = store.plate("N")
    store.add_manual_run(n.version_id, day(-3), "X1C", "ok", failures={"nunu": 1}, by="devadmin")
    store.add_manual_run(n.version_id, day(-2), "X1C", "aborted", aborted_pct=40, notes="spaghetti", by="devadmin")
    store.add_ledger(day(-30), "adjust", "nunu", 20, notes="stock from before Platewright", by="devadmin")
    store.add_ledger(day(-20), "out", "nunu", 30, demand_slug="winter-market", by="devadmin")
    store.add_ledger(day(-1), "out", "nunu", 12, notes="library display", by="devadmin")

samples = Path(cfg.data_dir) / "samples"
samples.mkdir(parents=True, exist_ok=True)
(samples / "ok-nunu-40up.gcode.3mf").write_bytes(
    sliced_3mf(objects=("Nunu",) * 40, filaments=((BROWN, "PLA", 170.0),), prediction=21 * 3600, weight=170))
(samples / "unknown-name.gcode.3mf").write_bytes(
    sliced_3mf(objects=("Nunu",) * 20 + ("Bunny v2",) * 10,
               filaments=((BROWN, "PLA", 90.0), ("#123456FF", "PLA", 20.0)), prediction=12 * 3600, weight=110))
(samples / "wrong-printer.gcode.3mf").write_bytes(
    sliced_3mf(objects=("Star",) * 10, printer_model="Bambu Lab A1 mini"))

(Path(cfg.data_dir) / "fake_bambuddy.json").write_text(json.dumps({"plates": shas}, indent=2))
print(shas["N"])
