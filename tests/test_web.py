import asyncio
import dataclasses
import datetime as dt
import hashlib
import io
import json
import logging
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from platewright.bambuddy.client import Bambuddy, s256
from platewright.bambuddy.sync import sync_once
from platewright.config import Settings as AppConfig
from platewright.core.plates import check_plate
from platewright.core.threemf import read_sliced
from platewright.store.db import Store
from platewright.web.app import RedactCallbackQuery, _safe_next, create_app
from tests.threemf_factory import sliced_3mf

CONFIG = AppConfig(
    public_url="https://plates.example.test",
    bambuddy_url="https://print.example.test",
    bambuddy_api_url="http://10.0.0.5:3700",
    bambuddy_key="bb_testkey",
    client_id="bba_client",
    client_secret="bbs_secret",
    session_key="x" * 40,
    cookie_secure=False,
    session_days=30,
    sync_interval=3600,
    data_dir="unused",
    seed_file="",
)
SETTINGS = {
    "unit_name": "toy", "period_target": 500, "max_plate_hours": 24,
    "printers": {"X1C": {"model": "Bambu Lab X1 Carbon", "plates_per_day": 1}},
    "palette": {"Body-Brown": {"hex": "#9D432C", "material": "PLA", "slot": "A3"},
                "White": {"hex": "#FFFFFF", "material": "PLA", "slot": "A1"}},
}
USER = {"id": 7, "username": "chris", "email": "c@example.test", "is_admin": True,
        "groups": ["Administrators"], "permissions": ["a:read", "b:update"]}
PLATE_FILE = sliced_3mf(objects=("Nunu",) * 4)
PLATE_SHA = hashlib.sha256(PLATE_FILE).hexdigest()
TODAY = dt.date.today()


class FakeBambuddy:
    """The endpoints Platewright calls, with Bambuddy v1.3.1's shapes and checks."""

    def __init__(self):
        self.challenges: dict[str, str] = {}
        self.badges: list[dict] = []
        self.badge_status = 200
        self.token_error: str | None = None
        self.entries: list[dict] = []
        self.archives: dict[int, dict] = {}
        self.spools: list[dict] = []
        self.calls: list[str] = []

    def issue(self, authorize_url: str) -> tuple[str, str]:
        q = parse_qs(urlsplit(authorize_url).query)
        code = f"code-{len(self.challenges)}"
        self.challenges[code] = q["code_challenge"][0]
        return code, q["state"][0]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if path == "/api/v1/connect/token":
            body = json.loads(request.content)
            if self.token_error:
                return httpx.Response(400, json={"detail": {"error": self.token_error}})
            assert body["redirect_uri"] == "https://plates.example.test/auth/callback"
            challenge = self.challenges.pop(body["code"], None)
            if challenge is None or s256(body["code_verifier"]) != challenge:
                return httpx.Response(400, json={"detail": {"error": "invalid_grant"}})
            return httpx.Response(200, json={"user": USER, "issued_at": "2026-11-20T00:00:00Z"})
        if request.headers.get("X-API-Key") != "bb_testkey":
            return httpx.Response(401, json={"detail": "no key"})
        if path == "/api/v1/external-links/badge":
            self.badges.append(json.loads(request.content))
            return httpx.Response(self.badge_status, json={"links": 1} if self.badge_status == 200 else {})
        if path == "/api/v1/print-log/":
            off = int(request.url.params.get("offset", 0))
            return httpx.Response(200, json={"items": self.entries[off:off + 200], "total": len(self.entries)})
        if path.startswith("/api/v1/archives/"):
            a = self.archives.get(int(path.rsplit("/", 1)[1]))
            return httpx.Response(200, json=a) if a else httpx.Response(404, json={"detail": "nope"})
        if path == "/api/v1/inventory/spools":
            return httpx.Response(200, json=self.spools)
        if path == "/api/v1/printers/":
            return httpx.Response(200, json=[{"id": 1, "name": "X1C"}])
        if path == "/api/v1/printers/1/status":
            return httpx.Response(200, json={"connected": True, "state": "RUNNING", "subtask_name": "PLATE-N_4up",
                                             "progress": 42.0, "remaining_time": 125})
        return httpx.Response(404)


def make_store() -> Store:
    store = Store(":memory:")
    store.set_settings(SETTINGS)
    store.add_model("nunu", "Nunu", status="qualified", distribution_ok="yes", min_age=3, small_parts="no")
    return store


def lock_plate(store: Store, code="N") -> int:
    s = read_sliced(io.BytesIO(PLATE_FILE))
    check = check_plate(s, store.core_models(), store.settings())
    return store.register_plate(code, check, sha256=PLATE_SHA, printer_model=s.printer_model,
                                bambuddy_file_id=2, by="chris")


def entry(i, status="completed", verdict=None, archive_id=None, name="PLATE-N_4up", grams=None):
    return {"id": i, "status": status, "user_verdict": verdict, "archive_id": archive_id, "print_name": name,
            "printer_name": "X1C", "completed_at": f"{TODAY.isoformat()}T12:00:00Z", "duration_seconds": 18000,
            "filament_used_grams": grams}


@pytest.fixture
def fake():
    return FakeBambuddy()


@pytest.fixture
def store():
    return make_store()


@pytest.fixture
def client(fake, store, tmp_path):
    cfg = dataclasses.replace(CONFIG, data_dir=str(tmp_path))
    app = create_app(cfg, transport=httpx.MockTransport(fake.handler), store=store)
    with TestClient(app, base_url="https://plates.example.test", follow_redirects=False) as c:
        yield c


def sign_in(client, fake):
    r = client.get("/auth/login")
    code, state = fake.issue(r.headers["location"])
    return client.get("/auth/callback", params={"code": code, "state": state})


# -- sign-in (phase 0) ----------------------------------------------------------------------

def test_pages_redirect_to_sign_in_keeping_the_page(client):
    r = client.get("/filament")
    assert r.status_code == 303 and r.headers["location"] == "/auth/login?next=/filament"


def test_login_redirects_to_bambuddy_with_pkce(client):
    r = client.get("/auth/login")
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert r.headers["location"].startswith("https://print.example.test/connect/authorize?")
    assert q["redirect_uri"] == ["https://plates.example.test/auth/callback"]
    assert q["code_challenge_method"] == ["S256"] and len(q["code_challenge"][0]) == 43
    assert "SameSite=lax" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]


def test_full_sign_in_returns_to_the_page(client, fake):
    r = client.get("/auth/login", params={"next": "/models"})
    code, state = fake.issue(r.headers["location"])
    r = client.get("/auth/callback", params={"code": code, "state": state})
    assert r.status_code == 303 and r.headers["location"] == "/models"
    assert "chris" in client.get("/models").text
    assert "cookie sent: chris" in client.get("/whoami").text


def test_callback_refusals(client, fake):
    assert "cookie" in client.get("/auth/callback", params={"code": "c", "state": "s"}).text.lower()
    r = client.get("/auth/login")
    code, _ = fake.issue(r.headers["location"])
    assert client.get("/auth/callback", params={"code": code, "state": "forged"}).status_code == 400
    fake.token_error = "invalid_client"
    r = sign_in(client, fake)
    assert r.status_code == 502 and "invalid_client" in r.text


def test_frame_ancestors_only_bambuddy(client):
    for path in ("/", "/health", "/auth/callback"):
        h = client.get(path).headers
        assert h["content-security-policy"].endswith("frame-ancestors https://print.example.test")
        assert "x-frame-options" not in h


def test_logout_and_cross_origin_post(client, fake):
    sign_in(client, fake)
    assert client.post("/sync", headers={"Origin": "https://evil.example.test"}).status_code == 403
    assert client.post("/auth/logout").status_code == 200
    assert client.get("/").status_code == 303


@pytest.mark.parametrize("value,expected", [(None, "/"), ("/x?y=1", "/x?y=1"), ("//evil", "/"),
                                            ("/\\evil", "/"), ("https://evil", "/")])
def test_safe_next(value, expected):
    assert _safe_next(value) == expected


def test_access_log_redacts_callback_code():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("1.2.3.4:5", "GET", "/auth/callback?code=secret&state=s", "1.1", 303), None)
    RedactCallbackQuery().filter(record)
    assert "secret" not in record.getMessage() and "/auth/callback?…" in record.getMessage()


def test_config_requires_urls_outside_dev(monkeypatch):
    from platewright import config

    for name in ("PLATEWRIGHT_DEV", "PLATEWRIGHT_PUBLIC_URL", "PLATEWRIGHT_BAMBUDDY_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PLATEWRIGHT_SESSION_KEY", "k" * 40)
    with pytest.raises(config.ConfigError, match="required"):
        config.load()
    monkeypatch.setenv("PLATEWRIGHT_DEV", "1")
    assert config.load().public_url == "http://localhost:3710"


# -- sync ---------------------------------------------------------------------------------

def run_sync(fake, store):
    async def go():
        bb = Bambuddy(CONFIG, transport=httpx.MockTransport(fake.handler))
        try:
            return await sync_once(bb, store)
        finally:
            await bb.aclose()
    return asyncio.run(go())


def test_sync_matches_by_fingerprint_and_name(fake, store):
    lock_plate(store)
    fake.archives = {10: {"content_hash": PLATE_SHA, "filename": "nunu.gcode.3mf"},
                     11: {"content_hash": "other", "filename": "PLATE-N_4up.gcode.3mf"},
                     12: {"content_hash": "x", "filename": "benchy.gcode.3mf"}}
    fake.entries = [entry(1, verdict="good", archive_id=10), entry(2, archive_id=11),
                    entry(3, archive_id=12, name="benchy"), entry(4, status="printing", archive_id=10),
                    entry(5, status="completed", verdict="reject", archive_id=10)]
    s = run_sync(fake, store)
    assert (s["new"], s["printing"], s["error"]) == (3, 1, None)
    assert s["other"] == {"benchy": 1} and len(s["by_name"]) == 1
    runs = {r.bambuddy_run_id: r for r in store.runs()}
    assert runs["1"].good == 4 and runs["1"].verdict == "good" and runs["1"].archive_id == 10
    assert runs["2"].matched_by_name and runs["2"].verdict == ""
    assert runs["5"].outcome == "rejected" and runs["5"].good == 0
    assert [r.bambuddy_run_id for r in store.awaiting_verdict()] == ["2"]
    # a verdict given later in Bambuddy updates the run on the next pass
    fake.entries[1]["user_verdict"] = "good"
    archive_calls = sum(c.startswith("/api/v1/archives/") for c in fake.calls)
    s = run_sync(fake, store)
    assert (s["new"], s["updated"]) == (0, 1) and not store.awaiting_verdict()
    # archives are fetched once, then remembered
    assert archive_calls == 3 and sum(c.startswith("/api/v1/archives/") for c in fake.calls) == 3


def test_sync_links_a_hand_logged_run_instead_of_counting_it_twice(fake, store):
    vid = lock_plate(store)
    store.q("INSERT INTO runs (plate_version_id, date, printer, outcome, source) VALUES (?, ?, 'X1C', 'ok', 'manual')",
            vid, TODAY.isoformat())
    fake.archives = {10: {"content_hash": PLATE_SHA}}
    fake.entries = [entry(1, archive_id=10)]
    assert run_sync(fake, store)["linked"] == 1
    (run,) = store.runs()
    assert run.source == "manual" and run.bambuddy_run_id == "1"


def test_name_matched_runs_keep_their_version(fake, store):
    v1 = lock_plate(store)
    fake.archives = {11: {"content_hash": "unknown", "filename": "PLATE-N_4up.gcode.3mf"}}
    fake.entries = [entry(2, archive_id=11)]
    run_sync(fake, store)
    other = sliced_3mf(objects=("Nunu",) * 6)
    s = read_sliced(io.BytesIO(other))
    store.register_plate("N", check_plate(s, store.core_models(), store.settings()),
                         sha256=hashlib.sha256(other).hexdigest())
    fake.entries[0]["user_verdict"] = "good"
    run_sync(fake, store)
    (run,) = store.runs()
    assert run.version == 1 and run.planned == 4 and run.verdict == "good"
    assert store.one("SELECT plate_version_id FROM runs")[0] == v1


def test_missing_archives_are_retried(fake, store):
    lock_plate(store)
    fake.entries = [entry(1, archive_id=10, name="x")]
    run_sync(fake, store)  # archive 10 not there yet (404)
    fake.archives = {10: {"content_hash": PLATE_SHA}}
    assert run_sync(fake, store)["new"] == 1


def test_tx_releases_the_lock_when_begin_fails(store):
    with store.tx():
        pass
    store._db.execute("BEGIN")  # someone left a transaction open
    with pytest.raises(Exception):
        with store.tx():
            pass
    store._db.execute("ROLLBACK")
    assert store._lock.acquire(blocking=False)
    store._lock.release()
    with store.tx() as db:
        db.execute("INSERT INTO sync_state (key, value) VALUES ('k', '1')")
    assert store.state("k") == 1


def test_htmx_poll_after_session_expiry_reloads_the_page(client):
    r = client.get("/partials/printing", headers={"HX-Request": "true",
                                                 "HX-Current-URL": "https://plates.example.test/filament?x=1"})
    assert r.status_code == 204 and r.headers["HX-Redirect"] == "/filament?x=1"
    r = client.post("/sync")
    assert r.status_code == 303 and r.headers["location"] == "/auth/login?next=/"


def test_sync_without_plates_counts_other_prints(fake, store):
    fake.entries = [entry(1, name="benchy")]
    s = run_sync(fake, store)
    assert s["other"] == {"benchy": 1} and s["note"] == "no plates registered yet"
    assert not any(c.startswith("/api/v1/archives/") for c in fake.calls)


def test_sync_error_is_recorded(fake, store):
    bad = CONFIG.__class__(**{**CONFIG.__dict__, "bambuddy_key": "wrong"})

    async def go():
        bb = Bambuddy(bad, transport=httpx.MockTransport(fake.handler))
        try:
            return await sync_once(bb, store)
        finally:
            await bb.aclose()
    s = asyncio.run(go())
    assert "401" in s["error"] and store.state("last_sync")["error"] == s["error"]


# -- pages --------------------------------------------------------------------------------

def test_today_page_and_badge(client, fake, store):
    lock_plate(store)
    fake.archives = {10: {"content_hash": PLATE_SHA}}
    fake.entries = [entry(1, archive_id=10), entry(2, verdict="good", archive_id=10)]
    sign_in(client, fake)
    r = client.post("/sync")
    assert r.status_code == 200 and "2 new" in r.text
    assert fake.badges[-1] == {"url": "https://plates.example.test", "count": 2}  # two runs to review
    page = client.get("/").text
    assert "To Review" in page and 'data-bambuddy-path="/archives/10"' in page and "No good / reject" in page
    assert "Good toys" in page and '8 <span class="of">of 500</span>' in page
    assert "Mark All Good" in page  # two prints to review
    printing = client.get("/partials/printing").text
    assert "PLATE-N_4up" in printing and "42%" in printing and "2 h 5 min left" in printing


def test_plates_and_models_pages(client, fake, store):
    lock_plate(store)
    sign_in(client, fake)
    plates = client.get("/plates").text
    assert "Plate N" in plates and "Nunu ×4" in plates and 'data-bambuddy-path="/files?highlight=2"' in plates
    assert "Version 1" in client.get("/plates/n").text
    assert client.get("/plates/ZZ").status_code == 404
    models = client.get("/models").text
    assert "Nunu" in models and "Test print passed" in models and "May hand out" in models and "Ages 3+" in models


def test_filament_page(client, fake, store):
    fake.spools = [{"rgba": "9D432CFF", "material": "PLA", "label_weight": 1000, "weight_used": 250},
                   {"rgba": "00FF00FF", "material": "PETG", "brand": "Acme", "label_weight": 1000, "weight_used": 0}]
    sign_in(client, fake)
    page = client.get("/filament").text
    assert "750 g" in page and "Spools not in the palette" in page and "Acme" in page
    assert page.count("reorder") >= 1  # White: none on hand


def test_empty_pages_explain_themselves(client, fake):
    sign_in(client, fake)
    assert "No plates locked yet" in client.get("/plates").text
    assert "No models registered yet" not in client.get("/models").text  # Nunu exists in this store
    assert "Nothing to review" in client.get("/").text


def test_sync_needs_admin(client, fake, store):
    USER["is_admin"] = False
    try:
        sign_in(client, fake)
        assert client.post("/sync").status_code == 403
    finally:
        USER["is_admin"] = True


def test_settings_seed_only_into_empty_table(tmp_path):
    store = Store(":memory:")
    assert store.seed_settings({"unit_name": "toy"})
    assert not store.seed_settings({"unit_name": "part"})
    assert store.settings().unit_name == "toy"


def test_cache_fetches_first_time_even_right_after_boot(monkeypatch):
    from platewright.web import app as web

    monkeypatch.setattr(web.time, "monotonic", lambda: 5.0)  # a host that booted 5 s ago
    cache = web.Cached(60)

    async def fetch():
        return ["spool"]
    assert asyncio.run(cache.get(fetch)) == (["spool"], None)


def test_static_urls_are_versioned(client, fake):
    sign_in(client, fake)
    page = client.get("/").text
    assert '/static/app.css?v=' in page and '/static/app.js?v=' in page
    href = page.split('/static/app.css?v=')[1].split('"')[0]
    assert len(href) == 10 and client.get(f"/static/app.css?v={href}").status_code == 200


def test_unknown_address_is_a_page_not_json(client, fake):
    sign_in(client, fake)
    r = client.get("/no-such-page")
    assert r.status_code == 404 and "<html" in r.text and "no page at this address" in r.text
    r = client.get("/no-such-page", headers={"HX-Request": "true"})
    assert r.status_code == 404 and 'role="alert"' in r.text and "<html" not in r.text
