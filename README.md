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

**Status: phase 2**, written against Bambuddy v1.3.1.

- **Today:** good parts this month against the target and the pace needed, what each printer is
  doing, the finished prints **to review** (confirm all good, or count bad parts per model), demand,
  recent runs and the last sync.
- **Demand:** orders, events and restock targets with coverage (stock on hand goes to the earliest
  first), plates needed against plate slots, and **Queue N plates**: one Bambuddy project per demand,
  queue items that wait for a manual start and ask for the good / reject verdict.
- **Plates:** upload a sliced `.gcode.3mf`, see the checks as a checklist (printer, palette, models
  linked and test-printed, license, hours), link unknown slicer names to models on the spot, and
  **lock** it as the next version. The file is kept by Platewright and uploaded to Bambuddy's
  production folder; a superseded or retired plate's file moves out of it. Runs Bambuddy didn't see
  can be logged by hand.
- **Models:** license and distribution terms, safety labels, slicer names, the source file in
  Bambuddy's library with its thumbnail, and test print pass / fail.
- **Stock:** made, adjusted, out and on hand per model; **Out** (one model, or a mixed box split by
  what's on hand, optionally for a demand) and **Adjust** with a reason.
- **Filament:** palette colors with grams on hand from Bambuddy's inventory, use per day, days left.
- **Settings:** wording ("part", "toy"), monthly target, printers, palette, operator group.
- **Sync** every 5 minutes: each finished print is credited to a locked plate version by the
  archive's fingerprint, falling back to the `PLATE-<code>_` file name. The **sidebar badge** is the
  prints to review plus demands at risk.

**Roles**, from Bambuddy: admins do everything; members of the operator group review prints and
record parts going out; everyone else reads. The sidebar-app basics from phase 0 stay: sign-in with
Bambuddy inside its frame (OAuth code flow with PKCE), the `bambuddy:theme` / `bambuddy:navigate`
messages with Bambuddy's origin only, and `Content-Security-Policy: frame-ancestors <Bambuddy's origin>`.

## Hosting

Put Platewright on a sibling host of Bambuddy, for example `plates.example.com` next to
`print.example.com`. Browsers then treat its cookie as first-party inside Bambuddy's frame. On an
unrelated domain the cookie is third-party, and Safari and Firefox drop it.

Server-side calls (the sign-in token exchange, the sync and the badge) can go to Bambuddy over the LAN
(`PLATEWRIGHT_BAMBUDDY_API_URL`).

## Setup in Bambuddy (as admin)

1. **Connected app:** Settings → API Keys → Connected Apps. Name `Platewright`, callback URL
   `<PLATEWRIGHT_PUBLIC_URL>/auth/callback`. Copy the client ID and the secret; the secret is shown
   only once.
2. **API key:** Settings → API Keys → Create Key with **Read Status**, **Send notifications**,
   **Manage Queue**, **Manage Library** and **Manage Projects**, nothing else. Create it as an admin:
   Send notifications needs an owner who may update notifications.
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
| `PLATEWRIGHT_BAMBUDDY_KEY` | | API key with Read Status (sync, pages) and Send notifications (badge). |
| `PLATEWRIGHT_SESSION_KEY` | required | Signs the session cookie, at least 32 characters. |
| `PLATEWRIGHT_SYNC_INTERVAL` | `300` | Seconds between syncs; the badge is reported after each. Bambuddy hides a badge older than 10 minutes. |
| `PLATEWRIGHT_DATA_DIR` | `/data` | Holds `platewright.db` (SQLite) and `plates/`, the locked files. Back up both. |
| `PLATEWRIGHT_SEED_FILE` | | Settings JSON (wording, target, printers, palette) for a new database; ignored once settings exist. |
| `PLATEWRIGHT_COOKIE_SECURE` | `1` | `0` only for plain-http local development. |
| `PLATEWRIGHT_DEV` | | `1`: throwaway session key and localhost URLs. |

## Settings

Stored in the database. Keys: `unit_name` (e.g. `toy`, `part`), `period_target` (good parts per
month, 0 = none), `max_plate_hours`, `assumed_fail_rate`, `reorder_days`, `operator_group`,
`printers` (`{"X1C": {"model": "Bambu Lab X1 Carbon", "plates_per_day": 1}}`; `model` exactly as
the slicer writes it) and `palette` (`{"White": {"hex": "#FFFFFF", "material": "PLA", "slot": "A1"}}`).
`dev/sample_config.json` is an example. Load them with `PLATEWRIGHT_SEED_FILE` or:

```bash
docker exec platewright platewright import-config /data/settings.json
```

`import-config` also reads a toys.py `config.json`. Other commands: `platewright sync`, and
`platewright check-plate <file.gcode.3mf>` to run the plate checks without locking anything.

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
uses: the framing page, theme and navigate messages, an already-granted sign-in, the token exchange,
the badge, and sample print log, archives, spools and printer status.

1. Seed a dev database (`./data`) with sample settings, two models and a plate. It prints the
   plate's fingerprint:

   ```bash
   PLATEWRIGHT_DEV=1 uv run python -m dev.seed_dev
   ```

2. Start the stand-in with that fingerprint, so its sample prints match the plate:

   ```bash
   FAKE_PLATE_SHA=<fingerprint> uv run uvicorn dev.fake_bambuddy:app --port 3700
   ```

3. Start Platewright:

   ```bash
   PLATEWRIGHT_DEV=1 PLATEWRIGHT_COOKIE_SECURE=0 PLATEWRIGHT_BAMBUDDY_KEY=bb_dev PLATEWRIGHT_CLIENT_ID=bba_dev PLATEWRIGHT_CLIENT_SECRET=bbs_dev uv run uvicorn platewright.web.app:create_app --factory --port 3710
   ```

4. Open <http://localhost:3700>.

GitHub Actions builds the image as `ghcr.io/loidolt/platewright` (`latest` from `main`).
