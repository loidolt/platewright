"""AMS slots: the slot list, which RFID links to check, and moving a tag to the right spool."""

import copy
import dataclasses
import html as html_
import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from platewright.core.slots import candidates, norm_tag, printer_slots, slot_label, tray_match
from platewright.web.app import create_app
from tests.test_web import CONFIG, FakeBambuddy, make_store, sign_in

TAG = "04A1B2C3D4E5F6"
UUID = "9F8E7D6C5B4A39281706F5E4D3C2B1A0"
BLACK = "000000FF"


def spool(i, **kw):
    return {"id": i, "material": "PLA", "subtype": "Basic", "rgba": BLACK, "brand": "Bambu Lab", "color_name": "Black",
            "label_weight": 1000, "weight_used": 0, "note": None, "tag_uid": None, "tray_uuid": None,
            "data_origin": "manual", "tag_type": None, "archived_at": None, "created_at": f"2026-10-{i % 28 + 1:02d}T08:00:00",
            **kw}


SPOOLS = [
    spool(12, tag_uid=TAG, tray_uuid=UUID, data_origin="rfid_linked", tag_type="bambulab", note="sealed",
          created_at="2026-10-01T08:00:00"),
    spool(30, weight_used=412.4, note="opened Nov 3", created_at="2026-10-20T08:00:00"),
    spool(31, subtype=None, brand=None, created_at="2026-11-01T08:00:00"),
    spool(32, brand="Polymaker"),
    spool(33, subtype="Matte"),
    spool(35, tag_uid="0102030405060708"),
    spool(36, material="PETG"),
    spool(37, rgba="000000ff", brand="bambu", created_at="2026-09-01T08:00:00"),
    spool(3, material="PETG", subtype=None, brand="Elegoo", rgba="2E7D32FF", color_name="Green"),
]


def tray(tray_id, tag="0" * 16, uuid="0" * 32, **kw):
    return {"id": tray_id, "tray_type": "PLA", "tray_sub_brands": "PLA Basic", "tray_color": BLACK,
            "tray_id_name": "A00-K0", "tag_uid": tag, "tray_uuid": uuid, "state": 11, **kw}


EMPTY = {"id": 1, "tray_type": "", "tray_color": "", "tag_uid": "0" * 16, "tray_uuid": "0" * 32, "state": 9}
STATUS = {"connected": True, "state": "IDLE",
          "ams": [{"id": 0, "tray": [tray(0, TAG, UUID), EMPTY]}],
          "vt_tray": [{"id": 254, "tray_type": "PETG", "tray_color": "2E7D32FF", "tag_uid": "0" * 16,
                       "tray_uuid": "0" * 32, "state": 11}]}
DENIED = {"detail": "API key does not have 'can_manage_inventory' permission"}


class SlotsBambuddy(FakeBambuddy):
    """Adds Bambuddy v1.3.1's inventory assignment and tag endpoints, and records every write."""

    def __init__(self):
        super().__init__()
        self.spools = copy.deepcopy(SPOOLS)
        self.status = copy.deepcopy(STATUS)
        self.assignments = [{"id": 1, "printer_id": 1, "ams_id": 0, "tray_id": 0, "spool_id": 12},
                            {"id": 2, "printer_id": 1, "ams_id": 255, "tray_id": 0, "spool_id": 3}]
        self.can_write = True  # the key's Manage Inventory permission
        self.refuse: str | None = None  # a write path that answers 500
        self.writes: list[tuple[str, str, dict]] = []
        self.probes = 0

    def find(self, spool_id):
        return next((s for s in self.spools if s["id"] == spool_id), None)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path.startswith("/api/v1/connect/") or request.headers.get("X-API-Key") != "bb_testkey":
            return super().handler(request)
        if path == "/api/v1/printers/1/status":
            return httpx.Response(200, json=self.status)
        if path == "/api/v1/inventory/assignments" and method == "GET":
            return httpx.Response(200, json=[{**a, "spool": self.find(a["spool_id"])} for a in self.assignments])
        if method in ("PATCH", "POST") and path.startswith("/api/v1/inventory/"):
            body = json.loads(request.content)
            if path == "/api/v1/inventory/spools/0":
                self.probes += 1
            else:
                self.writes.append((method, path.removeprefix("/api/v1"), body))
            if not self.can_write:  # checked before the spool is looked up
                return httpx.Response(403, json=DENIED)
            if path == self.refuse:
                return httpx.Response(500, json={"detail": "boom"})
            return self.write(method, path, body)
        return super().handler(request)

    def write(self, method, path, body) -> httpx.Response:
        if path == "/api/v1/inventory/assignments":
            slot = (body["printer_id"], body["ams_id"], body["tray_id"])
            self.assignments = [a for a in self.assignments if (a["printer_id"], a["ams_id"], a["tray_id"]) != slot
                                and not (body.get("move") and a["spool_id"] == body["spool_id"])]
            self.assignments.append({"id": 9, "printer_id": slot[0], "ams_id": slot[1], "tray_id": slot[2],
                                     "spool_id": body["spool_id"]})
            return httpx.Response(200, json=self.assignments[-1])
        parts = path.split("/")  # /api/v1/inventory/spools/{id}[/link-tag]
        s = self.find(int(parts[5]))
        if s is None:
            return httpx.Response(404, json={"detail": "Spool not found"})
        if path.endswith("/link-tag"):
            for f in ("tag_uid", "tray_uuid"):
                if body.get(f) and any(o[f] == body[f] for o in self.spools if o["id"] != s["id"]):
                    return httpx.Response(409, json={"detail": {"error": "tag_already_linked", "spool_id": 0}})
                if body.get(f) is not None:
                    s[f] = body[f]
            s["data_origin"] = body.get("data_origin") or "nfc_link"
            return httpx.Response(200, json=s)
        s.update(body)
        return httpx.Response(200, json=s)


@pytest.fixture
def fake():
    return SlotsBambuddy()


@pytest.fixture
def client(fake, tmp_path):
    cfg = dataclasses.replace(CONFIG, data_dir=str(tmp_path))
    app = create_app(cfg, transport=httpx.MockTransport(fake.handler), store=make_store())
    with TestClient(app, base_url="https://plates.example.test", follow_redirects=False) as c:
        sign_in(c, fake)
        yield c


def move(client, spool_id=30, tag=f"{TAG}:{UUID}", tray_id=0, ams_id=0, htmx=True):
    return client.post("/filament/slots/move", headers={"HX-Request": "true"} if htmx else {},
                       data={"printer_id": 1, "ams_id": ams_id, "tray_id": tray_id, "tag": tag, "spool_id": spool_id})


# -- reading the tray and the candidates ------------------------------------------------------

@pytest.mark.parametrize("fields,key", [
    ({"tray_type": "PLA", "tray_sub_brands": "PLA Basic"}, ("PLA", "Basic")),
    ({"tray_type": "PLA", "tray_sub_brands": "PLA"}, ("PLA", None)),
    ({"tray_type": "PETG", "tray_sub_brands": "PETG HF"}, ("PETG", "HF")),
    ({"tray_type": "PLA", "tray_sub_brands": "Support for PLA"}, ("Support for PLA", None)),
    ({"tray_type": "PLA", "tray_sub_brands": "PLA-CF"}, ("PLA-CF", None)),
    ({"tray_type": "PLA", "tray_sub_brands": "PLA Basic", "tray_id_name": "A00-M1"}, ("PLA", "Gradient")),
    ({"tray_type": "PLA", "tray_sub_brands": "PLA Silk", "tray_id_name": "A05-M4"}, ("PLA", "Dual Color")),
    ({"tray_type": "PLA", "tray_sub_brands": "PLA Silk", "tray_id_name": "A05-T1"}, ("PLA", "Tri Color")),
])
def test_tray_match_reads_material_and_subtype_like_bambuddy(fields, key):
    assert tray_match({**fields, "tray_color": BLACK}) == (*key, BLACK)


def test_tray_match_needs_type_and_color():
    assert tray_match({"tray_type": "PLA", "tray_color": ""}) is None
    assert tray_match({"tray_type": "", "tray_color": BLACK}) is None


def test_candidates_follow_bambuddys_rule_and_order():
    with_archived = SPOOLS + [spool(38, archived_at="2026-11-02T00:00:00")]
    # Exact subtype first, oldest first; then no subtype. Not: tagged (12, 35), another brand (32),
    # another subtype (33), another material (36, 3), archived (38).
    assert [s["id"] for s in candidates(with_archived, ("PLA", "Basic", BLACK))] == [37, 30, 31]
    assert [s["id"] for s in candidates(with_archived, ("pla", "BASIC", "000000ff"))] == [37, 30, 31]
    # A tray without a subtype matches spools without one only.
    assert [s["id"] for s in candidates(with_archived, ("PLA", None, BLACK))] == [31]
    assert candidates(with_archived, ("PLA", "Basic", "FFFFFFFF")) == []


def test_norm_tag_and_slot_labels():
    assert norm_tag("0" * 16) == "" and norm_tag(None) == "" and norm_tag("04:a1:b2") == "04A1B2"
    assert norm_tag("AABBCCDDEEFF00112233", 16) == "CCDDEEFF00112233"
    assert [slot_label(0, 0), slot_label(3, 3), slot_label(128, 0, ht=True), slot_label(255, 0),
            slot_label(255, 1), slot_label(0, 2, module_type="n10a")] == ["A1", "D4", "HT-A", "Ext", "Ext-2", "3H-3"]


def test_printer_slots_flags_a_tag_bambuddy_linked_by_itself():
    p = printer_slots({"id": 1, "name": "X1C"}, STATUS, [{"printer_id": 1, "ams_id": 0, "tray_id": 0, "spool_id": 12},
                                                         {"printer_id": 1, "ams_id": 255, "tray_id": 0, "spool_id": 3}],
                      SPOOLS)
    a1, a2, ext = p.slots
    assert (a1.label, a1.spool["id"], a1.holder["id"], a1.tag_uid, a1.tray_uuid) == ("A1", 12, 12, TAG, UUID)
    assert a1.check and [c["id"] for c in a1.candidates] == [37, 30, 31] and "spool #12" in a1.reason
    assert (a2.label, a2.empty, a2.tagged, a2.check) == ("A2", True, False, False)
    assert (ext.label, ext.ams_id, ext.tray_id, ext.spool["id"], ext.check) == ("Ext", 255, 0, 3, False)
    assert p.checks == 1


def test_printer_slots_leaves_a_hand_linked_tag_alone():
    spools = [spool(12, tag_uid=TAG, tray_uuid=UUID, data_origin="nfc_link")] + SPOOLS[1:]
    a1 = printer_slots({"id": 1}, STATUS, [{"printer_id": 1, "ams_id": 0, "tray_id": 0, "spool_id": 12}], spools).slots[0]
    assert a1.candidates and not a1.check


def test_printer_slots_flags_a_tag_no_spool_carries():
    spools = [spool(12, data_origin=None)] + SPOOLS[1:]  # a move that stopped after step 1
    a1 = printer_slots({"id": 1}, STATUS, [{"printer_id": 1, "ams_id": 0, "tray_id": 0, "spool_id": 12}], spools).slots[0]
    assert a1.holder is None and a1.check and a1.reason == "No spool carries this tag."
    assert [c["id"] for c in a1.candidates] == [37, 12, 30, 31]  # the assigned spool may take it too


# -- the panel --------------------------------------------------------------------------------

def test_filament_page_loads_the_panel(client):
    page = client.get("/filament").text
    assert "AMS Slots" in page and 'hx-get="/partials/slots"' in page


def test_panel_lists_slots_and_offers_the_candidates(client, fake):
    r = client.get("/partials/slots")
    assert r.status_code == 200
    html = r.text
    assert "X1C" in html and ">A1<" in html and ">A2<" in html and ">Ext<" in html
    assert TAG in html and UUID[:8] in html and "rfid_linked" in html and "sealed" in html
    assert "1 to check" in html and "Move Tag to Spool" in html
    radios = [int(v) for v in re.findall(r'name="spool_id" value="(\d+)"', html)]
    assert radios == [37, 30, 31]
    assert "412 g used" in html and "opened Nov 3" in html
    assert "Manage Inventory</strong>" not in html and "<fieldset disabled" not in html
    assert fake.probes == 1 and fake.writes == []


def test_move_tag_makes_the_four_calls_in_order(client, fake):
    r = move(client)
    assert r.status_code == 200
    assert fake.writes == [
        ("PATCH", "/inventory/spools/12", {"tag_uid": None, "tray_uuid": None, "data_origin": None, "tag_type": None}),
        ("POST", "/inventory/assignments", {"spool_id": 30, "printer_id": 1, "ams_id": 0, "tray_id": 0, "move": True}),
        ("PATCH", "/inventory/spools/30/link-tag", {"tag_uid": TAG, "tray_uuid": UUID}),
        ("PATCH", "/inventory/spools/30", {"tag_type": "bambulab"}),
    ]
    # The panel is read again and shows the result, with a toast.
    assert "Tag in X1C A1 moved from spool #12 to spool #30" in r.text and 'hx-swap-oob="beforeend:.toasts"' in r.text
    assert "1 to check" not in r.text and "nfc_link" in r.text
    right, wrong = fake.find(30), fake.find(12)
    assert (right["tag_uid"], right["tray_uuid"], right["tag_type"]) == (TAG, UUID, "bambulab")
    assert (wrong["tag_uid"], wrong["tray_uuid"], wrong["data_origin"], wrong["tag_type"]) == (None, None, None, None)
    assert {(a["ams_id"], a["tray_id"]): a["spool_id"] for a in fake.assignments}[(0, 0)] == 30


def test_move_without_htmx_goes_back_to_the_page(client, fake):
    r = move(client, htmx=False)
    where = r.headers["location"]
    assert r.status_code == 303 and where.startswith("/filament?notice=Tag+in+X1C+A1+moved") and where.endswith("#slots")
    assert len(fake.writes) == 4


def test_a_tag_without_a_tray_uuid_is_not_marked_bambu(client, fake):
    fake.status["ams"][0]["tray"][0]["tray_uuid"] = "0" * 32
    fake.find(12)["tray_uuid"] = None
    move(client, tag=f"{TAG}:")
    assert [w[1] for w in fake.writes] == ["/inventory/spools/12", "/inventory/assignments",
                                           "/inventory/spools/30/link-tag"]
    assert fake.writes[2][2] == {"tag_uid": TAG, "tray_uuid": None}


def test_a_tag_no_spool_carries_is_linked_without_clearing(client, fake):
    fake.find(12).update(tag_uid=None, tray_uuid=None, data_origin=None, tag_type=None)
    html = client.get("/partials/slots").text
    assert "Link Tag to Spool" in html and "No spool carries this tag." in html
    r = move(client, spool_id=12)
    assert [w[1] for w in fake.writes] == ["/inventory/assignments", "/inventory/spools/12/link-tag",
                                           "/inventory/spools/12"]
    assert "Tag in X1C A1 moved to spool #12" in r.text


def test_key_without_manage_inventory_disables_the_action(client, fake):
    fake.can_write = False
    html = client.get("/partials/slots").text
    assert "Manage Inventory</strong>" in html and "can_manage_inventory" in html
    assert "<fieldset disabled" in html and "Needs the API key's Manage Inventory permission" in html
    r = move(client)
    assert "can't change Bambuddy's inventory" in html_.unescape(r.text) and "can_manage_inventory" in r.text
    assert fake.writes == []  # refused before any call


def test_a_403_during_the_move_changes_nothing_and_locks_the_panel(client, fake):
    client.get("/partials/slots")  # the key could write when the page loaded...
    fake.can_write = False  # ...and lost the permission since
    r = move(client)
    assert [w[1] for w in fake.writes] == ["/inventory/spools/12"]  # stopped at the first refusal
    assert "Bambuddy refused to clear the tag from spool #12" in r.text and "Nothing was changed." in r.text
    assert "does the API key have this permission" not in r.text
    assert "Manage Inventory</strong>" in r.text and "<fieldset disabled" in r.text  # asked again
    assert fake.find(12)["tag_uid"] == TAG


def test_a_refusal_half_way_says_what_was_done(client, fake):
    fake.refuse = "/api/v1/inventory/spools/30/link-tag"
    r = move(client)
    assert "Bambuddy refused to link the tag to spool #30" in r.text
    assert "Done before that: clear the tag from spool #12; assign spool #30 to slot A1." in r.text
    # The tag is on no spool now; the panel offers to link it.
    assert "No spool carries this tag." in r.text and "Link Tag to Spool" in r.text


def test_move_refuses_a_changed_slot_or_a_spool_that_doesnt_match(client, fake):
    assert "This slot changed" in move(client, tag="DEADBEEF:").text
    assert "This slot changed" in move(client, tray_id=3).text
    assert "isn't one this tag can go to" in html_.unescape(move(client, spool_id=32).text)  # Polymaker
    assert "Choose the spool" in client.post("/filament/slots/move", headers={"HX-Request": "true"},
                                             data={"printer_id": 1, "ams_id": 0, "tray_id": 0, "tag": "x"}).text
    assert fake.writes == []


def test_viewers_see_the_check_but_cannot_move(client, fake, monkeypatch):
    import tests.test_web as web
    monkeypatch.setattr(web, "USER", {**web.USER, "is_admin": False, "groups": []})
    client.cookies.clear()
    sign_in(client, fake)
    html = client.get("/partials/slots").text
    assert "1 to check" in html and "Move Tag to Spool" not in html and fake.probes == 0
    assert move(client).status_code == 403 and fake.writes == []


def test_cross_origin_move_is_refused(client, fake):
    r = client.post("/filament/slots/move", headers={"Origin": "https://evil.example"},
                    data={"printer_id": 1, "ams_id": 0, "tray_id": 0, "tag": f"{TAG}:{UUID}", "spool_id": 30})
    assert r.status_code == 403 and fake.writes == []
