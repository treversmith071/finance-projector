#!/usr/bin/env bash
# Idempotent setup for the finance dashboard.
#
#   - Creates the Playwright venv + installs Chromium the FIRST time only.
#   - Starts the local bridge server if it isn't already running.
#
# Self-locating: works from any clone path, so no hardcoded directories.
# Safe to run on every /finproject — after the first run it's a no-op that
# just confirms the bridge is up.
#
# Flags:
#   --no-install   Skip the venv/Playwright/Chromium install step (used when the
#                  install hasn't been consented to yet). Still starts the bridge
#                  if the venv already exists.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

DO_INSTALL=1
[ "${1:-}" = "--no-install" ] && DO_INSTALL=0

PY="$HERE/.venv/bin/python"

# 0. Ensure the friendly hostname resolves (best-effort — writing /etc/hosts
#    needs root). If passwordless/cached sudo is available we add it silently and
#    permanently; otherwise we fall back to 127.0.0.1 and print the one-liner.
HOSTS_ALIAS="finance-projector"
HOSTS_LINE="127.0.0.1 ${HOSTS_ALIAS}"
HOSTS_HELPER="/usr/local/bin/finproject-hosts-setup"
if ! grep -qE "^[[:space:]]*127\.0\.0\.1[[:space:]]+.*\b${HOSTS_ALIAS}\b" /etc/hosts 2>/dev/null; then
  if [ -x "$HOSTS_HELPER" ] && sudo -n "$HOSTS_HELPER" 2>/dev/null; then
    # Zero-touch path: scoped passwordless helper (see setup-hostname.sh).
    echo "BOOTSTRAP: added '$HOSTS_LINE' to /etc/hosts (friendly URL enabled)" >&2
  elif sudo -n true 2>/dev/null && printf '%s\n' "$HOSTS_LINE" | sudo -n tee -a /etc/hosts >/dev/null 2>&1; then
    # Fallback: cached sudo credentials from a recent sudo in this terminal.
    echo "BOOTSTRAP: added '$HOSTS_LINE' to /etc/hosts (friendly URL enabled)" >&2
  else
    echo "BOOTSTRAP: hostname alias not set; URL will use 127.0.0.1." >&2
    echo "BOOTSTRAP:   one-time zero-touch setup:  sudo bash \"$HERE/setup-hostname.sh\"" >&2
    echo "BOOTSTRAP:   or add it just once:        echo \"$HOSTS_LINE\" | sudo tee -a /etc/hosts" >&2
  fi
fi

# 1. One-time venv + Playwright + Chromium (reaches the internet ~150MB).
if [ ! -x "$PY" ] || ! "$PY" -c "import playwright" >/dev/null 2>&1; then
  if [ "$DO_INSTALL" -eq 0 ]; then
    echo "BOOTSTRAP: install needed (venv/Playwright missing) — skipped (--no-install)." >&2
    exit 3
  fi
  echo "BOOTSTRAP: creating venv + installing Playwright/Chromium (one-time)…" >&2
  [ -x "$HERE/.venv/bin/python" ] || python3 -m venv .venv
  "$PY" -m pip -q install --upgrade pip
  "$PY" -m pip -q install playwright
  "$PY" -m playwright install chromium
  echo "BOOTSTRAP: install complete." >&2
fi

# Friendly URL for humans (falls back to the loopback IP if the hosts alias
# isn't configured). Probes below always use 127.0.0.1 so they work regardless.
if grep -qE '^[[:space:]]*127\.0\.0\.1[[:space:]]+.*\bfinance-projector\b' /etc/hosts 2>/dev/null; then
  URL="http://finance-projector:8000/"
else
  URL="http://127.0.0.1:8000/"
fi

# 2. Start the bridge server unless it's already answering on :8000.
if curl -s -o /dev/null --max-time 2 http://127.0.0.1:8000/api/sync-status; then
  echo "BOOTSTRAP: bridge already running at $URL" >&2
else
  echo "BOOTSTRAP: starting bridge server…" >&2
  nohup "$PY" dashboard_server.py >/tmp/finproject-bridge.log 2>&1 &
  # Give it a moment to bind the port before we report success.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl -s -o /dev/null --max-time 1 http://127.0.0.1:8000/api/sync-status; then
      break
    fi
    sleep 0.3
  done
  if curl -s -o /dev/null --max-time 2 http://127.0.0.1:8000/api/sync-status; then
    echo "BOOTSTRAP: bridge up at $URL" >&2
  else
    echo "BOOTSTRAP: bridge did not come up — see /tmp/finproject-bridge.log" >&2
    exit 4
  fi
fi
