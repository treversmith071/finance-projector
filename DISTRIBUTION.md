# FinProject — fully-distributable Mac app (scope)

This document scopes turning FinProject into a **real, downloadable macOS app**:
drag-to-Applications, no repo clone, no Python prerequisite, no terminal, no
first-run ~150 MB download.

## Status

- ✅ **Phase 1 (self-contained, unsigned) — DONE.** `build_dist.sh` produces a
  frozen `dist/FinProject.app` (embedded Python + Playwright, ~150 MB) that
  needs no repo/venv/Python on the target Mac. Verified: the app boots, serves
  the dashboard, rebuilds on `/api/ingest` in-process, writes all state to
  `~/Library/Application Support/FinProject/` (never the bundle), and the
  Playwright net-worth fetch launches Chrome from inside the bundle.
  - Build: `bash build_dist.sh` (add `--zip` for a hand-off archive).
  - Chrome remains the browser for net-worth sync (`channel="chrome"`), so no
    Chromium is bundled.
  - Unsigned: a downloaded copy is Gatekeeper-quarantined — recipient
    right-clicks the app → **Open** once. (Phase 2 removes this.)
- ⬜ **Phase 2 (signed + notarized DMG)** — not started; needs an Apple Developer
  Program membership.
- ✅ **Native window — DONE.** The frozen app now shows the dashboard in its own
  window (pywebview / macOS WKWebView) instead of a browser tab. Run from source
  it still uses the browser; `FINPROJECT_WINDOW=1/0` forces either. No Chromium
  bundled for this (WKWebView is the OS's own engine).
- ⬜ **Phase 3 remainder (auto-update / bundled Chromium for sync)** — not started.

The rest of this document is the original design/estimate; the "Work breakdown"
below describes what Phase 1 implemented (sections A–C) and what remains (D–F).

## Where we are today

`build_app.sh` produces `FinProject.app`, but it's a **thin launcher**, not a
self-contained app. It works great on the machine that built it, but it can't be
handed to another Mac because:

1. **No runtime is bundled.** Everything is Python (`project.py`,
   `dashboard_server.py`, `empower_playwright.py`) run through a repo-local
   `.venv`. The target Mac needs Python 3 + the repo present.
2. **The bundle bakes in an absolute repo path** (`REPO="…/finance-projector"`)
   and calls the repo's scripts — move or remove the repo and it breaks.
3. **Writable state lives next to the code** (`transactions.csv`, `networth.json`,
   `finance_config.json`, `monthly_savings_2026.html`, `empower_profile/`). An app
   in `/Applications` is read-only and can't write into its own bundle.
4. **It's unsigned.** Fine for a locally-built app (not quarantined), but a
   *downloaded* unsigned app is blocked by Gatekeeper.

## Target architecture

Freeze the Python into the bundle and move all writable state to the user's
Library. Ship as a signed, notarized `.dmg`.

```
FinProject.app/
  Contents/
    MacOS/FinProject            ← frozen binary (PyInstaller/py2app), embeds Python
    Resources/
      market_data.json          ← read-only bundled data
      dolphin.png, favicon.*     ← read-only assets
      prior_years/               ← default seasonal history (read-only)
    Info.plist, FinProject.icns

~/Library/Application Support/FinProject/   ← ALL writable state
  transactions.csv  networth.json  finance_config.json
  monthly_savings_2026.html  empower_profile/
```

## Work breakdown

### A. Collapse the subprocess boundaries (core refactor)
`dashboard_server.py` currently shells out with a separate interpreter:
- `_rebuild_dashboard()` → `subprocess.run([venv_py, project.py, …])`
- `_run_fetch()` → `subprocess.run([venv_py, empower_playwright.py])`

In a frozen bundle there is no standalone `python` to run those `.py` files.
Two options:
- **In-process** (preferred for `project.py`): import its render entrypoint and
  call it directly instead of spawning.
- **Self-reinvoke** (needed for the Playwright fetch, which wants process
  isolation and its own event loop): dispatch on a subcommand and relaunch the
  frozen binary itself, e.g. `subprocess.run([sys.executable, "--run-fetch"])`,
  with `main()` routing `--run-fetch`/`--rebuild` to the right function.

Effort: ~0.5–1 day. This is the main code change and is prerequisite to freezing.

### B. Relocate writable state
Introduce a `data_dir()` helper (`~/Library/Application Support/FinProject/`,
created on first run) and route every writable path through it in
`dashboard_server.py` and `project.py`. Bundled read-only assets resolve from the
app's `Resources/` (via `sys._MEIPASS` under PyInstaller). Seed `prior_years/`
from Resources on first run so users can still extend it.
Effort: ~0.5 day. Mechanical but touches many path constants; test the data-purge
logic (`purge_session_data`) against the new location.

### C. Freeze with PyInstaller
Build a one-bundle app embedding CPython + `dashboard_server` + `project` +
`empower_playwright` + the Playwright Python package and its Node driver.
- **Good news:** the net-worth fetch uses `channel="chrome"` (drives the user's
  installed Chrome), so **we do NOT bundle Chromium (~150 MB).** We only ship the
  Playwright driver. Chrome stays an optional prerequisite for net-worth sync;
  the projection/dashboard work without it.
- Watch-outs: Playwright's driver needs a PyInstaller hook + `PLAYWRIGHT_BROWSERS_PATH`
  handling; `webbrowser`/`open` still used to show the dashboard in the default
  browser (unchanged).
- **Python version:** the Phase 1 build was made with **Python 3.14** (PyInstaller
  6.21) and works. 3.14 is very new, though — if a future build hits odd freeze
  behavior (missing hidden imports, driver not found, crashes on launch), pin the
  build venv to **Python 3.12** (the most battle-tested combo with PyInstaller +
  Playwright) and rebuild: `python3.12 -m venv .venv && bash bootstrap.sh &&
  bash build_dist.sh`. Only the *build* interpreter matters — the frozen app
  embeds whatever it was built with, so this is invisible to end users.
Effort: ~1 day (Playwright + PyInstaller is the flakiest part; budget buffer).

### D. Code-sign + notarize
- Requires an **Apple Developer Program** membership ($99/yr) and a *Developer ID
  Application* certificate.
- Sign with **hardened runtime**; the embedded interpreter likely needs
  entitlements `com.apple.security.cs.allow-unsigned-executable-memory` and
  `disable-library-validation`.
- Notarize with `notarytool`, then `stapler staple` the app and the DMG.
Effort: ~0.5 day first time (mostly one-time cert/CI setup), minutes thereafter.

### E. Package as a DMG
Drag-to-Applications `.dmg` (e.g. `create-dmg`), signed + notarized.
Effort: ~0.25 day.

### F. (Optional) Auto-update
Sparkle, or a lightweight "check GitHub Releases" prompt. Out of scope for v1.

## Suggested phasing

| Phase | Outcome | Needs paid account? | Effort |
|---|---|---|---|
| **1 — Self-contained (unsigned)** | A/B/C: PyInstaller app, embedded Python, data in Library. Distribute as a zip; users right-click→Open once. | No | ~2–2.5 days |
| **2 — Signed & notarized DMG** | D/E: real drag-to-Applications download, no Gatekeeper friction. | Yes ($99/yr) | ~0.75 day |
| **3 — Polish** | F auto-update; optionally bundle Chromium to drop the Chrome prereq (+~150 MB). | — | as desired |

## Open decisions

1. **Apple Developer Program?** Phase 2 is impossible without it. If we stay
   unsigned (Phase 1 only), recipients must right-click→Open to bypass Gatekeeper.
2. **Chrome prerequisite for net-worth sync** — keep it (small bundle, `channel="chrome"`)
   or bundle Chromium for a zero-dependency sync (+~150 MB, Phase 3)?
3. **Distribution channel** — GitHub Releases DMG, or private hand-off? Affects
   whether notarization is worth it.
4. **Keep `build_app.sh`?** Yes — the thin-launcher stays the fast path for the
   developer machine; the frozen build is a separate `build_dist.sh` target.

## What does NOT need to change

- `project.py`'s computation and `render_html()` — reused as-is (called in-process).
- The dashboard HTML/JS, onboarding, net-worth card, data-hygiene purge behavior.
- `bootstrap.sh` and the `/finproject` slash command remain the dev-machine flow.
