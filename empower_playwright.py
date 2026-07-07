#!/usr/bin/env python3
"""
Fetch current Net Worth from Empower Personal Dashboard via a real browser
(Playwright) — your password never touches this code.

How it works:
  * We open Empower in your *real* Google Chrome (with automation flags stripped
    so Cloudflare's "verify you are human" check doesn't loop). When the dashboard
    loads it makes its own POST to .../api/newaccount/getAccounts2; we intercept
    that response and read `networth`. No csrf/cookie crafting, no reverse-
    engineered login — the browser handles all auth.
  * A dedicated Chrome profile (empower_profile/) persists your session AND the
    Cloudflare clearance, so after the first login subsequent runs breeze through.

Setup (one time — isolated venv keeps this out of system Python):
    python3 -m venv .venv
    .venv/bin/pip install playwright
    .venv/bin/python -m playwright install chromium   # (bundled fallback only)
Requires Google Chrome installed (it drives your real Chrome via channel="chrome").

Usage (run with the venv's Python):
    .venv/bin/python empower_playwright.py            # opens Chrome; log in first time
    .venv/bin/python empower_playwright.py --fresh    # wipe saved profile, start clean
    .venv/bin/python empower_playwright.py --no-save  # don't persist the profile

NOTE ON CLOUDFLARE: if the "verify you are human" page still loops even in real
Chrome, this automated-browser path is being blocked — use the lean paste tool
(empower_networth.py) instead, which reuses your normal browser's already-verified
session and sidesteps Cloudflare entirely.

SECURITY: empower_profile/ is a saved browser session (cookies) — a bearer
credential equivalent to being logged in. It's gitignored. Use --fresh to reset;
your password is never stored.
"""
import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone

import app_paths

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(app_paths.data_dir(), "empower_profile")
OUTPUT_FILE = os.path.join(app_paths.data_dir(), "networth.json")
# Load the site root, NOT the /dashboard/#/user/home deep link: the hash-routed
# deep link doesn't bootstrap a fresh session (renders a blank/stuck page). The
# root does the normal login/landing redirect and then fires getAccounts2 itself.
START_URL = "https://participant.empower-retirement.com/"
ACCOUNTS_PATH = "/api/newaccount/getAccounts2"


def _extract(payload: dict):
    """Return {'networth': float, 'components': {cash, investments, credit_cards}}
    from a getAccounts2 response, or None if it isn't a successful, authenticated
    payload (so we keep waiting for a real one)."""
    if not isinstance(payload, dict):
        return None
    header = payload.get("spHeader", {})
    if header.get("success") is False or header.get("errors"):
        return None
    sp = payload.get("spData", payload)
    nw = sp.get("networth")
    if nw is None:
        return None

    def num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0.0

    cash = num(sp.get("cashAccountsTotal"))
    invest = num(sp.get("investmentAccountsTotal"))
    credit = num(sp.get("creditCardAccountsTotal"))
    # Fallback: derive from the per-account list if the aggregate totals are
    # absent in the payload.
    if not (cash or invest or credit) and isinstance(sp.get("accounts"), list):
        for a in sp["accounts"]:
            bal = num(a.get("balance"))
            pt = str(a.get("productType") or "").upper()
            if pt == "BANK":
                cash += bal
            elif pt == "INVESTMENT":
                invest += bal
            elif pt == "CREDIT_CARD":
                credit += bal

    return {
        "networth": float(nw),
        "components": {
            "cash": round(cash, 2),                  # shown positive (teal)
            "investments": round(invest, 2),         # shown positive (purple)
            "credit_cards": -round(abs(credit), 2),  # always shown negative (pink)
        },
    }


def _launch(pw, headless: bool):
    """Launch the user's real Google Chrome with automation signals disabled so
    Cloudflare Turnstile doesn't loop. Falls back to bundled Chromium if Chrome
    isn't installed (bundled build is more likely to be challenged)."""
    kwargs = dict(
        user_data_dir=PROFILE_DIR,
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )
    try:
        return pw.chromium.launch_persistent_context(channel="chrome", **kwargs)
    except Exception:
        return pw.chromium.launch_persistent_context(**kwargs)


def get_networth(fresh: bool, save: bool) -> float:
    from playwright.sync_api import sync_playwright

    if fresh and os.path.isdir(PROFILE_DIR):
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)
    have_profile = os.path.isdir(PROFILE_DIR) and bool(os.listdir(PROFILE_DIR))

    with sync_playwright() as pw:
        context = _launch(pw, headless=False)
        holder = {}
        try:
            page = context.pages[0] if context.pages else context.new_page()

            def on_response(resp):
                if holder.get("data") is not None:
                    return
                if ACCOUNTS_PATH in resp.url and resp.status == 200:
                    try:
                        data = _extract(resp.json())
                    except Exception:
                        return
                    if data is not None:
                        holder["data"] = data

            page.on("response", on_response)
            page.goto(START_URL, wait_until="domcontentloaded")

            if have_profile:
                print("→ Using saved Chrome profile; grabbing net worth… "
                      "(log in again only if prompted)")
            else:
                print("→ In the Chrome window: clear the Cloudflare check if shown, "
                      "then log in (incl. 2FA). I'll capture your net worth "
                      "automatically once the dashboard loads…")

            timeout_s = 90 if have_profile else 300
            deadline = time.monotonic() + timeout_s
            while holder.get("data") is None and time.monotonic() < deadline:
                try:
                    page.wait_for_timeout(500)
                except Exception:
                    break  # window closed
        finally:
            try:
                context.close()
            except Exception:
                pass

    if not save and os.path.isdir(PROFILE_DIR):
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)

    if holder.get("data") is None:
        raise RuntimeError(
            "Timed out with no net-worth response. If the Cloudflare 'verify you "
            "are human' page kept looping, this automated-browser path is blocked "
            "— use the lean paste tool (empower_networth.py) instead."
        )
    return holder["data"]


def fetch_and_save(fresh: bool = False, save: bool = True) -> int:
    """Run the browser fetch and write networth.json. Callable in-process (the
    bridge server re-invokes the app with --run-fetch, which lands here) as well
    as from the CLI. Returns a process-style exit code."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("error: Playwright isn't available — run this with the project venv:\n"
              "  .venv/bin/python empower_playwright.py\n"
              "(set up once via: python3 -m venv .venv && .venv/bin/pip install "
              "playwright && .venv/bin/python -m playwright install chromium)",
              file=sys.stderr)
        return 1

    try:
        data = get_networth(fresh=fresh, save=save)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    nw = data["networth"]
    comp = data["components"]
    out = {
        "networth": round(nw, 2),
        "components": comp,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Net worth: ${nw:,.0f}  (cash ${comp['cash']:,.0f} · "
          f"investments ${comp['investments']:,.0f} · credit ${comp['credit_cards']:,.0f})")
    print(f"Saved to {OUTPUT_FILE}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch Empower net worth via browser.")
    ap.add_argument("--fresh", action="store_true",
                    help="Wipe the saved Chrome profile and start a clean login.")
    ap.add_argument("--no-save", dest="save", action="store_false",
                    help="Do not persist the browser profile to disk.")
    args = ap.parse_args()
    return fetch_and_save(fresh=args.fresh, save=args.save)


if __name__ == "__main__":
    sys.exit(main())
