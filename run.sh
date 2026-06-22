#!/bin/bash
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"
LOG_DIR="$PROJECT_DIR/logs"
MAIL_TYPE="${1:-morning}"
PORTFOLIO="${2:-bist}"

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/$(date +%Y-%m-%d)-$MAIL_TYPE-$PORTFOLIO.log"

echo "$(date '+%Y-%m-%d %H:%M:%S') — Starting $MAIL_TYPE briefing ($PORTFOLIO)" >> "$LOG_FILE"
PORTFOLIO="$PORTFOLIO" "$PYTHON" "$PROJECT_DIR/src/main.py" "$MAIL_TYPE" "$PORTFOLIO" >> "$LOG_FILE" 2>&1
echo "$(date '+%Y-%m-%d %H:%M:%S') — Done" >> "$LOG_FILE"
