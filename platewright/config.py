"""Settings, read once from PLATEWRIGHT_* environment variables."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit


class ConfigError(RuntimeError):
    pass


def _url(name: str, default: str = "") -> str:
    value = os.environ.get(name, default).strip().rstrip("/")
    if value and urlsplit(value).scheme not in ("http", "https"):
        raise ConfigError(f"{name} must be an http(s) URL, got {value!r}")
    return value


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


@dataclass(frozen=True)
class Settings:
    # Where browsers reach Platewright. The callback URL and the badge's link
    # address are built from it, so it must match what is registered in
    # Bambuddy exactly.
    public_url: str
    # Where browsers reach Bambuddy: the sign-in page and the origin that the
    # theme and navigate messages are exchanged with.
    bambuddy_url: str
    # Where this server reaches Bambuddy's API (LAN). Defaults to bambuddy_url.
    bambuddy_api_url: str
    bambuddy_key: str
    client_id: str
    client_secret: str
    session_key: str
    cookie_secure: bool
    session_days: int
    sync_interval: int
    data_dir: str
    seed_file: str
    webhook_secret: str = ""

    @property
    def bambuddy_origin(self) -> str:
        return origin_of(self.bambuddy_url)

    @property
    def callback_url(self) -> str:
        return f"{self.public_url}/auth/callback"

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "platewright.db")

    @property
    def sign_in_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


def load() -> Settings:
    dev = os.environ.get("PLATEWRIGHT_DEV", "") == "1"
    public_url = _url("PLATEWRIGHT_PUBLIC_URL", "http://localhost:3710" if dev else "")
    bambuddy_url = _url("PLATEWRIGHT_BAMBUDDY_URL", "http://localhost:3700" if dev else "")
    if not public_url or not bambuddy_url:
        raise ConfigError("PLATEWRIGHT_PUBLIC_URL and PLATEWRIGHT_BAMBUDDY_URL are required")
    session_key = os.environ.get("PLATEWRIGHT_SESSION_KEY", "")
    if not session_key:
        if not dev:
            raise ConfigError("PLATEWRIGHT_SESSION_KEY is required (PLATEWRIGHT_DEV=1 makes a throwaway one)")
        session_key = secrets.token_urlsafe(32)
    if len(session_key) < 32:
        raise ConfigError("PLATEWRIGHT_SESSION_KEY must be at least 32 characters")
    return Settings(
        public_url=public_url,
        bambuddy_url=bambuddy_url,
        bambuddy_api_url=_url("PLATEWRIGHT_BAMBUDDY_API_URL") or bambuddy_url,
        bambuddy_key=os.environ.get("PLATEWRIGHT_BAMBUDDY_KEY", "").strip(),
        client_id=os.environ.get("PLATEWRIGHT_CLIENT_ID", "").strip(),
        client_secret=os.environ.get("PLATEWRIGHT_CLIENT_SECRET", "").strip(),
        session_key=session_key,
        # Off only for plain-http local development.
        cookie_secure=os.environ.get("PLATEWRIGHT_COOKIE_SECURE", "1") != "0",
        session_days=int(os.environ.get("PLATEWRIGHT_SESSION_DAYS", "30")),
        # Sync and badge report, seconds. Bambuddy hides a badge older than 10 minutes.
        sync_interval=int(os.environ.get("PLATEWRIGHT_SYNC_INTERVAL")
                          or os.environ.get("PLATEWRIGHT_BADGE_INTERVAL") or "300"),
        data_dir=os.environ.get("PLATEWRIGHT_DATA_DIR", "./data" if dev else "/data"),
        # Settings for an empty database (JSON); afterwards the database owns them.
        seed_file=os.environ.get("PLATEWRIGHT_SEED_FILE", ""),
        # Bearer secret of the Bambuddy webhook provider that nudges a sync (empty = no webhook).
        webhook_secret=os.environ.get("PLATEWRIGHT_WEBHOOK_SECRET", "").strip(),
    )
