#!/usr/bin/env bash
# Generate a macOS .icns from a source PNG, scaled onto a square themed tile.
#
#   make_icns.sh <src_png> <out_icns> [bg_hex]
#
# Low-res sources look soft at large sizes — drop in a bigger PNG to sharpen.
set -euo pipefail

SRC="${1:?usage: make_icns.sh <src_png> <out_icns> [bg_hex]}"
OUT="${2:?usage: make_icns.sh <src_png> <out_icns> [bg_hex]}"
BG="${3:-0F1117}"     # app dark background

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
ICONSET="$WORK/icon.iconset"
MASTER="$WORK/master.png"
mkdir -p "$ICONSET"

cp "$SRC" "$MASTER"
sips -Z 820 "$MASTER" >/dev/null                          # longest side -> 820 (keep aspect)
sips -p 1024 1024 --padColor "$BG" "$MASTER" >/dev/null   # pad to a 1024 square tile

gen() { sips -z "$1" "$1" "$MASTER" --out "$ICONSET/$2" >/dev/null; }
gen 16   icon_16x16.png
gen 32   icon_16x16@2x.png
gen 32   icon_32x32.png
gen 64   icon_32x32@2x.png
gen 128  icon_128x128.png
gen 256  icon_128x128@2x.png
gen 256  icon_256x256.png
gen 512  icon_256x256@2x.png
gen 512  icon_512x512.png
gen 1024 icon_512x512@2x.png
iconutil -c icns "$ICONSET" -o "$OUT"
