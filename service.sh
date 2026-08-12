#!/bin/bash
# Manage the GoodWe monitor as a macOS login agent.
#
#   ./service.sh install       start at login, and start it now
#   ./service.sh uninstall     stop and remove
#   ./service.sh status        is it running? are alerts muted?
#   ./service.sh logs          follow the log
#   ./service.sh restart       reload after editing .env or the code
#   ./service.sh snooze 2h     mute alerts for a while (no restart needed)
#   ./service.sh unsnooze      cancel a snooze

set -euo pipefail

LABEL="io.github.goodwe-monitor"
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_SRC="$PROJECT/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

usage() { sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

# The snooze is a state file the running agent re-reads each poll, so these
# take effect immediately without a restart.
PY() { "$PROJECT/venv/bin/python" "$PROJECT/monitor.py" "$@"; }

install_agent() {
    [ -f "$PROJECT/.env" ] || { echo "No .env in $PROJECT"; exit 1; }
    mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT/logs"

    # Bake the real project path into the plist; launchd does no expansion.
    sed "s|__PROJECT__|$PROJECT|g" "$PLIST_SRC" > "$PLIST_DST"

    # bootout first so re-installing picks up changes.
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$PLIST_DST"
    launchctl enable "$DOMAIN/$LABEL"

    echo "Installed. Starts automatically at login."
    echo "  logs:   $PROJECT/logs/monitor.log"
    sleep 3
    status_agent
}

uninstall_agent() {
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST_DST"
    echo "Removed. It will no longer start at login."
}

status_agent() {
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
        local pid last
        pid=$(launchctl print "$DOMAIN/$LABEL" | awk '/^\tpid = /{print $3}')
        last=$(launchctl print "$DOMAIN/$LABEL" | awk '/last exit code = /{print $NF}')
        if [ -n "${pid:-}" ]; then
            echo "RUNNING (pid $pid)"
        else
            echo "loaded but not running (last exit code: ${last:-unknown})"
        fi
        PY --status || true
        if [ -f "$PROJECT/logs/monitor.log" ]; then
            echo "--- last 5 log lines ---"
            tail -5 "$PROJECT/logs/monitor.log"
        fi
    else
        echo "NOT INSTALLED"
        return 1
    fi
}

case "${1:-}" in
    install)   install_agent ;;
    uninstall) uninstall_agent ;;
    restart)   launchctl kickstart -k "$DOMAIN/$LABEL" && echo "Restarted." ;;
    status)    status_agent ;;
    logs)      tail -f "$PROJECT/logs/monitor.log" ;;
    snooze)    PY --snooze "${2:?usage: ./service.sh snooze 2h}" ;;
    unsnooze)  PY --unsnooze ;;
    *)         usage; exit 1 ;;
esac
