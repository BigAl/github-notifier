#!/usr/bin/env bash
# github-notifier installer for Linux.
#
#   bash install.sh
#
# Copies the script to ~/.local/share/github-notifier, puts a `github-notifier`
# command on PATH, creates config (prompts once), seeds state, and optionally
# adds the crontab line. Re-run any time to update the code (config is kept).
#
# Env overrides: GHN_PREFIX, GHN_CONFIG_DIR, GHN_STATE_DIR
set -euo pipefail

PREFIX="${GHN_PREFIX:-$HOME/.local}"
INSTALL_DIR="$PREFIX/share/github-notifier"
CONFIG_DIR="${GHN_CONFIG_DIR:-$HOME/.config/gh-notifier}"
STATE_DIR="${GHN_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/gh-notifier}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> github-notifier install"

command -v python3 >/dev/null || { echo "ERROR: python3 required" >&2; exit 1; }
command -v gh      >/dev/null || { echo "ERROR: gh CLI required (https://cli.github.com)" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "ERROR: gh not authenticated - run 'gh auth login' first" >&2; exit 1; }

# 1) code
mkdir -p "$INSTALL_DIR" "$PREFIX/bin" "$CONFIG_DIR" "$STATE_DIR"
install -m 0644 "$SRC_DIR/notifier.py" "$INSTALL_DIR/notifier.py"
cat > "$PREFIX/bin/github-notifier" <<EOF
#!/bin/sh
exec /usr/bin/env python3 "$INSTALL_DIR/notifier.py" "\$@"
EOF
chmod 0755 "$PREFIX/bin/github-notifier"
echo "--> installed $INSTALL_DIR"

# 2) config
CONFIG="$CONFIG_DIR/config.json"
if [ -f "$CONFIG" ]; then
    echo "--> keeping existing $CONFIG"
else
    echo "Provide Slack details (IDs are visible in Slack: profile -> ... -> Copy member ID)."
    read -r -p "Slack DM channel ID to post to (D0...): " CHANNEL
    read -r -p "Your Slack user ID (U0...): " USERID
    read -r -p "Path to the bot token file (xoxb-...): " TOKEN_FILE
    [ -f "${TOKEN_FILE/#\~/$HOME}" ] || echo "WARN: $TOKEN_FILE does not exist yet - create it before the first real use" >&2
    python3 - "$CONFIG" "$CHANNEL" "$USERID" "$TOKEN_FILE" <<'PY'
import json, sys
path, channel, user_id, token_file = sys.argv[1:5]
with open(path, "w") as f:
    json.dump({
        "channel": channel,
        "token_file": token_file,
        "user_id": user_id,
        "reasons": ["review_requested"],
        "max_messages": 8,
    }, f, indent=2)
    f.write("\n")
PY
    chmod 600 "$CONFIG"
    echo "--> wrote $CONFIG"
fi

# 3) first run seeds state silently
"$PREFIX/bin/github-notifier" || true

# 4) cron
CRON_LINE="*/5 * * * * $PREFIX/bin/github-notifier >/dev/null 2>&1"
if crontab -l 2>/dev/null | grep -qF "$PREFIX/bin/github-notifier"; then
    echo "--> crontab already wired"
else
    read -r -n 1 -p "Add to crontab? $CRON_LINE [y/N] " ANSWER || ANSWER=""
    echo
    if [ "${ANSWER:-n}" = "y" ] || [ "${ANSWER:-n}" = "Y" ]; then
        (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
        echo "--> crontab updated"
    else
        echo "--> skipping; add this line yourself:"
        echo "    $CRON_LINE"
    fi
fi

echo
echo "Done. Test with: $PREFIX/bin/github-notifier --force"
echo "Commands in the DM: pending / mark read / pause / resume / status"
