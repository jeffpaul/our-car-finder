#!/usr/bin/env bash
# Cron entry point. Cron fires hourly; this only proceeds at the [general] run_hours in
# config.toml, in its [general] timezone, so the schedule follows daylight saving.
# Run it by hand with --now to skip the time check.
set -uo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
mkdir -p logs
log="logs/run-$(date +%F).log"
if [[ "${1:-}" == "--now" ]]; then
  shift
else
  # Prints "<timezone> <HH> <HH> ...", e.g. "America/Chicago 07 16".
  schedule=$(.venv/bin/python -m finder.config 2>>"$log") || { echo "$(date) couldn't read the schedule from config.toml" >> "$log"; exit 1; }
  read -r tz run_hours <<< "$schedule"
  hour=$(TZ="$tz" date +%H)
  [[ " $run_hours " == *" $hour "* ]] || exit 0
fi
exec 9>logs/.lock
flock -n 9 || { echo "$(date) another run is still going" >> "$log"; exit 0; }
.venv/bin/python run.py "$@" >> "$log" 2>&1
status=$?
find logs -name 'run-*.log' -mtime +30 -delete
exit $status
