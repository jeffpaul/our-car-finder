#!/usr/bin/env bash
# One-shot cron job: rebuild and publish the generations guide after the morning scrape,
# then remove its own crontab entry.
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
log="logs/generations-$(date +%F).log"
exec 9>logs/.lock
flock -w 7200 9 || { echo "$(date) gave up waiting for the scrape to finish" >> "$log"; exit 1; }
.venv/bin/python scripts/generations.py --publish >> "$log" 2>&1
status=$?
crontab -l | grep -v 'scripts/generations_once.sh' | crontab -
exit $status
