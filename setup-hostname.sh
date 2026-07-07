#!/usr/bin/env bash
# One-time, per-machine setup for zero-touch friendly hostname.
#
# Run once with sudo:
#     sudo bash setup-hostname.sh
#
# It installs a narrowly-scoped, root-owned helper that can ONLY add the fixed
# line `127.0.0.1 finance-projector` to /etc/hosts, plus a sudoers drop-in that
# lets your user run *only that helper* without a password. After this,
# bootstrap.sh (and therefore every /finproject) enables the friendly URL with
# zero prompts — including on fresh clones on this machine.
#
# Security note: the passwordless grant is limited to the helper, which takes no
# arguments and performs one fixed action. It canNOT be used to write arbitrary
# content to /etc/hosts or run anything else as root.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "This installer must run as root. Re-run:  sudo bash $0" >&2
  exit 1
fi

TARGET_USER="${SUDO_USER:-$(id -un)}"
HELPER="/usr/local/bin/finproject-hosts-setup"
SUDOERS="/etc/sudoers.d/finproject-hosts"

mkdir -p /usr/local/bin

# 1. Root-owned helper — fixed action, no arguments, idempotent.
helper_tmp="$(mktemp)"
cat > "$helper_tmp" <<'EOF'
#!/bin/sh
# Adds the finance-projector loopback alias to /etc/hosts if missing.
# Fixed action only — takes no arguments.
grep -qE '^[[:space:]]*127\.0\.0\.1[[:space:]]+.*\bfinance-projector\b' /etc/hosts && exit 0
printf '%s\n' "127.0.0.1 finance-projector" >> /etc/hosts
EOF
install -m 0755 -o root -g wheel "$helper_tmp" "$HELPER"
rm -f "$helper_tmp"

# 2. Passwordless grant scoped to exactly that helper. Validate before install
#    so a syntax error can never leave a broken sudoers file behind.
sudoers_tmp="$(mktemp)"
printf '%s ALL=(root) NOPASSWD: %s\n' "$TARGET_USER" "$HELPER" > "$sudoers_tmp"
visudo -cf "$sudoers_tmp" >/dev/null
install -m 0440 -o root -g wheel "$sudoers_tmp" "$SUDOERS"
rm -f "$sudoers_tmp"

# 3. Apply immediately so this very run is already zero-touch.
"$HELPER"

echo "Zero-touch hostname setup complete:"
echo "  helper : $HELPER (root-owned, fixed action)"
echo "  sudoers: $SUDOERS (NOPASSWD for user '$TARGET_USER', helper only)"
echo "  /etc/hosts now maps finance-projector -> 127.0.0.1"
