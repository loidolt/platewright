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

**Status: phase 3**, written against Bambuddy v1.3.1, whose look it follows (its components, theme
and lucide icons).

- **Today:** the finished prints **to review** first (confirm all good, one by one or all at once, or
  count bad parts per model), then good parts this month against the target and the plates still
  needed, what each printer is doing, demand and recent runs.
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
- **Labels** on a Brother QL label printer on the network (optional): a **spool label** for any spool
  in Bambuddy's inventory, drawn by Bambuddy itself at the size of the loaded roll, and a **bag label**
  on a demand's page for finished parts (the demand, what's in the bag and how many, the model's safety
  labels, a QR code to the Bambuddy project), with a live preview. Both go straight to the printer.
- **Settings:** wording ("part", "toy"), monthly target, printers, palette, operator group, label
  printer.
- **Sync** every 5 minutes: each finished print is credited to a locked plate version by the
  archive's fingerprint, falling back to the `PLATE-<code>_` file name. The **sidebar badge** is the
  prints to review plus demands at risk.

- **Webhook:** a Bambuddy webhook provider pointed at `/hooks/bambuddy` (with the bearer secret
  `PLATEWRIGHT_WEBHOOK_SECRET`) makes the sync run right after a print ends.
- **App messages** through Bambuddy's notification channels that accept them ("Messages from connected
  apps"): a demand that falls at risk (once, until it recovers), and a weekly summary (Monday 08:00 by
  default). Settings has a test button.
- **Home screen:** a web manifest and icons; the icon opens the browser, where the Bambuddy session is.
- **Import** a toys.py folder: `platewright import-csv <folder> [--dry-run]`.

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

## Label printer

Platewright turns each label into Brother raster commands and sends them from the server (the
container, not the browser), so the server must reach the printer: same LAN, or a firewall rule from
the server to the printer's address. By default it sends an **IPP print job on port 631**, and the
printer answers whether it took the job. **Raw, port 9100** is there for printers without IPP; the
printer gives no answer there, so a failed label only shows as no label. Both print on a QL-810W. Give the printer a fixed address. Any Brother QL model
`brother_ql` knows works; set the roll that is loaded (a die-cut roll is detected by the printer, a
continuous one is cut at 90 mm). Settings → Label printer has a test button.

Spool labels come from Bambuddy's label renderer (`POST /inventory/labels/preview`); the API key
reads them with the same permission it reads the spool inventory with.

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
| `PLATEWRIGHT_WEBHOOK_SECRET` | | Bearer secret the Bambuddy webhook provider sends; empty = no webhook. |
| `PLATEWRIGHT_SEED_FILE` | | Settings JSON (wording, target, printers, palette) for a new database; ignored once settings exist. |
| `PLATEWRIGHT_COOKIE_SECURE` | `1` | `0` only for plain-http local development. |
| `PLATEWRIGHT_DEV` | | `1`: throwaway session key and localhost URLs. |

## Settings

Stored in the database. Keys: `unit_name` (e.g. `toy`, `part`), `period_target` (good parts per
month, 0 = none), `max_plate_hours`, `assumed_fail_rate`, `reorder_days`, `operator_group`,
`printers` (`{"X1C": {"model": "Bambu Lab X1 Carbon", "plates_per_day": 1}}`; `model` exactly as
the slicer writes it) and `palette` (`{"White": {"hex": "#FFFFFF", "material": "PLA", "slot": "A1"}}`).
`label_printer_host`, `label_printer_protocol` (`ipp` or `raw`), `label_printer_model` (e.g. `QL-810W`) and `label_size` (the loaded roll as
`brother_ql` names it: `29x90` die-cut, `62` continuous, ...) set up the label printer; no host means
none. `dev/sample_config.json` is an example. Load them with `PLATEWRIGHT_SEED_FILE` or:

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
the badge, and sample print log, archives, spools, printers, File Manager, MakerWorld, projects and
queue (nothing prints). Its sidebar switches the theme (mode, style, background, accent) and who is
signed in (admin, operator, viewer).

1. Seed a dev database (`./data`) with sample settings, models, plates, runs, demand and stock. It
   also writes `data/fake_bambuddy.json` (the plates' fingerprints, so the stand-in's prints match
   them) and sample sliced files in `data/samples/` to try New Plate with:

   ```bash
   PLATEWRIGHT_DEV=1 uv run python -m dev.seed_dev
   ```

2. Start the stand-in:

   ```bash
   uv run uvicorn dev.fake_bambuddy:app --port 3700
   ```

3. Start Platewright:

   ```bash
   PLATEWRIGHT_DEV=1 PLATEWRIGHT_COOKIE_SECURE=0 PLATEWRIGHT_BAMBUDDY_KEY=bb_dev PLATEWRIGHT_CLIENT_ID=bba_dev PLATEWRIGHT_CLIENT_SECRET=bbs_dev uv run uvicorn platewright.web.app:create_app --factory --port 3710
   ```

4. Open <http://localhost:3700>.

GitHub Actions builds the image as `ghcr.io/loidolt/platewright` (`latest` from `main`).
