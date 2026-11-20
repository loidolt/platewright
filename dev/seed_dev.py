"""Fill a dev database with the reference settings, two models and plate N.

    PLATEWRIGHT_DEV=1 uv run python -m dev.seed_dev
Prints the plate's fingerprint; start the fake Bambuddy with FAKE_PLATE_SHA=<it> so its prints match.
"""

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

store = Store(config.load().db_path)
store.set_settings(SETTINGS)
if not store.models():
    store.add_model("nunu", "Nunu", status="qualified", distribution_ok="yes", min_age=3, small_parts="yes",
                    designer="@designer", license="Standard Digital File License", aliases=("Nunu body",))
    store.add_model("091-rowan", "Rowan", ref="91", status="new", distribution_ok="unknown")
data = sliced_3mf(objects=("Nunu",) * 44, filaments=(("#9D432CFF", "PLA", 180.0), ("#FFFFFFFF", "PLA", 40.0)),
                  prediction=23 * 3600, weight=220, changes=174)
sha = hashlib.sha256(data).hexdigest()
if not store.plates():
    s = read_sliced(io.BytesIO(data))
    store.register_plate("N", check_plate(s, store.core_models(), store.settings()), sha256=sha,
                         printer_model=s.printer_model, nozzle="0.4", bambuddy_file_id=2,
                         description="Nunu, 44-up overnight", by="dev")
print(sha)
