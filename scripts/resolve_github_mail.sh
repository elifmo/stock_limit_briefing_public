#!/bin/bash
# Resolve which briefing to send for GitHub Actions (manual or scheduled).
# Writes GITHUB_OUTPUT: mail_type (empty = skip this run).
set -euo pipefail

if [ "${GITHUB_EVENT_NAME:-}" = "workflow_dispatch" ]; then
  echo "mail_type=${MAIL_TYPE_INPUT:-morning}" >> "${GITHUB_OUTPUT}"
  exit 0
fi

SCHEDULE="${GITHUB_SCHEDULE:-}"
SUMMER=$(python3 -c "from datetime import datetime; from zoneinfo import ZoneInfo; print(int(bool(datetime.now(ZoneInfo('Europe/Amsterdam')).dst())))")

mail_type=""

case "$SCHEDULE" in
  "0 8 * * 1-5")
    [ "$SUMMER" = "1" ] && mail_type="morning"
    ;;
  "0 9 * * 1-5")
    [ "$SUMMER" = "0" ] && mail_type="morning"
    ;;
  "0 11 * * 1-5")
    mail_type="intraday"
    ;;
  "45 15 * * 1-5")
    [ "$SUMMER" = "1" ] && mail_type="evening"
    ;;
  "45 16 * * 1-5")
    [ "$SUMMER" = "0" ] && mail_type="evening"
    ;;
esac

if [ -n "$mail_type" ]; then
  echo "mail_type=$mail_type" >> "${GITHUB_OUTPUT}"
  echo "Scheduled mail: $mail_type (cron: $SCHEDULE, summer=$SUMMER)"
else
  echo "Skipping — cron $SCHEDULE not active in current season (summer=$SUMMER)"
fi
