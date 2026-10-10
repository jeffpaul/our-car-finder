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
if [[ $status -ne 0 ]]; then
  .venv/bin/python -c 'import sys; sys.path.insert(0, "."); import run; from finder.notify import Notifier
Notifier(run.load_config()).ops("Generations guide rebuild failed", open(sys.argv[1]).read()[-1000:])' "$log"
fi
crontab -l | grep -v 'scripts/generations_once.sh' | crontab -
exit $status
