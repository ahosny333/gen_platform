#!/bin/bash
# ─── Run the Generator Monitoring Platform Backend ─────────────────────────────
#
# Usage:
#   ./run.sh            → Production-like (reload off)
#   ./run.sh --dev      → Development (auto-reload on file change)
#   ./run.sh --help     → Show help
#
# Prerequisites:
#   pip install -r requirements.txt
#   Mosquitto MQTT broker running on localhost:1883

set -e

DEV_MODE=false

for arg in "$@"; do
  case $arg in
    --dev) DEV_MODE=true ;;
  esac
done

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Generator Monitoring Platform — Backend Server"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [ "$DEV_MODE" = true ]; then
  echo "  Mode:   Development (auto-reload enabled)"
  echo "  Docs:   http://localhost:8000/docs"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
else
  echo "  Mode:   Production"
  echo "  Docs:   http://localhost:8000/docs"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  uvicorn app.main:app --host 0.0.0.0 --port 8000
fi
