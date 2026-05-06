#!/bin/sh
set -eu

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VENV_DIR="${AI_REVIEW_BOT_VENV:-$HOME/.local/share/ai-review-bot/venv}"
CONFIG_FILE="${AI_REVIEW_CONFIG:-$HOME/.config/ai-review/ai-review.conf}"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_FILE="$LAUNCH_AGENTS_DIR/com.ai-review.telegram-bot.plist"
LOG_DIR="$HOME/Library/Logs/ai-review"
LABEL="com.ai-review.telegram-bot"

mkdir -p "$(dirname "$VENV_DIR")" "$(dirname "$CONFIG_FILE")" "$LAUNCH_AGENTS_DIR" "$LOG_DIR"

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install "$REPO_DIR[bot]"

"$VENV_DIR/bin/ai-review" --config-file "$CONFIG_FILE" >/dev/null 2>&1 || true

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
  <key>StandardOutPath</key>
  <string>$LOG_DIR/telegram-bot.out.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/telegram-bot.err.log</string>
  <key>WorkingDirectory</key>
  <string>$REPO_DIR</string>
</dict>
</plist>
EOF

cat <<EOF
Installed ai-review Telegram bot support.

Virtualenv:
  $VENV_DIR

Config file:
  $CONFIG_FILE

LaunchAgent:
  $PLIST_FILE

Next steps:
  1. In Telegram, open @BotFather, run /newbot, choose a name and username,
     then copy the HTTP API token.
  2. Edit $CONFIG_FILE and set:

     [telegram]
     bot_token = PASTE_BOTFATHER_TOKEN_HERE
     authorized_user_id =
     jira_auth_via_telegram = no

  3. Start the service once:

     launchctl bootstrap gui/$(id -u) "$PLIST_FILE"
     launchctl kickstart -k gui/$(id -u)/$LABEL

  4. Send any message to the bot. Because authorized_user_id is empty, it will
     reply with your Telegram user ID.
  5. Edit $CONFIG_FILE again and set authorized_user_id to that number.
  6. Restart the service after updating config:

     launchctl kickstart -k gui/$(id -u)/$LABEL

Other useful commands:
  Stop:
     launchctl bootout gui/$(id -u) "$PLIST_FILE"

  Logs:
     tail -f "$LOG_DIR/telegram-bot.err.log"
EOF
