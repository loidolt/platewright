# Platewright

Repeatable, reliable print plates, as an app in [Bambuddy](https://github.com/maziggy/bambuddy)'s
sidebar.

Bambuddy runs the printers: library, queue, prints, the good / reject verdict, spools. Platewright
adds what a print farm needs to print the same plates again and again with known results:

- **models** with their license and distribution terms, and a test-print gate before a model may go
  on a production plate;
- **plates** checked against your rules (printer, palette, hours, linked models) and then **locked**
  as a version, so a reprint is the plate you tested;
- **yield**: parts per plate by model, parts that failed on an otherwise good plate, and stock on
  hand;
- **demand**: orders, events or restock targets, and how many plates cover them.

People sign in with their Bambuddy account. Platewright opens inside Bambuddy's frame, follows its
theme, and links back to Bambuddy's own pages. It uses only the integration points Bambuddy
documents for sidebar apps ([external links](https://wiki.bambuddy.cool/features/external-links/),
[connected apps](https://wiki.bambuddy.cool/features/connected-apps/)) and its REST API.

**Status: phase 0**, written against Bambuddy v1.3.1. It is a hello page that proves the
sidebar-app setup before any pipeline code exists:

1. **Sign in with Bambuddy** (connected app, OAuth code flow with PKCE) inside Bambuddy's frame. It
   shows the username, groups and `is_admin`. Platewright keeps its own `SameSite=Lax` session
   cookie.
2. **Follows Bambuddy's theme** from the `bambuddy:theme` message, asking with
   `bambuddy:theme-request`, and only from Bambuddy's origin.
3. **Open in Bambuddy**: `{"type": "bambuddy:navigate", "path": "/queue"}`.
4. **Sidebar badge**: `PUT /api/v1/external-links/badge` from the server every 5 minutes.
5. `Content-Security-Policy: frame-ancestors <Bambuddy's origin>`.

## Hosting

Put Platewright on a sibling host of Bambuddy, for example `plates.example.com` next to
`print.example.com`. Browsers then treat its cookie as first-party inside Bambuddy's frame. On an
unrelated domain the cookie is third-party, and Safari and Firefox drop it.

Server-side calls (the sign-in token exchange and the badge) can go to Bambuddy over the LAN
(`PLATEWRIGHT_BAMBUDDY_API_URL`).

## Setup in Bambuddy (as admin)

1. **Connected app:** Settings → API Keys → Connected Apps. Name `Platewright`, callback URL
   `<PLATEWRIGHT_PUBLIC_URL>/auth/callback`. Copy the client ID and the secret; the secret is shown
   only once.
2. **API key:** Settings → API Keys → Create Key. Turn on only **Read Status** and **Send
   notifications** (Manage Queue is on by default). Create it as an admin: Send notifications needs
   an owner who may update notifications.
3. **Sidebar link:** Settings → General → Sidebar → External links. URL exactly
   `PLATEWRIGHT_PUBLIC_URL` (the badge is matched to the link by scheme, host, port and path), and
   **Open in new tab off**.

The first time each user opens Platewright, Bambuddy asks once to allow it. After that, sign-in
happens without a prompt.

## Configuration

| Variable | Default | |
| --- | --- | --- |
| `PLATEWRIGHT_PUBLIC_URL` | required | Where browsers reach Platewright. The callback is `<this>/auth/callback`. |
| `PLATEWRIGHT_BAMBUDDY_URL` | required | Where browsers reach Bambuddy; the only origin allowed to frame Platewright. |
| `PLATEWRIGHT_BAMBUDDY_API_URL` | = `PLATEWRIGHT_BAMBUDDY_URL` | Where the server reaches Bambuddy's API. |
| `PLATEWRIGHT_CLIENT_ID`, `PLATEWRIGHT_CLIENT_SECRET` | | The connected app. |
| `PLATEWRIGHT_BAMBUDDY_KEY` | | API key with Send notifications (badge). |
| `PLATEWRIGHT_SESSION_KEY` | required | Signs the session cookie, at least 32 characters. |
| `PLATEWRIGHT_BADGE_INTERVAL` | `300` | Seconds between badge reports. Bambuddy hides a count older than 10 minutes. |
| `PLATEWRIGHT_BADGE_COUNT` | `1` | Phase 0 stand-in count. |
| `PLATEWRIGHT_COOKIE_SECURE` | `1` | `0` only for plain-http local development. |
| `PLATEWRIGHT_DEV` | | `1`: throwaway session key and localhost URLs. |

## Running

```bash
docker run -d --name platewright -p 3710:8000 --read-only --tmpfs /tmp \
  -v platewright-data:/data --env-file platewright.env ghcr.io/loidolt/platewright:latest
```

## Development

```bash
uv sync
```

```bash
uv run pytest
```

Try it locally against a stand-in Bambuddy. `dev/fake_bambuddy.py` imitates only what Platewright
uses: the framing page, theme and navigate messages, an already-granted sign-in, the token exchange
and the badge.

```bash
uv run uvicorn dev.fake_bambuddy:app --port 3700
```

```bash
PLATEWRIGHT_DEV=1 PLATEWRIGHT_COOKIE_SECURE=0 PLATEWRIGHT_BAMBUDDY_KEY=bb_dev PLATEWRIGHT_CLIENT_ID=bba_dev PLATEWRIGHT_CLIENT_SECRET=bbs_dev uv run uvicorn platewright.web.app:create_app --factory --port 3710
```

Then open <http://localhost:3700>.

GitHub Actions builds the image as `ghcr.io/loidolt/platewright` (`latest` from `main`).
