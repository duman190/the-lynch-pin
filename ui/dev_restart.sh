#!/usr/bin/env bash
# Dev helper: (re)start the portal in the background on loopback. Usage: ui/dev_restart.sh [extra args]
cd "$(dirname "$0")/.." || exit 1
PIDFILE=/tmp/lynch_portal.pid
if [ -f "$PIDFILE" ]; then kill "$(cat "$PIDFILE")" 2>/dev/null; sleep 1; fi
nohup python3 -m ui.server --port "${PORT:-8765}" "$@" > /tmp/lynch_portal.log 2>&1 &
echo $! > "$PIDFILE"
sleep 2
curl -s "http://127.0.0.1:${PORT:-8765}/api/health"; echo
