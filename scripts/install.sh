#!/bin/zsh
# Install kinchat for the current macOS user: venv, app support files, launchd agent.
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$HOME/Library/Application Support/kinchat"
LEGACY_APP="$HOME/Library/Application Support/mia-bridge"
LOGS="$HOME/Library/Logs/kinchat"
LABEL="io.kinchat.bridge"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LEGACY_PLIST="$HOME/Library/LaunchAgents/ai.atriel.mia-bridge.plist"
PY="${PYTHON:-$(command -v python3.13 || command -v python3.12 || echo /opt/homebrew/bin/python3.13)}"

# An install made before the rename keeps its config and state where they are (kinchat looks there too).
if [[ -d "$LEGACY_APP" && ! -e "$APP" ]]; then
  APP="$LEGACY_APP"
fi

mkdir -p "$APP" "$LOGS" "$HOME/Library/LaunchAgents"

if [[ ! -d "$PROJECT/.venv" ]]; then
  "$PY" -m venv "$PROJECT/.venv"
fi
"$PROJECT/.venv/bin/pip" install -q -e "$PROJECT"

[[ -f "$APP/config.yaml" ]] || cp "$PROJECT/config.example.yaml" "$APP/config.yaml"
[[ -f "$APP/family.md" ]] || cp "$PROJECT/family.example.md" "$APP/family.md"

sed -e "s|__VENV__|$PROJECT/.venv|g" -e "s|__PROJECT__|$PROJECT|g" -e "s|__HOME__|$HOME|g" \
  "$PROJECT/launchd/$LABEL.plist.template" > "$PLIST"

# Stop and remove the agent from before the rename so two bridges never run at once.
if [[ -f "$LEGACY_PLIST" ]]; then
  launchctl bootout "gui/$(id -u)" "$LEGACY_PLIST" 2>/dev/null || true
  rm -f "$LEGACY_PLIST"
fi

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
REAL_PY="$("$PROJECT/.venv/bin/python" -c 'import os, sys; print(os.path.realpath(sys.executable))')"
echo "Installed. Logs: $LOGS/bridge.log"
echo "Grant Full Disk Access (System Settings > Privacy & Security) to:"
echo "  $REAL_PY"
echo "Edit $APP/config.yaml and $APP/family.md, then: launchctl kickstart -k gui/$(id -u)/$LABEL"
echo "Dashboard: http://127.0.0.1:8770"
