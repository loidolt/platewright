"""Phase 3: CSV import from toys.py, the webhook nudge, app messages, the home-screen manifest."""

import asyncio
import dataclasses
import datetime as dt
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from platewright.core.messages import demand_alerts, weekly_summary
from platewright.core.plan import ActivePlate, Demand, RunTotals, demand_plan, month_status
from platewright.importer import import_folder
from platewright.messages import Messenger
from platewright.store.db import Store
from platewright.web.app import create_app
from tests.test_phase2 import WritableBambuddy
from tests.test_web import CONFIG, SETTINGS, make_store, sign_in

TOYS = Path(__file__).parent / "fixtures" / "toys"  # written by toys.py itself, 2026-11-20


# -- import -----------------------------------------------------------------------------------

def test_import_reproduces_toys_py(tmp_path):
    store = Store(":memory:")
    rep = import_folder(TOYS, store, tmp_path)
    assert (rep["models"], rep["test_prints"], rep["plates"], rep["versions"], rep["runs"], rep["demands"],
            rep["stock_moves"], rep["plate_files"]) == (2, 2, 1, 2, 4, 1, 4, 2)
    # toys.py inventory said: Bear made 18, given 8, on hand 10; Fox made 3, adjusted +3, given 1, on hand 5
    s = store.stock()
    assert (s["007-bear"]["made"], s["007-bear"]["out"], s["007-bear"]["on_hand"]) == (18, 8, 10)
    assert (s["fox"]["made"], s["fox"]["adjusted"], s["fox"]["out"], s["fox"]["on_hand"]) == (3, 3, 1, 5)
    versions = store.plate_versions("A")
    assert [v.version for v in versions] == [2, 1] and versions[1].mix == {"007-bear": 6, "fox": 2}
    assert len(list((tmp_path / "plates").glob("*.gcode.3mf"))) == 2
    assert all(not r.reviewed_at is None for r in store.runs())  # handled in toys.py already
    (d,) = store.open_demands()
    assert d.slug == "fair" and d.target == 30 and d.needs == {"007-bear": 10}
    assert store.delivered() == {"fair": {"007-bear": 5}}
    assert store.settings().unit_name == "toy" and store.settings().period_target == 500
    fox = store.model("fox")
    assert fox["aliases"] == "Fox body" and fox["distribution_ok"] == "unknown" and fox["status"] == "qualified"
    # again: nothing twice
    again = import_folder(TOYS, store, tmp_path)
    assert (again["models"], again["runs"], again["versions"], again["stock_moves"]) == (0, 0, 0, 0)
    assert store.stock()["007-bear"]["on_hand"] == 10


def test_same_day_hand_logged_runs_both_import(tmp_path):
    import shutil
    folder = tmp_path / "toys"
    shutil.copytree(TOYS, folder)
    runs = folder / "03_runs" / "runs.csv"
    lines = runs.read_text().splitlines()
    runs.write_text("\n".join(lines + [lines[2]]) + "\n")  # the 2026-11-02 run, twice
    store = Store(":memory:")
    assert import_folder(folder, store, tmp_path / "data")["runs"] == 5
    assert import_folder(folder, store, tmp_path / "data")["runs"] == 0


def test_import_dry_run_changes_nothing(tmp_path):
    store = Store(":memory:")
    rep = import_folder(TOYS, store, tmp_path, dry_run=True)
    assert rep["runs"] == 4 and not store.models() and not store.runs() and not store.settings_values()
    assert not (tmp_path / "plates").exists()


# -- webhook ------------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    fake = WritableBambuddy()
    cfg = dataclasses.replace(CONFIG, data_dir=str(tmp_path), webhook_secret="s3cret")
    store = make_store()
    app = create_app(cfg, transport=httpx.MockTransport(fake.handler), store=store)
    with TestClient(app, base_url="https://plates.example.test", follow_redirects=False) as c:
        c.fake, c.store = fake, store
        yield c


def test_webhook_needs_the_secret_and_schedules_a_sync(client, monkeypatch):
    from platewright.web.routes import hooks
    monkeypatch.setattr(hooks, "DELAY", 0)
    assert client.post("/hooks/bambuddy", json={"event": "print_complete"}).status_code == 401
    assert client.post("/hooks/bambuddy", json={}, headers={"Authorization": "Bearer nope"}).status_code == 401
    calls_before = sum(c == "/api/v1/print-log/" for c in client.fake.calls)
    r = client.post("/hooks/bambuddy", json={"event": "print_complete"}, headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 202 and r.json()["sync"]
    r = client.post("/hooks/bambuddy", json={"event": "printer_offline"}, headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200 and not r.json()["sync"]
    for _ in range(50):
        if sum(c == "/api/v1/print-log/" for c in client.fake.calls) > calls_before:
            break
        asyncio.run(asyncio.sleep(0.02))
    assert sum(c == "/api/v1/print-log/" for c in client.fake.calls) > calls_before
    assert client.store.state("webhook")["event"] == "printer_offline"


def test_webhook_off_without_a_secret(tmp_path):
    app = create_app(dataclasses.replace(CONFIG, data_dir=str(tmp_path)), store=make_store(),
                     transport=httpx.MockTransport(WritableBambuddy().handler))
    with TestClient(app, base_url="https://plates.example.test") as c:
        assert c.post("/hooks/bambuddy", json={}).status_code == 404


# -- messages -----------------------------------------------------------------------------------

def plan_for(days_left: int, need: int):
    from platewright.core.settings import Settings
    s = Settings.from_values(SETTINGS)
    today = dt.date(2026, 11, 20)
    d = Demand(1, "fair", "Spring fair", (today + dt.timedelta(days=days_left)).isoformat(), 0, {"nunu": need})
    return demand_plan([d], {}, {}, [ActivePlate("N", 4, 5.0, {"nunu": 4})], 0.0, s, today), s


def test_demand_alert_once_per_risk():
    plans, s = plan_for(2, 40)  # 10 plates, 2 slots
    alerts, state = demand_alerts(plans, {}, s)
    assert len(alerts) == 1 and "40 toys short" in alerts[0]["title"] and "plate slots" in alerts[0]["message"]
    again, state2 = demand_alerts(plans, state, s)
    assert again == [] and state2 == state
    ok, _ = plan_for(30, 4)
    assert demand_alerts(ok, state, s) == ([], {})


def test_weekly_summary_text():
    from platewright.core.settings import Settings
    s = Settings.from_values(SETTINGS)
    today = dt.date(2026, 11, 23)
    runs = [RunTotals("2026-11-20", 44, 42), RunTotals("2026-11-01", 44, 44)]
    m = month_status(runs, [ActivePlate("N", 44, 23.0, {})], s, today)
    plans, _ = plan_for(2, 40)
    text = weekly_summary(today, runs, m, 3, plans, s)["message"]
    assert "42 good toys from 1 plate, 2 bad" in text and "86 of 500" in text and "3 finished prints to review" in text
    assert "Spring fair" in text and "at risk" in text


def test_messenger_sends_alerts_and_the_summary_once(client):
    sent = []
    real = client.fake.handler

    def capture(request):
        if request.url.path == "/api/v1/notifications/app-message":
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"channels": 1})
        return real(request)
    m = Messenger(client.app.state.ctx.bambuddy, client.store, "https://plates.example.test")
    client.app.state.ctx.bambuddy._http._transport = httpx.MockTransport(capture)
    client.store.save_demand("fair", name="Spring fair", kind="event", due=dt.date.today().isoformat(), target=0,
                             notes="", needs={"nunu": 50})
    monday_9 = dt.datetime.combine(dt.date.today() - dt.timedelta(days=dt.date.today().weekday()), dt.time(9)).astimezone()
    titles = asyncio.run(m.tick(monday_9))
    assert any("Spring fair" in t for t in titles) and "Platewright: the week" in titles
    assert sent[0]["url"].startswith("https://plates.example.test/demand/")
    assert asyncio.run(m.tick(monday_9)) == []  # nothing new
    client.store.set_settings({"messages": False})
    client.store.set_state("summary_week", None)
    assert asyncio.run(m.tick(monday_9)) == []


def test_test_message_button_reports_missing_channel(client):
    sign_in(client, client.fake)
    real = client.fake.handler
    client.app.state.ctx.bambuddy._http._transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"channels": 0}) if r.url.path.endswith("/app-message") else real(r))
    r = client.post("/settings/test-message")
    assert "Messages+from+connected+apps" in r.headers["location"]
    assert "Messages from connected apps" in client.get("/settings").text


# -- home screen -------------------------------------------------------------------------------

def test_manifest_and_icons(client):
    m = client.get("/manifest.webmanifest")
    assert m.headers["content-type"].startswith("application/manifest+json")
    icons = m.json()["icons"]
    assert m.json()["display"] == "browser" and len(icons) == 3
    for i in icons:
        r = client.get(i["src"])
        assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get("/favicon.ico", follow_redirects=True).content[:4] == b"\x89PNG"
