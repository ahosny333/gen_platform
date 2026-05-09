#!/bin/bash
# ─── Run the Generator Monitoring Platform Backend ─────────────────────────────
#
# Usage:
#   ./run.sh            → Production-like (reload off)
#   ./run.sh --dev      → Development (auto-reload on file change)
#   ./run.sh --help     → Show help
#   bash run.sh --workers 8  → Custom worker count
#
# Prerequisites:
#   pip install -r requirements.txt
#   Redis running on localhost:6379
#   Mosquitto MQTT broker running on localhost:1883

set -e

DEV_MODE=false
WORKERS=4


# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --dev)     DEV_MODE=true ;;
        --workers) WORKERS="$2"; shift ;;
    esac
    shift
done

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Generator Monitoring Platform"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [ "$DEV_MODE" = true ]; then
    echo "  Mode:     Development (1 worker, auto-reload)"
    echo "  Docs:     http://localhost:8000/docs"
    echo "  Note:     Redis not required in dev mode (1 worker)"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    uvicorn app.main:app \
        --host 0.0.0.0 \
        --port 8000 \
        --reload
else
    echo "  Mode:     Production ($WORKERS workers)"
    echo "  Docs:     http://localhost:8000/docs"
    echo "  Redis:    Required — make sure Redis is running"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    # Use gunicorn with UvicornWorker for production
    # This is the recommended production setup for FastAPI
    gunicorn app.main:app \
        --bind 0.0.0.0:8000 \
        --workers $WORKERS \
        --worker-class uvicorn.workers.UvicornWorker \
        --timeout 120 \
        --keep-alive 5 \
        --access-logfile - \
        --error-logfile -
fi
