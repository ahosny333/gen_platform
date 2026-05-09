#!/bin/bash
# ─── Generator Monitoring Platform — Start Script ─────────────────────────────
#
# Two separate processes must run simultaneously:
#
#   Process 1 — MQTT Worker (handles MQTT + DB writes)
#   Process 2 — API Server  (handles HTTP + WebSocket)
#
# Usage:
#   bash run.sh --mqtt          Start MQTT worker only
#   bash run.sh --api           Start API server only (production, 4 workers)
#   bash run.sh --api --dev     Start API server only (development, 1 worker)
#   bash run.sh --all           Start BOTH in background (development only)
#   bash run.sh --all --dev     Start BOTH in foreground dev mode
#
# Production (recommended — use two separate terminals or systemd):
#   Terminal 1: bash run.sh --mqtt
#   Terminal 2: bash run.sh --api
#
# Quick development (single command, both processes):
#   bash run.sh --all --dev

set -e

MODE=""
DEV_MODE=false
WORKERS=4

while [[ "$#" -gt 0 ]]; do
    case $1 in
        --mqtt)    MODE="mqtt" ;;
        --api)     MODE="api" ;;
        --all)     MODE="all" ;;
        --dev)     DEV_MODE=true ;;
        --workers) WORKERS="$2"; shift ;;
    esac
    shift
done

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Generator Monitoring Platform"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

start_mqtt() {
    echo "  Starting MQTT Worker..."
    echo "  Handles: MQTT subscribe + DB writes + Redis publish"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    python -m app.mqtt_worker
}

start_api_dev() {
    echo "  Starting API Server [DEV — 1 worker, auto-reload]"
    echo "  Handles: REST API + WebSocket + Redis subscribe"
    echo "  Docs: http://localhost:8000/docs"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
}

start_api_prod() {
    echo "  Starting API Server [PROD — $WORKERS workers]"
    echo "  Handles: REST API + WebSocket + Redis subscribe"
    echo "  Docs: http://localhost:8000/docs"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    gunicorn app.main:app \
        --bind 0.0.0.0:8000 \
        --workers $WORKERS \
        --worker-class uvicorn.workers.UvicornWorker \
        --timeout 120 \
        --keep-alive 5 \
        --access-logfile - \
        --error-logfile -
}

case $MODE in
    mqtt)
        start_mqtt
        ;;
    api)
        if [ "$DEV_MODE" = true ]; then
            start_api_dev
        else
            start_api_prod
        fi
        ;;
    all)
        if [ "$DEV_MODE" = true ]; then
            echo "  Mode: Development (both processes)"
            echo "  Press Ctrl+C to stop both"
            echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            # Run MQTT worker in background
            python -m app.mqtt_worker &
            MQTT_PID=$!
            echo "  MQTT Worker started (PID=$MQTT_PID)"
            # Run API server in foreground
            start_api_dev
            # Kill MQTT worker when API server stops
            kill $MQTT_PID 2>/dev/null
        else
            echo "  For production, use two separate terminals:"
            echo "    Terminal 1: bash run.sh --mqtt"
            echo "    Terminal 2: bash run.sh --api"
            echo "  Or use systemd services (see systemd/ directory)"
            exit 1
        fi
        ;;
    *)
        echo "  Usage:"
        echo "    bash run.sh --mqtt              Start MQTT worker"
        echo "    bash run.sh --api               Start API server (production)"
        echo "    bash run.sh --api --dev         Start API server (development)"
        echo "    bash run.sh --all --dev         Start both (development)"
        echo ""
        echo "  Production (recommended):"
        echo "    Terminal 1: bash run.sh --mqtt"
        echo "    Terminal 2: bash run.sh --api"
        exit 1
        ;;
esac
