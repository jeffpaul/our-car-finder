#!/usr/bin/env bash
# Cron entry point. Cron fires hourly (the box is on UTC); this only proceeds at
# the Central-time hours below, so the schedule follows daylight saving.
# Run it by hand with --now to skip the time check.
set -uo pipefail
cd "$(dirname "$0")"
RUN_HOURS="07 16"
if [[ "${1:-}" == "--now" ]]; then
  shift
else
  hour=$(TZ=America/Chicago date +%H)
  [[ " $RUN_HOURS " == *" $hour "* ]] || exit 0
fi
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
mkdir -p logs
log="logs/run-$(date +%F).log"
exec 9>logs/.lock
flock -n 9 || { echo "$(date) another run is still going" >> "$log"; exit 0; }
.venv/bin/python run.py "$@" >> "$log" 2>&1
status=$?
find logs -name 'run-*.log' -mtime +30 -delete
exit $status
