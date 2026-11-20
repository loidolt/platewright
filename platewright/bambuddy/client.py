"""Bambuddy API: the connected-app sign-in and the sidebar badge.

Written against Bambuddy v1.3.1 (backend/app/api/routes/connected_apps.py and
external_links.py). Sign-in is an OAuth 2.0 authorization-code flow with PKCE,
S256 only:

1. The browser goes to ``<bambuddy>/connect/authorize`` (an SPA page) with our
   client_id, callback URL, state and code challenge. After a one-time consent
   per user, Bambuddy sends the browser to the callback with a code that lives
   60 seconds.
2. This server swaps the code at ``POST /api/v1/connect/token`` with the
   client secret and the PKCE verifier, and gets the user's identity.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from platewright.config import Settings


def new_verifier() -> str:
    # 64 random bytes -> 86 base64url characters (RFC 7636 allows 43-128).
    return secrets.token_urlsafe(64)


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(settings: Settings, *, state: str, verifier: str) -> str:
    query = urlencode(
        {
            "client_id": settings.client_id,
            "redirect_uri": settings.callback_url,
            "state": state,
            "code_challenge": s256(verifier),
            "code_challenge_method": "S256",
        }
    )
    return f"{settings.bambuddy_url}/connect/authorize?{query}"


@dataclass(frozen=True)
class BambuddyUser:
    id: int
    username: str
    email: str | None
    is_admin: bool
    groups: list[str]
    permission_count: int


class SignInError(Exception):
    """The token exchange failed. ``error`` is Bambuddy's code when it gave one
    (invalid_client, invalid_grant, auth_disabled), else a short reason."""

    def __init__(self, error: str, status: int | None = None):
        super().__init__(error)
        self.error = error
        self.status = status


class Bambuddy:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._http = httpx.AsyncClient(
            base_url=f"{settings.bambuddy_api_url}/api/v1",
            timeout=httpx.Timeout(10.0),
            transport=transport,
            headers={"User-Agent": "platewright/0"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def exchange_code(self, code: str, verifier: str) -> BambuddyUser:
        try:
            response = await self._http.post(
                "/connect/token",
                json={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.settings.callback_url,
                    "client_id": self.settings.client_id,
                    "client_secret": self.settings.client_secret,
                    "code_verifier": verifier,
                },
            )
        except httpx.HTTPError as exc:
            raise SignInError(f"Bambuddy unreachable: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise SignInError(_error_code(response), response.status_code)
        user = response.json()["user"]
        return BambuddyUser(
            id=int(user["id"]),
            username=user["username"],
            email=user.get("email"),
            is_admin=bool(user["is_admin"]),
            groups=list(user.get("groups", [])),
            permission_count=len(user.get("permissions", [])),
        )

    async def set_badge(self, count: int) -> int:
        """Set the count on the sidebar links pointing at Platewright. Returns how
        many links Bambuddy updated. Raises httpx.HTTPStatusError on refusal
        (401/403: key or its permission; 404: no link has our address)."""
        response = await self._http.put(
            "/external-links/badge",
            json={"url": self.settings.public_url, "count": count},
            headers={"X-API-Key": self.settings.bambuddy_key},
        )
        response.raise_for_status()
        return int(response.json().get("links", 0))

    # -- reads with the API key (Read status) ----------------------------------------------

    async def get(self, path: str, **params):
        """GET an API path with the key. Raises BambuddyError on any failure."""
        try:
            response = await self._http.get(path, params={k: v for k, v in params.items() if v is not None},
                                            headers={"X-API-Key": self.settings.bambuddy_key})
        except httpx.HTTPError as exc:
            raise BambuddyError(f"Bambuddy unreachable: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise BambuddyError(f"GET {path}: HTTP {response.status_code} {_error_code(response)}",
                                response.status_code)
        try:
            return response.json()
        except ValueError as exc:
            raise BambuddyError(f"GET {path}: not JSON") from exc

    async def print_log(self, since: str | None) -> list[dict]:
        """Every print-log entry since a date (YYYY-MM-DD), oldest first."""
        out: list[dict] = []
        offset = 0
        while True:
            page = await self.get("/print-log/", limit=200, offset=offset, sort_dir="asc",
                                  date_from=f"{since}T00:00:00" if since else None) or {}
            items = page.get("items") or []
            out += items
            offset += len(items)
            if not items or offset >= int(page.get("total") or 0):
                return sorted(out, key=lambda e: (e.get("started_at") or e.get("created_at") or "", e["id"]))

    async def archive(self, archive_id: int) -> dict:
        return await self.get(f"/archives/{archive_id}") or {}

    async def spools(self) -> list[dict]:
        return await self.get("/inventory/spools") or []

    async def printers(self) -> list[dict]:
        return await self.get("/printers/") or []

    async def printer_status(self, printer_id: int) -> dict:
        return await self.get(f"/printers/{printer_id}/status") or {}

    # -- writes with the API key (Queue, Manage library, Manage projects) -----------------

    async def send(self, method: str, path: str, *, json=None, params: dict | None = None, files=None,
                   timeout: float = 30):
        try:
            response = await self._http.request(
                method, path, json=json, files=files, timeout=timeout,
                params={k: v for k, v in (params or {}).items() if v is not None},
                headers={"X-API-Key": self.settings.bambuddy_key})
        except httpx.HTTPError as exc:
            raise BambuddyError(f"Bambuddy unreachable: {type(exc).__name__}") from exc
        if response.status_code not in (200, 201):
            hint = " (does the API key have this permission?)" if response.status_code in (401, 403) else ""
            raise BambuddyError(f"{method} {path}: HTTP {response.status_code} {_error_code(response)}{hint}",
                                response.status_code)
        try:
            return response.json() if response.content else None
        except ValueError:
            return None

    async def makerworld(self, url: str) -> dict:
        """What Bambuddy's MakerWorld resolver says about a model page: its title, designer and
        license as MakerWorld states them, and the File Manager files already imported from it."""
        res = await self.send("POST", "/makerworld/resolve", json={"url": url}, timeout=20) or {}
        design = res.get("design") if isinstance(res.get("design"), dict) else {}
        creator = design.get("designCreator") if isinstance(design.get("designCreator"), dict) else {}
        return {"title": _text(design.get("title")),
                "designer": _text(creator.get("name")) or _text(creator.get("handle")),
                "license": _text(design.get("license")),
                "file_ids": [i for i in res.get("already_imported_library_ids") or [] if isinstance(i, int)]}

    async def folder_id(self, name: str) -> int:
        """A top-level library folder by name, created if missing."""
        for f in await self.get("/library/folders") or []:
            if f.get("name") == name and not f.get("parent_id"):
                return int(f["id"])
        return int((await self.send("POST", "/library/folders", json={"name": name}))["id"])

    async def library_files(self, folder_id: int | None) -> list[dict]:
        return await self.get("/library/files", folder_id=folder_id, include_root=False) or []

    async def library_file(self, file_id: int) -> dict:
        return await self.get(f"/library/files/{file_id}") or {}

    async def upload(self, filename: str, data: bytes, folder_id: int | None) -> dict:
        return await self.send("POST", "/library/files", params={"folder_id": folder_id},
                               files={"file": (filename, data, "application/octet-stream")}, timeout=300)

    async def move_files(self, file_ids: list[int], folder_id: int | None) -> None:
        res = await self.send("POST", "/library/files/move", json={"file_ids": file_ids, "folder_id": folder_id}) or {}
        moved = res.get("moved")
        if moved is not None and moved < len(file_ids):
            # Bambuddy answers 200 for skipped files (not owner, read-only source, name taken).
            raise BambuddyError(f"Bambuddy didn't move library file(s) {file_ids}: {res.get('skipped') or res}")

    async def create_project(self, body: dict) -> dict:
        return await self.send("POST", "/projects/", json=body)

    async def update_project(self, project_id: int, body: dict) -> dict:
        return await self.send("PATCH", f"/projects/{project_id}", json=body)

    async def add_to_queue(self, body: dict) -> dict:
        return await self.send("POST", "/queue/", json=body)


class BambuddyError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _text(value) -> str:
    return value.strip()[:200] if isinstance(value, str) else ""


def _error_code(response: httpx.Response) -> str:
    # FastAPI wraps HTTPException details: {"detail": {"error": "..."}} or
    # {"detail": "..."}; anything else is reported by status.
    try:
        detail = response.json().get("detail")
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(detail, dict) and "error" in detail:
        return str(detail["error"])
    if isinstance(detail, str):
        return detail
    return f"HTTP {response.status_code}"
