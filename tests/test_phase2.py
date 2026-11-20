"""Phase 2: roles, models, locking plates, review, stock, demand and queueing, settings."""

import dataclasses
import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from platewright.web.app import create_app
from tests.test_web import CONFIG, PLATE_FILE, PLATE_SHA, USER, FakeBambuddy, entry, make_store, sign_in
from tests.threemf_factory import project_3mf, sliced_3mf


class WritableBambuddy(FakeBambuddy):
    """Adds the library, project and queue endpoints Platewright writes to."""

    def __init__(self):
        super().__init__()
        self.folders = [{"id": 1, "name": "Models", "parent_id": None}]
        self.files: dict[int, dict] = {50: {"id": 50, "filename": "nunu.3mf", "folder_id": 1, "file_hash": "m"}}
        self.projects: dict[int, dict] = {}
        self.queue: list[dict] = []
        self.patches: list[tuple[int, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path.startswith("/api/v1/connect/") or request.headers.get("X-API-Key") != "bb_testkey":
            return super().handler(request)
        if path == "/api/v1/library/folders" and method == "GET":
            return httpx.Response(200, json=self.folders)
        if path == "/api/v1/library/folders" and method == "POST":
            f = {"id": len(self.folders) + 1, "name": json.loads(request.content)["name"], "parent_id": None}
            self.folders.append(f)
            return httpx.Response(200, json=f)
        if path == "/api/v1/library/files" and method == "GET":
            if request.url.params.get("recursive") == "true":
                return httpx.Response(200, json=list(self.files.values()))
            fid = int(request.url.params.get("folder_id", 0))
            return httpx.Response(200, json=[f for f in self.files.values() if f["folder_id"] == fid])
        if path == "/api/v1/library/files" and method == "POST":
            body = request.read()
            name = body.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
            data = body.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
            fid = max(self.files) + 1
            self.files[fid] = {"id": fid, "filename": name, "folder_id": int(request.url.params["folder_id"]),
                               "file_hash": hashlib.sha256(data).hexdigest()}
            return httpx.Response(200, json={"id": fid, "filename": name})
        if path.startswith("/api/v1/library/files/") and path.endswith("/thumbnail"):
            return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})
        if path.startswith("/api/v1/library/files/") and method == "GET":
            f = self.files.get(int(path.rsplit("/", 1)[1]))
            return httpx.Response(200, json=f) if f else httpx.Response(404, json={})
        if path == "/api/v1/library/files/move":
            body = json.loads(request.content)
            for i in body["file_ids"]:
                self.files[i]["folder_id"] = body["folder_id"]
            return httpx.Response(200, json={"moved": len(body["file_ids"])})
        if path == "/api/v1/projects/" and method == "POST":
            pid = 100 + len(self.projects)
            self.projects[pid] = json.loads(request.content)
            return httpx.Response(200, json={"id": pid})
        if path.startswith("/api/v1/projects/") and method == "PATCH":
            pid = int(path.rsplit("/", 1)[1])
            if pid not in self.projects:
                return httpx.Response(404, json={"detail": "gone"})
            self.patches.append((pid, json.loads(request.content)))
            return httpx.Response(200, json={"id": pid})
        if path == "/api/v1/queue/" and method == "POST":
            self.queue.append(json.loads(request.content))
            return httpx.Response(200, json={"id": 900 + len(self.queue)})
        return super().handler(request)


@pytest.fixture
def fake():
    return WritableBambuddy()


@pytest.fixture
def store():
    return make_store()


@pytest.fixture
def client(fake, store, tmp_path):
    cfg = dataclasses.replace(CONFIG, data_dir=str(tmp_path))
    app = create_app(cfg, transport=httpx.MockTransport(fake.handler), store=store)
    with TestClient(app, base_url="https://plates.example.test", follow_redirects=False) as c:
        yield c


@pytest.fixture
def as_user():
    """Sign in as someone other than an admin for one test."""
    saved = dict(USER)

    def set_(**fields):
        USER.update(fields)
    yield set_
    USER.clear()
    USER.update(saved)


def lock_via_web(client, data=PLATE_FILE, code="N"):
    r = client.post("/plates/new", files={"file": ("nunu-4.gcode.3mf", data)})
    assert r.status_code == 303, r.text
    sha = r.headers["location"].rsplit("/", 1)[1]
    return client.post(f"/plates/new/{sha}/lock", data={"code": code, "description": "four Nunu"}), sha


# -- roles ------------------------------------------------------------------------------------

def test_viewer_reads_but_cannot_write(client, fake, as_user):
    as_user(is_admin=False, groups=["Viewers"])
    sign_in(client, fake)
    assert client.get("/models").status_code == 200
    assert client.post("/models", data={"name": "X"}).status_code == 403
    assert client.post("/stock/out", data={"qty": 1}).status_code == 403
    assert "Settings" not in client.get("/").text.split("</nav>")[0]
    assert client.get("/settings").status_code == 403


def test_operator_group_comes_from_settings(client, fake, store, as_user):
    as_user(is_admin=False, groups=["Toy helpers"])
    store.set_settings({"operator_group": "Toy helpers"})
    sign_in(client, fake)
    assert "· operator" in client.get("/").text
    assert client.post("/models", data={"name": "X"}).status_code == 403  # models are admin work


def test_errors_render_as_a_page(client, fake):
    sign_in(client, fake)
    r = client.get("/models/nope")
    assert r.status_code == 404 and "No such model" in r.text and "<html" in r.text


# -- models ---------------------------------------------------------------------------------

def test_add_edit_and_test_print_a_model(client, fake, store):
    sign_in(client, fake)
    assert "nunu.3mf" in client.get("/models/new").text  # library files to link
    r = client.post("/models", data={"name": "Rowan", "ref": "91", "distribution_ok": "yes", "min_age": "3",
                                     "small_parts": "no", "aliases": "rowan body\nrowan head", "bambuddy_file_id": "50"})
    assert r.headers["location"].startswith("/models/091-rowan")
    m = store.model("091-rowan")
    assert m["status"] == "new" and m["bambuddy_file_id"] == 50 and set(m["aliases"].split("\x1f")) == {"rowan body", "rowan head"}
    assert client.post("/models", data={"name": "Rowan", "ref": "091"}).headers["location"].startswith("/models/091-rowan?notice=")
    assert client.post("/models/091-rowan", data={"name": "Rowan", "url": "javascript:alert(1)"}).status_code == 400
    client.post("/models/091-rowan", data={"name": "Rowan the Bear", "aliases": "rowan body", "distribution_ok": "yes",
                                           "bambuddy_file_id": "50"})
    assert store.model("091-rowan")["name"] == "Rowan the Bear" and store.model("091-rowan")["aliases"] == "rowan body"
    client.post("/models/091-rowan/test", data={"result": "pass", "notes": "clean"})
    assert store.model("091-rowan")["status"] == "qualified"
    page = client.get("/models/091-rowan").text
    assert "test print passed" in page and "clean" in page and 'src="/thumb/50"' in page
    assert client.get("/thumb/50").headers["content-type"] == "image/png"


# -- plates ---------------------------------------------------------------------------------

def test_project_file_is_explained_not_locked(client, fake):
    sign_in(client, fake)
    r = client.post("/plates/new", files={"file": ("nunu.3mf", project_3mf())})
    assert r.status_code == 400 and "not a sliced plate" in r.text and "&#39;Nunu&#39; x2" in r.text


def test_check_link_lock_and_publish(client, fake, store):
    sign_in(client, fake)
    data = sliced_3mf(objects=("Nunu", "Nunu 2", "Mystery", "Mystery 2"))
    r = client.post("/plates/new", files={"file": ("mix.gcode.3mf", data)})
    sha = r.headers["location"].rsplit("/", 1)[1]
    page = client.get(f"/plates/new/{sha}").text
    assert "not linked to a registered model" in page and "'Mystery' ×2 is" in page
    assert client.post(f"/plates/new/{sha}/lock", data={"code": "M"}).status_code == 400  # blocked
    client.post(f"/plates/new/{sha}/alias", data={"name": "Mystery", "model": "nunu"})
    assert "Everything checks out" in client.get(f"/plates/new/{sha}").text
    r = client.post(f"/plates/new/{sha}/lock", data={"code": "m", "description": "mix"})
    assert r.headers["location"].startswith("/plates/M?notice=") and "library" in r.headers["location"]
    p = store.plate("M")
    assert p.units == 4 and p.mix == {"nunu": 4}
    lib = fake.files[p.bambuddy_file_id]
    assert lib["filename"] == "PLATE-M_4up.gcode.3mf" and lib["file_hash"] == hashlib.sha256(data).hexdigest()
    assert next(f for f in fake.folders if f["id"] == lib["folder_id"])["name"] == "Production plates"


def test_new_version_moves_the_old_file_out_and_retire(client, fake, store):
    sign_in(client, fake)
    lock_via_web(client)
    v1 = store.plate("N")
    lock_via_web(client, sliced_3mf(objects=("Nunu",) * 6))
    v2 = store.plate("N")
    assert (v1.version, v2.version, v2.units) == (1, 2, 6)
    retired = next(f["id"] for f in fake.folders if f["name"] == "Retired plates")
    assert fake.files[v1.bambuddy_file_id]["folder_id"] == retired
    assert fake.files[v2.bambuddy_file_id]["folder_id"] != retired
    assert "Earlier versions" in client.get("/plates/N").text
    client.post("/plates/N/retire")
    assert not store.plate("N").active and fake.files[v2.bambuddy_file_id]["folder_id"] == retired


def test_same_file_again_reuses_the_library_copy(client, fake, store):
    sign_in(client, fake)
    lock_via_web(client)
    n_files = len(fake.files)
    store.set_version_file(store.plate("N").version_id, None)
    client.post("/plates/N/publish")
    assert len(fake.files) == n_files and store.plate("N").bambuddy_file_id


def test_upload_failure_leaves_the_plate_locked(client, fake, store):
    sign_in(client, fake)
    real = fake.handler

    def no_library(request):
        if request.url.path == "/api/v1/library/files" and request.method == "POST":
            return httpx.Response(403, json={"detail": "nope"})
        return real(request)
    fake.handler = no_library
    client.app.state.ctx.bambuddy._http._transport = httpx.MockTransport(no_library)
    r, _ = lock_via_web(client)
    assert "not+in+Bambuddy+yet" in r.headers["location"] and "permission" in r.headers["location"]
    assert store.plate("N").bambuddy_file_id is None
    assert "Upload to Bambuddy" in client.get("/plates/N").text


# -- review and runs ------------------------------------------------------------------------

def test_review_records_bad_parts_and_clears_the_badge(client, fake, store):
    sign_in(client, fake)
    lock_via_web(client)
    fake.archives = {10: {"content_hash": PLATE_SHA}}
    fake.entries = [entry(1, archive_id=10)]
    client.post("/sync")
    assert fake.badges[-1]["count"] == 1
    (run,) = store.to_review()
    r = client.post(f"/runs/{run.id}/review", data={"bad_nunu": "1"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "<strong>3</strong> / 4 good" in r.text and "Reviewed" in r.text
    assert not store.to_review() and store.stock()["nunu"]["made"] == 3
    assert client.post(f"/runs/{run.id}/review", data={"bad_nunu": "9"}).status_code == 400
    client.post("/sync")
    assert fake.badges[-1]["count"] == 0
    # all good, after a change of mind
    client.post(f"/runs/{run.id}/review", data={})
    assert store.run(run.id).good == 4


def test_log_a_run_by_hand(client, fake, store):
    sign_in(client, fake)
    lock_via_web(client)
    import datetime as dt
    today = dt.date.today().isoformat()
    client.post("/plates/N/runs", data={"date": today, "outcome": "ok", "bad": "1"})
    (run,) = store.runs()
    assert run.source == "manual" and run.good == 3 and run.reviewed_at
    assert client.post("/plates/N/runs", data={"date": "2999-01-01"}).status_code == 400


# -- stock ----------------------------------------------------------------------------------

def test_stock_out_and_adjust(client, fake, store):
    sign_in(client, fake)
    client.post("/stock/adjust", data={"model": "nunu", "qty": "10", "notes": "made before Platewright"})
    assert store.stock()["nunu"]["on_hand"] == 10
    r = client.post("/stock/out", data={"qty": "12"})
    assert "Not+recorded" in r.headers["location"] and store.stock()["nunu"]["on_hand"] == 10
    client.post("/stock/out", data={"qty": "4", "model": "nunu"})
    client.post("/stock/out", data={"qty": "8", "force": "1"})  # 6 Nunu + 2 unlinked
    s = store.stock()
    assert s["nunu"]["on_hand"] == 0 and s["_unlinked"]["out"] == 2
    page = client.get("/stock").text
    assert "made before Platewright" in page and "mixed" in page
    assert client.post("/stock/adjust", data={"model": "nunu", "qty": "0", "notes": "x"}).status_code == 400


# -- demand and queueing --------------------------------------------------------------------

def test_demand_coverage_and_queue(client, fake, store):
    sign_in(client, fake)
    lock_via_web(client)
    r = client.post("/demand", data={"name": "Christmas outreach", "kind": "event", "due": "2099-12-20",
                                     "target": "20", "need_nunu": "10"})
    assert r.headers["location"].startswith("/demand/christmas-outreach")
    page = client.get("/demand/christmas-outreach").text
    assert "short" in page.lower() and "≈ 3 × plate N" in page
    r = client.post("/demand/christmas-outreach/queue", data={"plate": "N", "times": "5"})
    assert "Queued+plate+N" in r.headers["location"]
    (q,) = fake.queue
    assert q["quantity"] == 5 and q["manual_start"] and q["confirm_outcome"] and q["require_previous_success"]
    assert q["library_file_id"] == store.plate("N").bambuddy_file_id and q["printer_id"] == 1
    pid = store.demand("christmas-outreach")["bambuddy_project_id"]
    assert fake.projects[pid]["target_parts_count"] == 20 and fake.projects[pid]["due_date"] == "2099-12-20T12:00:00"
    client.post("/demand/christmas-outreach/queue", data={"plate": "N", "times": "1"})
    assert len(fake.projects) == 1 and fake.patches[-1][0] == pid  # the same project, updated
    client.post("/stock/adjust", data={"model": "nunu", "qty": "20", "notes": "x"})
    client.post("/stock/out", data={"qty": "20", "model": "nunu", "demand": "christmas-outreach"})
    assert "Covered" in client.get("/demand").text or "out so far" in client.get("/demand").text
    client.post("/demand/christmas-outreach/status", data={"status": "done"})
    assert store.demand("christmas-outreach")["status"] == "done"


def test_demand_validation(client, fake):
    sign_in(client, fake)
    assert client.post("/demand", data={"name": "X", "kind": "event", "due": "2099-01-01", "target": "5",
                                        "need_nunu": "10"}).status_code == 400
    assert client.post("/demand", data={"name": "X", "kind": "event", "due": "soon"}).status_code == 400


# -- settings -------------------------------------------------------------------------------

def test_settings_save_and_validate(client, fake, store):
    sign_in(client, fake)
    form = {"unit_name": "toy", "period_target": "400", "max_plate_hours": "20", "assumed_fail_rate": "5",
            "reorder_days": "14", "operator_group": "Toy helpers", "library_folder": "Production plates",
            "retired_folder": "Retired plates",
            "printer_key": ["X1C", ""], "printer_model": ["Bambu Lab X1 Carbon", ""], "printer_per_day": ["1", ""],
            "printer_model_id": ["", ""], "printer_bambuddy_name": ["", ""],
            "color_name": ["White", "Brown", ""], "color_hex": ["ffffff", "#9d432c", ""],
            "color_material": ["pla", "PLA", ""], "color_slot": ["a1", "A2", ""], "color_backup": ["", "", ""]}
    r = client.post("/settings", data=form)
    assert r.status_code == 303
    s = store.settings()
    assert s.period_target == 400 and s.assumed_fail_rate == 0.05 and s.palette["White"] == \
        {"hex": "#FFFFFF", "material": "PLA", "slot": "A1", "backup_slot": ""}
    bad = {**form, "color_hex": ["nothex", "#9d432c", ""]}
    r = client.post("/settings", data=bad)
    assert r.status_code == 400 and "needs a hex value" in r.text
    assert store.settings().palette["White"]["hex"] == "#FFFFFF"  # nothing saved
    assert client.get("/settings/export").json()["period_target"] == 400


def test_backup_status(client, fake, tmp_path):
    import datetime as dt
    sign_in(client, fake)
    assert client.get("/health/backup").status_code == 503
    assert "No backup has run yet" in client.get("/settings").text
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (tmp_path / "backup-status.json").write_text(json.dumps({"ok": True, "at": now, "backup": "x", "bytes": 2048}))
    assert client.get("/health/backup").status_code == 200
    assert "2.0 KB" in client.get("/settings").text
    (tmp_path / "backup-status.json").write_text(json.dumps({"ok": False, "at": now, "error": "line 40"}))
    assert client.get("/health/backup").status_code == 503 and "line 40" in client.get("/settings").text


# -- review fixes ---------------------------------------------------------------------------

def test_same_name_demand_gets_its_own_record(client, fake, store):
    sign_in(client, fake)
    form = {"name": "Spring fair", "kind": "event", "due": "2099-04-01", "target": "10"}
    client.post("/demand", data=form)
    r = client.post("/demand", data={**form, "due": "2099-05-01", "target": "99"})
    assert r.headers["location"].startswith("/demand/spring-fair-2")
    assert store.demand("spring-fair")["target"] == 10 and store.demand("spring-fair-2")["target"] == 99
    client.post("/demand", data={**form, "name": "New"})
    assert store.demand("new-1") and client.get("/demand/new").status_code == 200


def test_skipped_move_is_reported_and_failed_upload_keeps_the_old_file(client, fake, store):
    sign_in(client, fake)
    lock_via_web(client)
    v1 = store.plate("N")
    real = fake.handler

    def skip_moves(request):
        if request.url.path == "/api/v1/library/files/move":
            return httpx.Response(200, json={"moved": 0, "skipped": [{"id": v1.bambuddy_file_id, "reason": "not_owner"}]})
        return real(request)
    client.app.state.ctx.bambuddy._http._transport = httpx.MockTransport(skip_moves)
    r, _ = lock_via_web(client, sliced_3mf(objects=("Nunu",) * 5))
    assert "didn%27t+move" in r.headers["location"] or "didn't" in r.headers["location"]

    def no_uploads(request):
        if request.url.path == "/api/v1/library/files" and request.method == "POST":
            return httpx.Response(500, json={})
        return real(request)
    client.app.state.ctx.bambuddy._http._transport = httpx.MockTransport(no_uploads)
    v2 = store.plate("N")
    lock_via_web(client, sliced_3mf(objects=("Nunu",) * 7))
    production = next(f["id"] for f in fake.folders if f["name"] == "Production plates")
    assert fake.files[v2.bambuddy_file_id]["folder_id"] == production  # still printable


def test_stopped_run_needs_a_sane_percentage(client, fake, store):
    import datetime as dt
    sign_in(client, fake)
    lock_via_web(client)
    today = dt.date.today().isoformat()
    for pct in ("", "0", "500", "²"):
        assert client.post("/plates/N/runs", data={"date": today, "outcome": "aborted", "aborted_pct": pct}).status_code == 400
    client.post("/plates/N/runs", data={"date": today, "outcome": "aborted", "aborted_pct": "50"})
    (run,) = store.runs()
    assert run.hours == round(store.plate("N").hours * 0.5, 2) and run.good == 0


def test_unicode_digits_and_duplicate_aliases_dont_crash(client, fake, store):
    sign_in(client, fake)
    assert client.post("/demand", data={"name": "X", "kind": "event", "due": "2099-01-01", "target": "²"}).status_code == 400
    r = client.post("/models", data={"name": "Bear", "aliases": "bear body\nbear body", "min_age": "³"})
    assert r.status_code == 303 and store.model("bear")["aliases"] == "bear body" and store.model("bear")["min_age"] is None


def test_settings_refuse_one_folder_for_both(client, fake, store):
    from platewright.core.settings import Settings
    s = Settings.from_values({**store.settings_values(), "retired_folder": "Production plates"})
    assert any("different Bambuddy folder" in p for p in s.problems())
    assert any("fail rate" in p for p in Settings.from_values({"assumed_fail_rate": float("nan")}).problems())


def test_thumbnails_only_for_linked_files(client, fake):
    sign_in(client, fake)
    assert client.get("/thumb/50").status_code == 404  # no model links file 50 yet
