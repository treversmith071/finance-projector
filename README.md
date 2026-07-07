# Finance Projector

Turns a hub-account transaction export into a savings/spending projection and an
interactive HTML dashboard, with an optional **net worth** figure synced from
[Empower Personal Dashboard](https://participant.empower-retirement.com/).

- `project.py` — parses the CSV, computes YTD + projected savings/spending, and
  renders `monthly_savings_2026.html` (cards, charts, per-year tabs).
- Settings gear — override key assumptions and see the dashboard recompute live.
- Empower net-worth sync — a Net worth card with a Cash/Investments/Credit
  component bar, refreshed via a small local bridge server.

> Generated dashboards and all account data are **gitignored** — the code is the
> source of truth. Regenerate the dashboard anytime by re-running the script.

## Download (easiest — no Terminal)

Grab the ready-to-run macOS app from **[Releases](https://github.com/treversmith071/finance-projector/releases/latest)**:

1. Download **`FinProject.zip`** and unzip it.
2. Drag **FinProject.app** into **Applications**.
3. **Right-click the app → Open** the first time (it's unsigned, so macOS asks
   once — if it won't open, go to **System Settings → Privacy & Security → Open Anyway**).
4. On the **Welcome** screen, drop your account export and answer the quick setup.

Requires an **Apple Silicon Mac** (M1/M2/M3…). Python and everything else is
bundled — no clone, no Terminal, no install steps. Google Chrome is only needed
for the optional net-worth sync. All data stays local.

## Quick start (build from source)

```bash
git clone https://github.com/treversmith071/finance-projector.git
cd finance-projector
bash build_app.sh --install     # builds FinProject.app, installs to /Applications
```

Launch **FinProject** from Spotlight (⌘-Space). The first launch does a one-time
setup (Python venv + Playwright/Chromium, ~150 MB) in a Terminal window, then
opens the dashboard; every launch after is instant. On the **Welcome page**, drop
your account export and answer the quick onboarding questions — projections
populate immediately. No personal data comes with the clone; it's all local.

Prefer not to install an app? Run the dashboard directly:

```bash
python3 project.py <your-export.csv> --html monthly_savings_2026.html
```

Requires **Python 3**; **Google Chrome** is only needed for the optional net-worth
sync. See [Install as a Mac app](#install-as-a-mac-app-standalone) for the
standalone/distributable build, and the sections below for everything else.

## Requirements

- Python 3 (standard library only for the core tool and the bridge server).
- For the browser-based net-worth sync: **Google Chrome** installed, plus
  **Playwright** in a local virtualenv (setup below).

## Install as a Mac app (standalone)

`build_app.sh` builds **FinProject.app** — a double-clickable macOS launcher that
starts the local bridge and opens the dashboard, with no terminal typing and no
Claude Code. The app is a thin wrapper around `bootstrap.sh`; all logic stays in
`project.py` / `dashboard_server.py`.

```bash
# From a fresh clone:
cd finance-projector
bash build_app.sh --install     # builds FinProject.app and copies it to /Applications
```

Then launch **FinProject** from Spotlight (⌘-Space), Launchpad, or `/Applications`:

1. **First launch only:** a Terminal window opens and runs the one-time setup
   (venv + Playwright/Chromium, ~150 MB). When it finishes the dashboard opens.
2. On the **Welcome page**, upload your export and complete the quick onboarding.
3. Every launch after that is instant — the app starts/reuses the bridge and
   opens the dashboard straight away.

Notes:

- **No code-signing needed.** Because you build the bundle locally it isn't
  quarantined by Gatekeeper — it just runs.
- **Not a portable file.** The bundle bakes in the absolute path to this repo and
  needs the repo's Python scripts present, so it can't be handed to another Mac
  as-is. Each machine clones and runs `build_app.sh` (see
  [`DISTRIBUTION.md`](DISTRIBUTION.md) for a fully-distributable design).
- **If you move the repo** after building, rerun `bash build_app.sh --install`.
- `FinProject.app` is gitignored (machine-specific); `build_app.sh` regenerates it.

Omit `--install` to build the bundle in the repo without copying it to
`/Applications`.

### Distributable build (no repo/Python needed)

`build_app.sh` above is the **developer fast-path** — the bundle it makes still
needs this repo + venv present. To make a **self-contained** app that runs on
another Mac with no clone, no Python, and no venv, use the frozen build:

```bash
bash build_dist.sh          # -> dist/FinProject.app (embedded Python + Playwright)
bash build_dist.sh --zip    # …plus dist/FinProject.zip for hand-off
```

It's **unsigned**, so a downloaded copy is Gatekeeper-quarantined — the recipient
right-clicks the app → **Open** once. All state goes to
`~/Library/Application Support/FinProject/`. See
[`DISTRIBUTION.md`](DISTRIBUTION.md) for the full design and the (not-yet-built)
signed/notarized Phase 2.

## 1. Generate the dashboard

```bash
python3 project.py <transactions.csv> --html monthly_savings_2026.html
```

Or paste transactions via the `/finproject` slash command, which writes the CSV
and runs this for you.

Useful flags (all optional; sensible defaults):

| Flag | Default | Meaning |
|---|---|---|
| `--as-of YYYY-MM-DD` | today | YTD cutoff |
| `--rent N` | from config | Monthly rent |
| `--biweekly-deposit N` | from config | Spending-account deposit per pay period |
| `--k401-annual N` | from config | Annual 401k contribution |
| `--gross-venmo` / `--net-venmo` | net | Count Venmo OUT as spending, or net it |
| `--net-gambling` / `--gross-gambling` | gross | Net gambling, or count OUT as spending |
| `--td-as-spending` / `--td-as-savings` | savings | Treat TD transfers as spending or savings |
| `--no-seasonal` | off | Disable the prior-year seasonal lift |

Drop `<YYYY>_transactions.csv` files in `prior_years/` to add historical tabs and
enable the seasonal projection (auto-detected — no code change needed).

> **Uploads auto-split by year.** When you load an export through the dashboard
> (the Welcome page or the ⬆ button), the bridge routes each row by its own
> date: current-year rows become the live dataset, and any prior-year rows are
> archived to `prior_years/<YYYY>_transactions.csv` automatically — even when a
> single file spans multiple years. Uploading only prior-year rows keeps your
> current data and just updates the archive.

## 2. Interactive settings (the ⚙ gear)

Click the gear (top-right) to override **monthly rent**, **spending-account
deposits**, **annual 401k**, and whether you **receive bonuses** (+ the bonus
threshold). Changes:

- persist per-year in the browser's `localStorage`,
- **recompute the cards, tables, and charts live** (a faithful in-browser port of
  the Python math), and
- affect only the view — your transaction data is never modified.

## 3. Net worth sync from Empower

The net-worth number and its Cash/Investments/Credit breakdown come from
Empower's Personal Dashboard. A browser page can't launch scripts or read local
files, so a tiny **localhost-only bridge server** connects the dashboard to the
fetcher.

### One-time setup

```bash
python3 -m venv .venv
.venv/bin/pip install playwright
.venv/bin/python -m playwright install chromium
```

### Everyday use

```bash
.venv/bin/python dashboard_server.py     # opens http://localhost:8000/
```

> Tip: `bash bootstrap.sh` does the one-time venv setup *and* starts the bridge
> in one idempotent step (it's what `/finproject` runs automatically). Re-running
> it is a no-op if everything's already up.

Then in the dashboard click **Sync net worth** (or **Refresh** on the card):

1. Your real Google Chrome opens to Empower.
2. First run only: clear the Cloudflare check if shown, then log in (incl. 2FA).
3. The card updates itself with your net worth and the component bar — no reload.

Your **password is never stored or seen by the code** — you type it into the real
Empower page. The session is saved in `empower_profile/` so later syncs are quick
and usually silent.

You can also fetch straight from the terminal:

```bash
.venv/bin/python empower_playwright.py            # reuse saved session
.venv/bin/python empower_playwright.py --fresh    # wipe profile, log in again
.venv/bin/python empower_playwright.py --no-save  # don't persist the session
```

It writes `networth.json`:

```json
{
  "networth": 250000.0,
  "components": { "cash": 50000.0, "investments": 210000.0, "credit_cards": -10000.0 },
  "fetched_at": "2026-01-01T00:00:00+00:00"
}
```

### Friendly URL (optional)

By default the dashboard is served at **http://127.0.0.1:8000/**. If you'd rather
see **http://finance-projector:8000/** in the address bar, `bootstrap.sh` will
enable it automatically once the machine has a loopback alias for the name.

Adding that alias needs root once. Pick one:

```bash
# One-off (simplest): add the line yourself.
echo "127.0.0.1 finance-projector" | sudo tee -a /etc/hosts

# Zero-touch: install a narrowly-scoped, root-owned helper + passwordless grant
# so bootstrap.sh self-adds the alias on this run and every future clone —
# no more prompts ever. The grant covers ONLY a fixed-action helper.
sudo bash setup-hostname.sh
```

Either way, once the alias resolves the friendly URL just works; skip this and
`127.0.0.1:8000` keeps working unchanged.

### When the session expires

Cookies expire after a while. The card shows a stale note after 7 days, and a
sync will re-open the login window automatically. To force a clean login:
`--fresh` (or delete `empower_profile/`).

### Fallback: paste-based fetch (no Playwright)

`empower_networth.py` is a stdlib-only alternative that reuses a session you copy
from your normal browser's DevTools (it sidesteps Cloudflare entirely). Copy
`empower_session.example.json` → `empower_session.json`, paste in the `Cookie`
header, the `csrf` form value, and your `user-agent` from a `getAccounts2`
request, then `chmod 600 empower_session.json` and run `python3
empower_networth.py`. Note: this fallback fetches the **net-worth total only**
(no component bar); the bar is populated by the Playwright fetcher.

## Security / what's gitignored

These hold credentials or personal financial data and are **never committed**:

- `empower_session.json`, `empower_state.json` — session credentials
- `empower_profile/` — saved browser session (a bearer credential; `chmod 600`)
- `networth.json` — your balances
- `monthly_savings_*.html` — generated dashboard (financial data)
- `prior_years/` — historical transaction CSVs
- `.venv/` — the Playwright virtualenv

The bridge server binds to `127.0.0.1` only — nothing is exposed off your
machine. If session values ever leak (e.g. a screenshot), log out of Empower to
rotate them.

## Files

| File | Purpose |
|---|---|
| `project.py` | CSV → projection report + dashboard HTML (source of truth) |
| `build_app.sh` | Build/install the thin **FinProject.app** launcher (dev fast-path) |
| `build_dist.sh` | Freeze a self-contained, distributable **FinProject.app** (PyInstaller) |
| `make_icns.sh` | Shared: build the `.icns` app icon from `dolphin.png` |
| `app_paths.py` | Shared: resolve read-only asset dir vs. per-user writable data dir |
| `bootstrap.sh` | Idempotent setup: venv install + start bridge + friendly-URL self-heal |
| `setup-hostname.sh` | One-time, scoped installer for the zero-touch `finance-projector` alias |
| `dashboard_server.py` | Localhost bridge: serves the dashboard, runs the sync |
| `empower_playwright.py` | Browser-based net-worth fetch (Playwright + real Chrome) |
| `empower_networth.py` | Stdlib paste-session fallback (net-worth total only) |
| `empower_session.example.json` | Template for the paste-based tool |
| `prior_years/` | Historical CSVs for extra tabs + seasonal projection |
