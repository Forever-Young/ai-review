#!/bin/sh
set -eu

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VENV_DIR="${AI_REVIEW_BOT_VENV:-$HOME/.local/share/ai-review-bot/venv}"
CONFIG_FILE="${AI_REVIEW_CONFIG:-$HOME/.config/ai-review/ai-review.conf}"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_FILE="$LAUNCH_AGENTS_DIR/com.ai-review.telegram-bot.plist"
LOG_DIR="$HOME/Library/Logs/ai-review"
LABEL="com.ai-review.telegram-bot"
LAUNCHCTL_DOMAIN="gui/$(id -u)"
DEFAULT_PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

detect_launchd_path() {
  if command -v zsh >/dev/null 2>&1; then
    shell_path=$(zsh -lc 'printf %s "$PATH"' 2>/dev/null || true)
    if [ -n "$shell_path" ]; then
      printf '%s' "$shell_path"
      return
    fi
  fi
  if command -v /usr/libexec/path_helper >/dev/null 2>&1; then
    helper_path=$(/usr/libexec/path_helper -s 2>/dev/null | sed -n 's/^PATH="\([^"]*\)"; export PATH;$/\1/p')
    if [ -n "$helper_path" ]; then
      printf '%s' "$helper_path"
      return
    fi
  fi
  printf '%s' "${PATH:-$DEFAULT_PATH}"
}

LAUNCHD_PATH=$(detect_launchd_path)

trim_value() {
  printf '%s' "$1" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'
}

telegram_config_value() {
  key="$1"
  if [ ! -f "$CONFIG_FILE" ]; then
    return 0
  fi
  awk -v wanted="$key" '
    BEGIN { in_section = 0 }
    /^[[:space:]]*\[/ {
      in_section = ($0 ~ /^[[:space:]]*\[telegram\][[:space:]]*$/)
      next
    }
    in_section && $0 ~ "^[[:space:]]*" wanted "[[:space:]]*=" {
      line = $0
      sub(/^[^=]*=/, "", line)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", line)
      print line
      exit
    }
  ' "$CONFIG_FILE"
}

mkdir -p "$(dirname "$VENV_DIR")" "$(dirname "$CONFIG_FILE")" "$LAUNCH_AGENTS_DIR" "$LOG_DIR"

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install "$REPO_DIR[bot]"

"$VENV_DIR/bin/ai-review" --config-file "$CONFIG_FILE" >/dev/null 2>&1 || true

BOT_TOKEN_VALUE=$(trim_value "$(telegram_config_value "bot_token")")
AUTHORIZED_USER_ID_VALUE=$(trim_value "$(telegram_config_value "authorized_user_id")")
HAS_BOT_TOKEN="no"
HAS_AUTHORIZED_USER_ID="no"
if [ -n "$BOT_TOKEN_VALUE" ] && [ "$BOT_TOKEN_VALUE" != "PASTE_BOTFATHER_TOKEN_HERE" ]; then
  HAS_BOT_TOKEN="yes"
fi
if [ -n "$AUTHORIZED_USER_ID_VALUE" ]; then
  HAS_AUTHORIZED_USER_ID="yes"
fi

cat > "$PLIST_FILE" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV_DIR/bin/ai-review</string>
    <string>--config-file</string>
    <string>$CONFIG_FILE</string>
    <string>bot</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$LAUNCHD_PATH</string>
  </dict>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/telegram-bot.out.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/telegram-bot.err.log</string>
  <key>WorkingDirectory</key>
  <string>$REPO_DIR</string>
</dict>
</plist>
EOF

SERVICE_ACTION="started"
if launchctl print "$LAUNCHCTL_DOMAIN/$LABEL" >/dev/null 2>&1; then
  SERVICE_ACTION="fully reloaded"
fi
# launchd can briefly keep stale state right after bootout; unload by both
# service label and plist path, then retry bootstrap once if needed.
launchctl bootout "$LAUNCHCTL_DOMAIN/$LABEL" >/dev/null 2>&1 || true
launchctl bootout "$LAUNCHCTL_DOMAIN" "$PLIST_FILE" >/dev/null 2>&1 || true
if ! launchctl bootstrap "$LAUNCHCTL_DOMAIN" "$PLIST_FILE"; then
  sleep 1
  launchctl bootout "$LAUNCHCTL_DOMAIN/$LABEL" >/dev/null 2>&1 || true
  launchctl bootout "$LAUNCHCTL_DOMAIN" "$PLIST_FILE" >/dev/null 2>&1 || true
  launchctl bootstrap "$LAUNCHCTL_DOMAIN" "$PLIST_FILE"
fi
launchctl kickstart -k "$LAUNCHCTL_DOMAIN/$LABEL"

if [ "$HAS_BOT_TOKEN" = "yes" ]; then
  TOKEN_STATUS="already configured"
else
  TOKEN_STATUS="missing"
fi

if [ "$HAS_AUTHORIZED_USER_ID" = "yes" ]; then
  USER_STATUS="already configured ($AUTHORIZED_USER_ID_VALUE)"
else
  USER_STATUS="missing"
fi

cat <<EOF
Installed ai-review Telegram bot support.

Virtualenv:
  $VENV_DIR

Config file:
  $CONFIG_FILE

LaunchAgent:
  $PLIST_FILE

LaunchAgent PATH:
  $LAUNCHD_PATH

Telegram config status:
  bot_token: $TOKEN_STATUS
  authorized_user_id: $USER_STATUS

Service:
  $LABEL $SERVICE_ACTION for $LAUNCHCTL_DOMAIN
  (bootout + bootstrap + kickstart to refresh LaunchAgent environment)

Next steps:
EOF

if [ "$HAS_BOT_TOKEN" != "yes" ]; then
cat <<EOF
  1. In Telegram, open @BotFather, run /newbot, choose a name and username,
     then copy the HTTP API token.
  2. Edit $CONFIG_FILE and set:

     [telegram]
     bot_token = PASTE_BOTFATHER_TOKEN_HERE
     authorized_user_id =
     jira_auth_via_telegram = no

EOF
if [ "$HAS_AUTHORIZED_USER_ID" = "yes" ]; then
cat <<EOF
  3. Update the service after changing config:
EOF
else
cat <<EOF
  3. Send any message to the bot. Because authorized_user_id is empty, it will
     reply with your Telegram user ID.
  4. Edit $CONFIG_FILE again and set authorized_user_id to that number.
  5. Update the service after changing config:
EOF
fi
else
if [ "$HAS_AUTHORIZED_USER_ID" = "yes" ]; then
cat <<EOF
  1. Config is ready. Send a PR URL to your bot in Telegram.
  2. Update the service after changing config:
EOF
else
cat <<EOF
  1. Send any message to the bot. Because authorized_user_id is empty, it will
     reply with your Telegram user ID.
  2. Edit $CONFIG_FILE and set authorized_user_id to that number.
  3. Update the service after changing config:
EOF
fi
fi

cat <<EOF

     launchctl kickstart -k $LAUNCHCTL_DOMAIN/$LABEL

Other useful commands:
  Stop:
     launchctl bootout $LAUNCHCTL_DOMAIN "$PLIST_FILE"

  Logs:
     tail -f "$LOG_DIR/telegram-bot.err.log"
EOF
