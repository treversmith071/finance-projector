#!/usr/bin/env bash
# Build a self-contained, frozen FinProject.app with PyInstaller.
#
# This is Phase 1 of DISTRIBUTION.md: the app embeds Python + Playwright and all
# assets, so the target Mac needs NO repo, NO venv, and NO Python installed. It
# is UNSIGNED — a downloaded copy is Gatekeeper-quarantined, so a first-time user
# right-clicks the app → Open (once) to launch it. (See DISTRIBUTION.md Phase 2
# for signing + notarization.)
#
# Chrome is still the browser used for the optional Empower net-worth sync
# (channel="chrome"), so no ~150 MB Chromium is bundled.
#
# Usage:
#   bash build_dist.sh            # -> dist/FinProject.app
#   bash build_dist.sh --zip      # …and dist/FinProject.zip for hand-off
#
# The result differs from build_app.sh: that one builds a thin launcher that
# needs this repo + venv present (the developer fast-path); this one is the
# standalone, distributable bundle.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
PY="$HERE/.venv/bin/python"
ICNS="$HERE/build/FinProject.icns"

# 0. Prerequisites: the venv (from bootstrap.sh) plus PyInstaller.
if [ ! -x "$PY" ]; then
  echo "No .venv found — run 'bash bootstrap.sh' first to create it." >&2
  exit 1
fi
if ! "$PY" -c "import PyInstaller" >/dev/null 2>&1; then
  echo "Installing PyInstaller into the venv…"
  "$PY" -m pip -q install pyinstaller
fi
# pywebview gives the app its native window (WKWebView) instead of a browser tab.
if ! "$PY" -c "import webview" >/dev/null 2>&1; then
  echo "Installing pywebview into the venv…"
  "$PY" -m pip -q install pywebview
fi

# 1. App icon (shared generator).
mkdir -p "$HERE/build"
bash "$HERE/make_icns.sh" "$HERE/dolphin.png" "$ICNS"

# 2. Freeze. dashboard_server.py is the entry point; it imports project,
#    empower_playwright, and app_paths, which PyInstaller follows automatically.
#    Read-only assets are added at the bundle root so app_paths.resource_dir()
#    (sys._MEIPASS) finds them; --collect-all playwright bundles its Node driver.
rm -rf "$HERE/dist/FinProject.app" "$HERE/build/FinProject" "$HERE/FinProject.spec"
"$PY" -m PyInstaller \
  --name FinProject \
  --windowed \
  --noconfirm \
  --clean \
  --icon "$ICNS" \
  --osx-bundle-identifier com.treversmith.finproject \
  --collect-all playwright \
  --collect-all webview \
  --add-data "market_data.json:." \
  --add-data "dolphin.png:." \
  --add-data "favicon.png:." \
  --add-data "favicon.ico:." \
  dashboard_server.py

APP="$HERE/dist/FinProject.app"
[ -d "$APP" ] || { echo "Build failed: $APP not produced." >&2; exit 2; }
echo "Built $APP"

# 3. Optional zip for hand-off (ditto preserves the bundle + resource forks).
if [ "${1:-}" = "--zip" ]; then
  ( cd "$HERE/dist" && rm -f FinProject.zip &&
    ditto -c -k --sequesterRsrc --keepParent FinProject.app FinProject.zip )
  echo "Zipped $HERE/dist/FinProject.zip"
fi
