"""A stand-in for Bambuddy v1.3.1, for trying Platewright locally.

It imitates only what Platewright touches: the sidebar page that frames the app
(sandbox, theme and navigate messages), /connect/authorize (approves at once,
like an already-granted user), the token exchange, the badge endpoint, and sample
print log, archives, spools, printers, File Manager, MakerWorld, projects and queue.

    uv run uvicorn dev.fake_bambuddy:app --port 3700
    PLATEWRIGHT_DEV=1 PLATEWRIGHT_COOKIE_SECURE=0 PLATEWRIGHT_PUBLIC_URL=http://localhost:3710 \
      PLATEWRIGHT_BAMBUDDY_URL=http://localhost:3700 PLATEWRIGHT_BAMBUDDY_KEY=bb_dev \
      PLATEWRIGHT_CLIENT_ID=bba_dev PLATEWRIGHT_CLIENT_SECRET=bbs_dev \
      uv run uvicorn platewright.web.app:create_app --factory --port 3710

Then open http://localhost:3700. Its sidebar switches the theme (mode, style, background,
accent) and who is signed in (admin, operator, viewer). Plate fingerprints come from
./data/fake_bambuddy.json, which dev/seed_dev.py writes (or FAKE_PLATE_SHA for plate N).
"""

import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

APP_URL = "http://localhost:3710"
CLIENT_ID, CLIENT_SECRET, API_KEY = "bba_dev", "bbs_dev", "bb_dev"
USERS = {
    "admin": {"id": 1, "username": "devadmin", "email": "dev@example.test", "is_admin": True,
              "groups": ["Administrators"], "permissions": ["settings:read", "queue:update"]},
    "operator": {"id": 2, "username": "devoperator", "email": "op@example.test", "is_admin": False,
                 "groups": ["Platewright operators"], "permissions": []},
    "viewer": {"id": 3, "username": "devviewer", "email": "view@example.test", "is_admin": False,
               "groups": [], "permissions": []},
}
current = {"role": os.environ.get("FAKE_ROLE", "admin")}

app = FastAPI()
codes: dict[str, str] = {}
badge: dict = {"count": None, "at": 0.0}

OPTIONS = {
    "mode": ["dark", "light"],
    "style": ["classic", "glow", "vibrant"],
    "background": ["neutral", "warm", "cool", "oled", "slate", "forest"],
    "accent": ["green", "teal", "blue", "orange", "purple", "red"],
    "role": list(USERS),
}

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fake Bambuddy</title>
<style>
 body{margin:0;font-family:system-ui;display:flex;height:100vh;background:#1a1a1a}
 nav{width:200px;background:#222;color:#eee;padding:12px;display:flex;flex-direction:column;gap:8px;font-size:14px}
 nav a{color:#eee} #frame{flex:1;border:0;min-width:0} .b{background:#00ae42;border-radius:9px;padding:0 6px;margin-left:6px}
 #path{font-size:12px;color:#aaa} label{display:flex;justify-content:space-between;gap:6px}
 @media (max-width:767px){body{flex-direction:column}nav{width:auto;flex-direction:row;flex-wrap:wrap;padding:6px;gap:6px;font-size:12px}
   nav b{display:none}}
</style></head><body>
<nav><b>Fake Bambuddy</b>
 <span>Platewright<span class="b" id="badge"></span></span>
 SELECTS
 <span id="path">at /</span>
</nav>
<iframe id="frame" src="APP_URL" sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"></iframe>
<script src="/fake.js"></script></body></html>"""

JS = """
const f = document.getElementById('frame'), origin = new URL(f.src).origin;
const pick = id => document.getElementById(id);
const saved = JSON.parse(localStorage.getItem('fake-theme') || '{}');
['mode','style','background','accent'].forEach(k => { if (saved[k]) pick(k).value = saved[k]; });
const send = () => {
  const t = {mode: pick('mode').value, style: pick('style').value, background: pick('background').value,
             accent: pick('accent').value};
  localStorage.setItem('fake-theme', JSON.stringify(t));
  f.contentWindow.postMessage({type:'bambuddy:theme', ...t, canNavigate: true}, origin);
};
f.addEventListener('load', send);
['mode','style','background','accent'].forEach(k => pick(k).addEventListener('change', send));
pick('role').addEventListener('change', async () => {
  await fetch('/fake/role', {method: 'POST', headers: {'content-type': 'application/json'},
                             body: JSON.stringify({role: pick('role').value})});
  f.src = origin + '/auth/login';
});
addEventListener('message', e => {
  if (e.source !== f.contentWindow) return;
  if (e.data?.type === 'bambuddy:theme-request') send();
  if (e.data?.type === 'bambuddy:navigate' && e.origin === origin) {
    document.getElementById('path').textContent = 'navigated to ' + e.data.path;
  }
});
const poll = async () => { const r = await (await fetch('/api/v1/external-links/')).json();
  document.getElementById('badge').textContent = r[0].badge_count ?? ''; };
poll(); setInterval(poll, 3000);
"""


def _selects() -> str:
    out = []
    for name, values in OPTIONS.items():
        opts = "".join(f"<option{' selected' if name == 'role' and v == current['role'] else ''}>{v}</option>"
                       for v in values)
        out.append(f'<label>{name} <select id="{name}">{opts}</select></label>')
    return "\n ".join(out)


@app.get("/", response_class=HTMLResponse)
def home():
    return PAGE.replace("APP_URL", APP_URL).replace("SELECTS", _selects())


@app.get("/fake.js")
def fake_js():
    return HTMLResponse(JS, media_type="text/javascript")


@app.post("/fake/role")
def set_role(body: dict = Body(...)):
    if body.get("role") not in USERS:
        raise HTTPException(400)
    current["role"] = body["role"]
    return {"role": current["role"]}


@app.get("/connect/authorize")
def authorize(client_id: str, redirect_uri: str, state: str, code_challenge: str, code_challenge_method: str):
    if client_id != CLIENT_ID or redirect_uri != f"{APP_URL}/auth/callback" or code_challenge_method != "S256":
        return HTMLResponse("Can't sign you in", status_code=400)
    code = secrets.token_urlsafe(16)
    codes[code] = code_challenge
    return RedirectResponse(f"{redirect_uri}?{urlencode({'code': code, 'state': state})}", status_code=302,
                            headers={"Content-Security-Policy": "frame-ancestors 'self'"})


@app.post("/api/v1/connect/token")
def token(body: dict = Body(...)):
    challenge = codes.pop(body.get("code", ""), None)
    digest = hashlib.sha256(body.get("code_verifier", "").encode()).digest()
    ok = challenge and base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == challenge
    if body.get("client_secret") != CLIENT_SECRET:
        return JSONResponse({"detail": {"error": "invalid_client"}}, 401)
    if not ok or body.get("redirect_uri") != f"{APP_URL}/auth/callback":
        return JSONResponse({"detail": {"error": "invalid_grant"}}, 400)
    return {"user": USERS[current["role"]], "issued_at": "2026-11-20T00:00:00Z"}


@app.put("/api/v1/external-links/badge")
def set_badge(body: dict = Body(...), x_api_key: str = Header("")):
    if x_api_key != API_KEY:
        raise HTTPException(401)
    if body.get("url", "").rstrip("/") != APP_URL:
        raise HTTPException(404, "No sidebar link points at this address")
    badge.update(count=body["count"], at=time.time())
    return {"links": 1}


@app.get("/api/v1/external-links/")
def links(request: Request):
    fresh = time.time() - badge["at"] < 600
    return [{"id": 1, "name": "Platewright", "url": APP_URL, "badge_count": badge["count"] if fresh else None}]


# -- reads and writes Platewright makes with the API key (sample data) -----------------------

def _key(x_api_key: str) -> None:
    if x_api_key != API_KEY:
        raise HTTPException(401)


def _shas() -> dict:
    path = Path(os.environ.get("PLATEWRIGHT_DATA_DIR", "./data")) / "fake_bambuddy.json"
    try:
        shas = json.loads(path.read_text()).get("plates", {})
    except (OSError, ValueError):
        shas = {}
    if os.environ.get("FAKE_PLATE_SHA"):
        shas["N"] = os.environ["FAKE_PLATE_SHA"]
    return shas


def _day(offset: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() + offset * 86400))


# archive id: (file name, plate code or None, days ago, verdict, status)
ARCHIVES = {
    1: ("PLATE-N_44up.gcode.3mf", "N", 0, "good", "completed"),
    2: ("PLATE-N_44up.gcode.3mf", "N", 1, None, "completed"),
    3: ("Benchy.gcode.3mf", None, 1, None, "completed"),
    4: ("PLATE-MX_24up.gcode.3mf", "MX", 2, "good", "completed"),
    5: ("PLATE-S_30up.gcode.3mf", "S", 4, "good", "completed"),
    6: ("PLATE-S_30up.gcode.3mf", "S", 6, "reject", "completed"),
    7: ("Calibration cube.gcode.3mf", None, 7, None, "completed"),
}


@app.get("/api/v1/print-log/")
def print_log(x_api_key: str = Header("")):
    _key(x_api_key)
    items = [{"id": 100 + aid, "archive_id": aid, "print_name": name.removesuffix(".gcode.3mf"),
              "printer_name": "X1C", "status": status, "started_at": f"{_day(-ago)}T01:00:00Z",
              "completed_at": f"{_day(-ago)}T12:00:00Z", "duration_seconds": 39600, "user_verdict": verdict}
             for aid, (name, _code, ago, verdict, status) in ARCHIVES.items()]
    return {"items": items, "total": len(items)}


@app.get("/api/v1/archives/{archive_id}")
def archive(archive_id: int, x_api_key: str = Header("")):
    _key(x_api_key)
    if archive_id not in ARCHIVES:
        raise HTTPException(404)
    name, code, *_ = ARCHIVES[archive_id]
    return {"id": archive_id, "filename": name,
            "content_hash": _shas().get(code, "") if code else hashlib.sha256(name.encode()).hexdigest()}


@app.get("/api/v1/inventory/spools")
def spools(x_api_key: str = Header("")):
    _key(x_api_key)
    return [
        {"id": 1, "material": "PLA", "rgba": "9D432CFF", "brand": "Bambu", "color_name": "Brown",
         "label_weight": 1000, "weight_used": 380},
        {"id": 2, "material": "PLA", "rgba": "FFFFFFFF", "brand": "Bambu", "color_name": "White",
         "label_weight": 1000, "weight_used": 900},
        {"id": 4, "material": "PLA", "rgba": "C12E1FFF", "brand": "Bambu", "color_name": "Red",
         "label_weight": 1000, "weight_used": 200},
        {"id": 3, "material": "PETG", "rgba": "2E7D32FF", "brand": "Elegoo", "color_name": "Green",
         "label_weight": 1000, "weight_used": 120},
    ]


PRINTERS = {
    1: ("X1C", {"connected": True, "state": "RUNNING", "subtask_name": "PLATE-N_44up", "progress": 63.0,
                "remaining_time": 512}),
    2: ("P1S", {"connected": True, "state": "IDLE"}),
    3: ("A1 mini", {"connected": False, "state": "IDLE"}),
}


@app.get("/api/v1/printers/")
def printers(x_api_key: str = Header("")):
    _key(x_api_key)
    return [{"id": pid, "name": name} for pid, (name, _) in PRINTERS.items()]


@app.get("/api/v1/printers/{printer_id}/status")
def printer_status(printer_id: int, x_api_key: str = Header("")):
    _key(x_api_key)
    return PRINTERS[printer_id][1]


FOLDERS = [{"id": 1, "name": "Models", "parent_id": None, "children": [
    {"id": 4, "name": "MakerWorld", "parent_id": 1, "children": []}]},
    {"id": 2, "name": "Production plates", "parent_id": None, "children": []},
    {"id": 3, "name": "Retired plates", "parent_id": None, "children": []}]
FILES = {
    11: {"id": 11, "filename": "Nunu.3mf", "folder_id": 4},
    12: {"id": 12, "filename": "Star.3mf", "folder_id": 1},
    13: {"id": 13, "filename": "Bunny v2.3mf", "folder_id": 4},
    14: {"id": 14, "filename": "Rowan.stl", "folder_id": None},
    2: {"id": 2, "filename": "PLATE-N_44up.gcode.3mf", "folder_id": 2},
    4: {"id": 4, "filename": "PLATE-MX_24up.gcode.3mf", "folder_id": 2},
}
COLORS = {11: "#9D432C", 12: "#C12E1F", 13: "#E8D5B7", 14: "#6B8E23", 2: "#9D432C", 4: "#C12E1F"}


@app.get("/api/v1/library/folders")
def folders(x_api_key: str = Header("")):
    _key(x_api_key)
    return FOLDERS


@app.post("/api/v1/library/folders")
def new_folder(body: dict = Body(...), x_api_key: str = Header("")):
    _key(x_api_key)
    f = {"id": 10 + len(FOLDERS), "name": body["name"], "parent_id": None, "children": []}
    FOLDERS.append(f)
    return f


def _in(folder_id: int | None, recursive: bool) -> set:
    ids = {folder_id}
    if recursive:
        for top in FOLDERS:
            if top["id"] == folder_id:
                ids |= {c["id"] for c in top["children"]}
    return ids


@app.get("/api/v1/library/files")
def files(folder_id: int | None = None, recursive: bool = False, x_api_key: str = Header("")):
    _key(x_api_key)
    ids = _in(folder_id, recursive)
    return [f for f in FILES.values() if f["folder_id"] in ids]


@app.get("/api/v1/library/files/{file_id}")
def library_file(file_id: int, x_api_key: str = Header("")):
    _key(x_api_key)
    if file_id not in FILES:
        raise HTTPException(404)
    return FILES[file_id]


@app.get("/api/v1/library/files/{file_id}/thumbnail")
def thumbnail(file_id: int, x_api_key: str = Header("")):
    _key(x_api_key)
    color = COLORS.get(file_id, "#888888")
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" fill="#f0f0f0"/>'
           f'<ellipse cx="32" cy="38" rx="18" ry="16" fill="{color}"/><circle cx="32" cy="20" r="10" fill="{color}"/></svg>')
    return Response(svg, media_type="image/svg+xml")


@app.post("/api/v1/library/files")
def upload(request: Request, folder_id: int | None = None, x_api_key: str = Header("")):
    _key(x_api_key)
    fid = max(FILES) + 1
    FILES[fid] = {"id": fid, "filename": f"upload-{fid}.gcode.3mf", "folder_id": folder_id}
    return FILES[fid]


@app.post("/api/v1/library/files/move")
def move(body: dict = Body(...), x_api_key: str = Header("")):
    _key(x_api_key)
    for fid in body.get("file_ids", []):
        if fid in FILES:
            FILES[fid]["folder_id"] = body.get("folder_id")
    return {"moved": len(body.get("file_ids", []))}


@app.post("/api/v1/makerworld/resolve")
def makerworld(body: dict = Body(...), x_api_key: str = Header("")):
    _key(x_api_key)
    return {"design": {"title": "Bunny v2", "license": "CC BY-NC 4.0",
                       "designCreator": {"name": "Hopper Designs", "handle": "@hopper"}},
            "already_imported_library_ids": [13]}


projects: dict[int, dict] = {}


@app.post("/api/v1/projects/")
def new_project(body: dict = Body(...), x_api_key: str = Header("")):
    _key(x_api_key)
    pid = 20 + len(projects)
    projects[pid] = {"id": pid, **body}
    return projects[pid]


@app.patch("/api/v1/projects/{project_id}")
def update_project(project_id: int, body: dict = Body(...), x_api_key: str = Header("")):
    _key(x_api_key)
    projects.setdefault(project_id, {"id": project_id}).update(body)
    return projects[project_id]


queue: list[dict] = []


@app.post("/api/v1/queue/")
def add_to_queue(body: dict = Body(...), x_api_key: str = Header("")):
    _key(x_api_key)
    queue.append(body)  # a stand-in: nothing prints
    return {"id": 300 + len(queue), **body}


@app.post("/api/v1/notifications/app-message")
def app_message(body: dict = Body(...), x_api_key: str = Header("")):
    _key(x_api_key)
    return {"channels": 1}
