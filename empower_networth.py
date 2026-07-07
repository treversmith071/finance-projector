#!/usr/bin/env python3
"""
Fetch current Net Worth from Empower Personal Dashboard — no password stored,
no third-party packages (standard library only).

Auth model (confirmed by inspecting the live site): the Personal Dashboard API
at pc-api.empower-retirement.com authenticates with SESSION COOKIES + a CSRF
token. There is no bearer/access token to mint. So this script reuses a session
you already have in the browser: you paste the request's `Cookie` header and the
`csrf` form value into empower_session.json, and it calls getAccounts2 and reads
`networth` from the response.

  Endpoint : POST https://pc-api.empower-retirement.com/api/newaccount/getAccounts2
  Body     : includeOwners=true, apiClient=WEB, lastServerChangeId=-1, csrf=<token>
  Auth     : Cookie header (session)
  Result   : JSON -> spData.networth

Refreshing the session (cookies expire in hours–days):
  1. Open participant.empower-retirement.com (logged in) → DevTools → Network → Fetch/XHR
  2. Click `getAccounts2` → Headers: copy the full `Cookie:` value and your `user-agent`
  3. `Payload` tab → copy the `csrf` value
  4. Paste all three into empower_session.json (see empower_session.example.json)

SECURITY: empower_session.json holds live session credentials (equivalent to
being logged in). It is gitignored — keep it `chmod 600` and never commit it.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.path.join(HERE, "empower_session.json")
OUTPUT_FILE = os.path.join(HERE, "networth.json")
ENDPOINT = "https://pc-api.empower-retirement.com/api/newaccount/getAccounts2"
DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Safari/537.36")


class SessionError(Exception):
    """Raised when the session is invalid/expired or the response is unexpected."""


def load_session(path=SESSION_FILE):
    if not os.path.exists(path):
        raise SessionError(
            "Missing empower_session.json. Copy empower_session.example.json → "
            "empower_session.json and fill in cookie + csrf (see this file's header)."
        )
    with open(path) as f:
        s = json.load(f)
    for k in ("cookie", "csrf"):
        val = s.get(k)
        if not val:
            raise SessionError(f"empower_session.json is missing '{k}'.")
        if val.strip().upper().startswith("PASTE"):
            raise SessionError(
                f"empower_session.json still has the placeholder for '{k}'. "
                f"Paste your real value from DevTools (Cookie + user-agent from "
                f"the getAccounts2 Headers tab, csrf from its Payload tab)."
            )
    return s


def parse_networth(payload: dict) -> float:
    """Extract net worth from a getAccounts2 response.

    Personal Capital wraps results as {spHeader: {...}, spData: {...}}. On an
    expired session the HTTP status can still be 200 but spHeader flags the
    failure, so check that before giving up."""
    header = payload.get("spHeader", {})
    if header.get("success") is False or header.get("errors"):
        raise SessionError(
            "API reported a failure (session likely expired — re-copy Cookie + "
            f"csrf): {json.dumps(header)[:300]}"
        )
    sp = payload.get("spData", payload)
    networth = sp.get("networth")
    if networth is None:
        raise SessionError(
            "Response did not contain 'networth' — the endpoint or session may "
            "have changed."
        )
    return float(networth)


def fetch_networth(session: dict) -> float:
    body = urllib.parse.urlencode({
        "includeOwners": "true",
        "apiClient": "WEB",
        "lastServerChangeId": str(session.get("last_server_change_id", -1)),
        "csrf": session["csrf"],
    }).encode()
    # Note: no Accept-Encoding → server returns uncompressed JSON (stdlib urllib
    # does not auto-decompress gzip).
    req = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
        "Cookie": session["cookie"],
        "User-Agent": session.get("user_agent") or DEFAULT_UA,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://participant.empower-retirement.com",
        "Referer": "https://participant.empower-retirement.com/",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            ctype = resp.headers.get("Content-Type", "")
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read()[:200].decode("utf-8", "replace").replace("\n", " ")
        raise SessionError(
            f"Auth/session error (HTTP {e.code}). Session expired or Cloudflare "
            f"blocked the call — re-copy Cookie + csrf (and user_agent) into "
            f"empower_session.json.\nResponse preview: {detail}"
        )
    except urllib.error.URLError as e:
        raise SessionError(f"Network error reaching Empower: {e.reason}")

    if "application/json" not in ctype:
        raise SessionError(
            f"Expected JSON but got '{ctype or 'unknown'}' — likely a Cloudflare "
            f"challenge or expired session. Re-copy Cookie + csrf + user_agent.\n"
            f"Response preview: {raw[:200]}"
        )
    return parse_networth(json.loads(raw))


def main() -> int:
    try:
        session = load_session()
        networth = fetch_networth(session)
    except SessionError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    out = {
        "networth": round(networth, 2),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Net worth: ${networth:,.0f}")
    print(f"Saved to {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
