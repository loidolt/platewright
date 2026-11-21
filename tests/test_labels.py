"""Labels: drawing, Brother raster data, sending to port 9100, and the label routes."""

import asyncio
import datetime as dt
import io
import json

import httpx
import pytest
from PIL import Image

from platewright import labels
from platewright.core.settings import Settings
from platewright.web.routes.labels import bag_content
from tests.test_web import client, fake, sign_in, store  # noqa: F401  (fixtures)

LABELS_ON = {"label_printer_host": "10.0.0.9", "label_printer_model": "QL-810W", "label_size": "29x90"}


# -- drawing and raster -----------------------------------------------------------------------

def test_sizes_are_landscape_and_match_the_printer():
    s = labels.size("29x90")
    assert (s.width, s.height, s.across, s.endless) == (991, 306, 306, False)
    assert s.mm == (83.9, 25.9)  # Bambuddy's custom layout takes at least 40 x 20 mm
    wide = labels.size("62")
    assert (wide.width, wide.height, wide.across, wide.endless) == (1063, 696, 696, True)
    assert all(z.width >= z.height for z in labels.sizes())
    with pytest.raises(labels.LabelError):
        labels.size("nope")


def test_problems():
    assert labels.problems("", "anything", "anything") == []
    assert labels.problems("10.20.10.174", "QL-810W", "29x90") == []
    assert len(labels.problems("http://10.0.0.9", "QL-1", "zz", "lpd")) == 4


def test_bag_label_is_the_label_size_and_has_ink():
    spec = labels.size("29x90")
    img = labels.bag_label(spec, title="Spring fair with a name far too long to fit on one label at all",
                           headline="12 × Nunu", lines=["Event · due Dec 5, 2026"], qr_url="https://x.test/projects/2")
    assert img.size == (991, 306) and img.mode == "L"
    assert img.getextrema() == (0, 255)
    # the QR code sits on the right, the text on the left
    assert min(img.crop((700, 0, 991, 306)).getdata()) == 0


@pytest.mark.parametrize("label,landscape", [("29x90", (991, 306)), ("62", (1063, 696)), ("102x51", (1164, 526))])
def test_raster_for_die_cut_and_continuous(label, landscape):
    spec = labels.size(label)
    img = Image.new("L", landscape, 255)
    data = labels.raster(img, "QL-810W" if label != "102x51" else "QL-1110NWB", spec, copies=2)
    assert data.startswith(b"\x1bia\x01")  # raster mode
    assert data.count(b"\x1biz") == 2  # print information, once per copy (blank picture: no look-alikes)
    assert data.endswith(b"\x1a")  # print and feed after the last copy


def test_fit_rotates_and_pads_a_portrait_picture():
    spec = labels.size("29x90")
    out = labels.fit(Image.new("RGB", (300, 980), "white"), spec)
    assert out.size == (991, 306) and out.mode == "L"


def test_send_writes_everything_to_the_port():
    got = bytearray()

    async def run():
        async def handle(reader, writer):
            got.extend(await reader.read())
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        await labels.send_raw("127.0.0.1", b"x" * 50000, port=port)
        await asyncio.sleep(0.05)
        server.close()

    asyncio.run(run())
    assert bytes(got) == b"x" * 50000


def test_ipp_print_job_is_sent_and_checked():
    seen = []

    def printer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = request.content
        assert body[:4] == b"\x01\x01\x00\x02"  # IPP 1.1 Print-Job
        assert b"application/octet-stream" in body and b"ipp://10.0.0.9:631/ipp/print" in body
        assert body.endswith(b"\x03RASTER")
        status = 0x0000 if len(seen) == 1 else 0x0400  # then client-error-bad-request
        return httpx.Response(200, content=b"\x01\x01" + status.to_bytes(2, "big") + body[4:8] + b"\x03")

    t = httpx.MockTransport(printer)
    asyncio.run(labels.send_ipp("10.0.0.9", b"RASTER", transport=t))
    assert str(seen[0].url) == "http://10.0.0.9:631/ipp/print"
    assert seen[0].headers["content-type"] == "application/ipp"
    with pytest.raises(labels.LabelError, match="refused"):
        asyncio.run(labels.send_ipp("10.0.0.9", b"RASTER", transport=t))


def test_send_reports_an_unreachable_printer():
    with pytest.raises(labels.LabelError, match="can't reach"):
        asyncio.run(labels.send_raw("127.0.0.1", b"x", port=1, timeout=2))


def test_bag_content():
    s = Settings.from_values({"unit_name": "toy"})
    d = {"name": "Spring fair", "kind": "event", "due": "2026-12-05"}
    model = {"name": "Nunu", "min_age": 3, "small_parts": "yes"}
    c = bag_content(d, model, 12, s, dt.date(2026, 11, 21))
    assert c == {"title": "Spring fair", "headline": "12 × Nunu",
                 "lines": ["Event · due Dec 5, 2026", "Packed Nov 21, 2026", "Ages 3+ · Small parts"]}
    assert bag_content(d, None, None, s, dt.date(2026, 11, 21))["headline"] == "Mixed toys"


# -- routes -----------------------------------------------------------------------------------

@pytest.fixture
def sent(monkeypatch):
    """What the routes print: (picture, host, model, label size, copies)."""
    calls = []

    async def fake_print(img, host, model, label, copies=1, protocol="ipp"):
        labels.raster(img, model, labels.size(label), copies)  # the real raster still has to work
        calls.append((img, host, model, label, copies))

    monkeypatch.setattr(labels, "print_label", fake_print)
    return calls


def add_demand(store):
    store.save_demand("fair", name="Spring fair", kind="event", due="2026-12-05", target=0, notes="",
                      needs={"nunu": 10}, by="chris")


def test_bag_label_preview_and_print(client, fake, store, sent):
    sign_in(client, fake)
    add_demand(store)
    page = client.get("/demand/fair").text
    assert "Bag Label" in page and "/demand/fair/label.png?model=nunu" in page
    assert "No label printer is set up" in page
    r = client.get("/demand/fair/label.png", params={"model": "nunu", "qty": "12"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(r.content)).size == (991, 306)
    frag = client.get("/demand/fair/label/preview", params={"model": "_mixed", "qty": ""}).text
    assert 'id="label-preview"' in frag and "model=_mixed" in frag
    assert client.get("/demand/fair/label.png", params={"model": "ghost"}).status_code == 400

    r = client.post("/demand/fair/label", data={"model": "nunu", "qty": "12", "copies": "1"})
    assert "tone=bad" in r.headers["location"] and not sent  # no printer yet

    store.set_settings(LABELS_ON)
    r = client.post("/demand/fair/label", data={"model": "nunu", "qty": "12", "copies": "3"})
    assert r.status_code == 303 and "tone" not in r.headers["location"]
    (img, host, model, label, copies), = sent
    assert (img.size, host, model, label, copies) == ((991, 306), "10.0.0.9", "QL-810W", "29x90", 3)


def test_spool_label_asks_bambuddy_for_the_picture(client, fake, store, sent):
    asked = []
    handler = fake.handler

    def with_labels(request: httpx.Request):
        if request.url.path == "/api/v1/inventory/labels/preview":
            asked.append(json.loads(request.content))
            return httpx.Response(200, content=labels.png(Image.new("RGB", (991, 306), "white")),
                                  headers={"content-type": "image/png"})
        return handler(request)

    fake.handler = with_labels
    client.app.state.ctx.bambuddy._http._transport = httpx.MockTransport(with_labels)
    fake.spools = [{"id": 15, "material": "PC", "rgba": "000000FF", "brand": "Bambu Lab", "color_name": "Black",
                    "label_weight": 1000, "weight_used": 0}]
    store.set_settings(LABELS_ON)
    sign_in(client, fake)
    assert "/filament/spools/15/label" in client.get("/filament").text
    r = client.post("/filament/spools/15/label")
    assert r.status_code == 303 and "spool+%2315" in r.headers["location"]
    assert asked[0]["template"] == "custom" and asked[0]["layout"]["label_w_mm"] == 83.9
    assert asked[0]["spool_id"] == 15 and asked[0]["monochrome"] is True
    assert len(sent) == 1


def test_label_printer_settings_are_checked(client, fake, store, sent):
    sign_in(client, fake)
    page = client.get("/settings").text
    assert 'name="label_printer_host"' in page and "29 × 90 mm die-cut" in page
    printer = {"unit_name": "toy", "printer_key": "X1C", "printer_model": "Bambu Lab X1 Carbon", "printer_model_id": "",
               "printer_per_day": "1", "printer_bambuddy_name": "",
               "color_name": "White", "color_hex": "#FFFFFF", "color_material": "PLA", "color_slot": "A1",
               "color_backup": ""}
    r = client.post("/settings", data={**printer, "label_printer_host": "http://printer",
                                       "label_printer_model": "QL-810W", "label_size": "29x90"})
    assert r.status_code == 400 and "only its address" in r.text
    r = client.post("/settings", data={**printer, "label_printer_host": "10.0.0.9", "label_printer_model": "QL-810W",
                                       "label_size": "29x90"})
    assert r.status_code == 303 and store.settings().labels_on
    r = client.post("/settings/test-label")
    assert r.status_code == 303 and len(sent) == 1


def test_label_buttons_from_htmx_answer_with_a_toast_only(client, fake, store, sent):
    """Printing keeps the page where it is: htmx gets a toast to add, not a redirect."""
    sign_in(client, fake)
    add_demand(store)
    hx = {"HX-Request": "true"}
    r = client.post("/demand/fair/label", data={"model": "nunu", "qty": "2", "copies": "1"}, headers=hx)
    assert r.status_code == 200 and "location" not in r.headers
    assert 'hx-swap-oob="beforeend:.toasts"' in r.text and "no label printer is set up" in r.text and 'role="alert"' in r.text
    store.set_settings(LABELS_ON)
    r = client.post("/demand/fair/label", data={"model": "nunu", "qty": "2", "copies": "1"}, headers=hx)
    assert r.status_code == 200 and "1 bag label sent" in r.text and 'role="status"' in r.text
    r = client.post("/settings/test-label", headers=hx)
    assert r.status_code == 200 and "Test label sent" in r.text and len(sent) == 2
    page = client.get("/demand/fair").text
    assert 'hx-post="/demand/fair/label" hx-swap="none"' in page
