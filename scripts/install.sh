#!/bin/zsh
# Install kinchat for the current macOS user: venv, app support files, launchd agent.
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$HOME/Library/Application Support/kinchat"
LEGACY_APP="$HOME/Library/Application Support/mia-bridge"
LOGS="$HOME/Library/Logs/kinchat"
LABEL="io.kinchat.bridge"
PY="${PYTHON:-$(command -v python3.13 || command -v python3.12 || echo /opt/homebrew/bin/python3.13)}"

# An install made before the rename keeps its config and state where they are (kinchat looks there too).
if [[ -d "$LEGACY_APP" && ! -e "$APP" ]]; then
  APP="$LEGACY_APP"
fi

mkdir -p "$APP" "$LOGS"

if [[ ! -d "$PROJECT/.venv" ]]; then
  "$PY" -m venv "$PROJECT/.venv"
fi
# [graph] brings msal for Microsoft 365 task mail and calendar, which the default config uses;
# [ics] expands recurring events for calendar.source: ics.
"$PROJECT/.venv/bin/pip" install -q -e "$PROJECT[graph,ics]"

[[ -f "$APP/config.yaml" ]] || cp "$PROJECT/config.example.yaml" "$APP/config.yaml"
[[ -f "$APP/family.md" ]] || cp "$PROJECT/family.example.md" "$APP/family.md"

# Write and load the launchd agent (and retire the pre-rename one, keeping its plist as *.disabled).
"$PROJECT/.venv/bin/python" -m kinchat install-agent
REAL_PY="$("$PROJECT/.venv/bin/python" -c 'import os, sys; print(os.path.realpath(sys.executable))')"
echo "Installed. Logs: $LOGS/bridge.log"
echo "Grant Full Disk Access (System Settings > Privacy & Security) to:"
echo "  $REAL_PY"
echo "Edit $APP/config.yaml and $APP/family.md, then: launchctl kickstart -k gui/$(id -u)/$LABEL"
echo "Dashboard: http://127.0.0.1:8770"
