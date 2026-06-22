#!/bin/bash
# Append intraday cron entry if not already present.
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CRON_LINE="0 14 * * 1-5 TZ=Europe/Istanbul $PROJECT_DIR/run.sh intraday >> $PROJECT_DIR/logs/cron-intraday.log 2>&1"
MARKER="stock-limit-briefing intraday"

mkdir -p "$PROJECT_DIR/logs"

if crontab -l 2>/dev/null | grep -qF "$MARKER"; then
  echo "Intraday cron already installed."
  crontab -l | grep -F "$MARKER" || true
  exit 0
fi

(
  crontab -l 2>/dev/null || true
  echo "# $MARKER"
  echo "$CRON_LINE"
) | crontab -

echo "Intraday cron installed (14:00 Turkey, Mon–Fri):"
crontab -l | grep -F "$MARKER" -A1
