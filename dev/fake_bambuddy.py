"""A stand-in for Bambuddy v1.3.1, for trying Platewright locally.

It imitates only what Platewright touches: the sidebar page that frames the app
(sandbox, theme and navigate messages), /connect/authorize (approves at once,
like an already-granted user), the token exchange and the badge endpoint.

    uv run uvicorn dev.fake_bambuddy:app --port 3700
    PLATEWRIGHT_DEV=1 PLATEWRIGHT_COOKIE_SECURE=0 PLATEWRIGHT_PUBLIC_URL=http://localhost:3710 \
      PLATEWRIGHT_BAMBUDDY_URL=http://localhost:3700 PLATEWRIGHT_BAMBUDDY_KEY=bb_dev \
      PLATEWRIGHT_CLIENT_ID=bba_dev PLATEWRIGHT_CLIENT_SECRET=bbs_dev \
      uv run uvicorn platewright.web.app:create_app --factory --port 3710

Then open http://localhost:3700.
"""

import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

APP_URL = "http://localhost:3710"
CLIENT_ID, CLIENT_SECRET, API_KEY = "bba_dev", "bbs_dev", "bb_dev"
USER = {"id": 1, "username": "devadmin", "email": "dev@example.test", "is_admin": True,
        "groups": ["Administrators"], "permissions": ["settings:read", "queue:update"]}

app = FastAPI()
codes: dict[str, str] = {}
badge: dict = {"count": None, "at": 0.0}

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fake Bambuddy</title>
<style>
 body{margin:0;font-family:system-ui;display:flex;height:100vh}
 nav{width:200px;background:#222;color:#eee;padding:12px;display:flex;flex-direction:column;gap:8px}
 nav a{color:#eee} #frame{flex:1;border:0} .b{background:#00ae42;border-radius:9px;padding:0 6px;margin-left:6px}
 #path{font-size:12px;color:#aaa}
</style></head><body>
<nav><b>Fake Bambuddy</b>
 <span>Platewright<span class="b" id="badge"></span></span>
 <label>mode <select id="mode"><option>light</option><option selected>dark</option></select></label>
 <label>accent <select id="accent"><option>green</option><option>blue</option><option>orange</option><option>purple</option></select></label>
 <label>background <select id="bg"><option>neutral</option><option>slate</option><option>forest</option><option>oled</option></select></label>
 <span id="path">at /</span>
</nav>
<iframe id="frame" src="APP_URL" sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"></iframe>
<script src="/fake.js"></script></body></html>"""

JS = """
const f = document.getElementById('frame'), origin = new URL(f.src).origin;
const send = () => f.contentWindow.postMessage({type:'bambuddy:theme', mode: mode.value, style:'classic',
  background: bg.value, accent: accent.value, canNavigate: true}, origin);
f.addEventListener('load', send);
[mode, accent, bg].forEach(el => el.addEventListener('change', send));
addEventListener('message', e => {
  if (e.source !== f.contentWindow) return;
  if (e.data?.type === 'bambuddy:theme-request') send();
  if (e.data?.type === 'bambuddy:navigate' && e.origin === origin) {
    document.getElementById('path').textContent = 'at ' + e.data.path; f.remove();
  }
});
const poll = async () => { const r = await (await fetch('/api/v1/external-links/')).json();
  document.getElementById('badge').textContent = r[0].badge_count ?? ''; };
poll(); setInterval(poll, 3000);
"""


@app.get("/", response_class=HTMLResponse)
def home():
    return PAGE.replace("APP_URL", APP_URL)


@app.get("/fake.js")
def fake_js():
    return HTMLResponse(JS, media_type="text/javascript")


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
    return {"user": USER, "issued_at": "2026-11-20T00:00:00Z"}


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
