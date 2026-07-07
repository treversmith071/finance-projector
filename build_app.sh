#!/usr/bin/env bash
# Build FinProject.app — a standalone macOS launcher for the finance dashboard.
#
# The app is a thin wrapper: its executable is a shell script that runs the
# existing bootstrap.sh (start/reuse the local bridge) and opens the dashboard
# in your browser. All real logic stays in dashboard_server.py / project.py, so
# this bundle never duplicates behaviour — it's just a double-clickable front door.
#
# Usage:
#   bash build_app.sh                 # build FinProject.app in the repo
#   bash build_app.sh --install       # …and copy it to /Applications
#
# Re-run this anytime the launcher, icon, or repo path changes.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$HERE/FinProject.app"
CONTENTS="$APP/Contents"
MACOS="$CONTENTS/MacOS"
RES="$CONTENTS/Resources"
ICON_SRC="$HERE/dolphin.png"      # branded logo used for the icon
BG_HEX="0F1117"                    # app dark background (matches the dashboard)

INSTALL=0
[ "${1:-}" = "--install" ] && INSTALL=1

echo "Building $APP …"
rm -rf "$APP"
mkdir -p "$MACOS" "$RES"

# ---------------------------------------------------------------------------
# 1. Icon: build a square .icns from dolphin.png (see make_icns.sh).
# ---------------------------------------------------------------------------
bash "$HERE/make_icns.sh" "$ICON_SRC" "$RES/FinProject.icns" "$BG_HEX"

# ---------------------------------------------------------------------------
# 2. Info.plist
# ---------------------------------------------------------------------------
cat > "$CONTENTS/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>FinProject</string>
  <key>CFBundleDisplayName</key><string>FinProject</string>
  <key>CFBundleIdentifier</key><string>com.treversmith.finproject</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>FinProject</string>
  <key>CFBundleIconFile</key><string>FinProject</string>
  <key>LSMinimumSystemVersion</key><string>10.13</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSUIElement</key><false/>
</dict>
</plist>
PLIST

# ---------------------------------------------------------------------------
# 3. Launcher executable. REPO is baked in at build time (edit + rebuild if the
#    repo ever moves). Starts/reuses the bridge and opens the dashboard.
# ---------------------------------------------------------------------------
LAUNCHER="$MACOS/FinProject"
cat > "$LAUNCHER" <<LAUNCH
#!/bin/bash
set -u
REPO="$HERE"
LAUNCH
cat >> "$LAUNCHER" <<'LAUNCH'
BOOTSTRAP="$REPO/bootstrap.sh"
PY="$REPO/.venv/bin/python"
PROBE="http://127.0.0.1:8000/api/sync-status"
URL="http://127.0.0.1:8000/"
if grep -qE '^[[:space:]]*127\.0\.0\.1[[:space:]]+.*\bfinance-projector\b' /etc/hosts 2>/dev/null; then
  URL="http://finance-projector:8000/"
fi

if [ ! -f "$BOOTSTRAP" ]; then
  osascript -e "display alert \"FinProject\" message \"Couldn't find the finance-projector repo at:
$REPO

The repo may have moved — edit REPO in the app launcher and rebuild with build_app.sh.\" as critical" >/dev/null 2>&1
  exit 1
fi

# Is the bridge already answering on :8000?
WAS_UP=0
curl -s -o /dev/null --max-time 2 "$PROBE" && WAS_UP=1

if [ "$WAS_UP" -eq 1 ]; then
  # Already running — bootstrap is a no-op; the server won't re-open the browser,
  # so we do it here.
  bash "$BOOTSTRAP" >/tmp/finproject-launch.log 2>&1 || true
  open "$URL"
  exit 0
fi

# Not running. First run needs a one-time ~150MB Playwright/Chromium install —
# run bootstrap in a visible Terminal so the download shows progress and errors.
if [ ! -x "$PY" ] || ! "$PY" -c "import playwright" >/dev/null 2>&1; then
  osascript >/dev/null 2>&1 <<OSA
tell application "Terminal"
  activate
  do script "bash '$BOOTSTRAP'; echo; echo '[FinProject] Setup complete — the dashboard opens in your browser. You can close this window.'"
end tell
OSA
  exit 0
fi

# Installed but not running — start the bridge in the background (nohup inside
# bootstrap keeps it alive); the server opens the browser itself on a fresh start.
bash "$BOOTSTRAP" >/tmp/finproject-launch.log 2>&1 || true
exit 0
LAUNCH
chmod +x "$LAUNCHER"

# Refresh Finder's icon cache for the freshly written bundle.
touch "$APP"

echo "Built $APP"

if [ "$INSTALL" -eq 1 ]; then
  DEST="/Applications/FinProject.app"
  echo "Installing to $DEST …"
  rm -rf "$DEST"
  cp -R "$APP" "$DEST"
  touch "$DEST"
  echo "Installed. Launch it from Spotlight, Launchpad, or /Applications."
fi
