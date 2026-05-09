"""
app/mqtt_worker.py
───────────────────
Standalone MQTT Worker Process
═══════════════════════════════════════════════════════════════════════════════

This is the HEART of the professional architecture.
Run as a SEPARATE process from the API workers.

Responsibilities (and ONLY this process does these):
  1. Connect to MQTT broker — ONE connection, ONE client ID
  2. Receive ALL messages from ESP32 devices
  3. Save to PostgreSQL database  — ONE writer, zero duplicates
  4. Publish to Redis pub/sub     — signals API workers to push via WebSocket

What this process does NOT do:
  - Serve HTTP requests
  - Handle WebSocket connections
  - Serve the REST API

How to run:
  python -m app.mqtt_worker

Production (via systemd):
  See systemd/mqtt-worker.service

Architecture diagram:

  ESP32 devices
      │ MQTT
      ▼
  Mosquitto Broker
      │
      ▼
  ┌─────────────────────────────────────────────────┐
  │  mqtt_worker.py  (THIS FILE — 1 process only)   │
  │                                                  │
  │  MQTTService                                     │
  │    on_message() ─→ asyncio.Queue                 │
  │                         │                        │
  │  DataRouter (async)     │                        │
  │    ← reads queue        │                        │
  │    → saves to DB ───────┘                        │
  │    → publishes to Redis ────────────────────────►│
  └─────────────────────────────────────────────────┘
      │ Redis pub/sub
      ▼
  API Workers (4x) — receive from Redis, push to WebSocket clients

Key guarantee: ONE process = ONE MQTT connection = ZERO duplicate DB writes
═══════════════════════════════════════════════════════════════════════════════
"""

import asyncio
import os
import signal
import sys

# Add project root to path when running as: python -m app.mqtt_worker
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings
from app.core.logging import logger
from app.core.state import shared_state
from app.db.init_db import init_db
from app.services.mqtt_service import mqtt_service
from app.services.data_router import data_router
from app.services.redis_manager import redis_manager

settings = get_settings()


async def main() -> None:
    """
    Main entry point for the MQTT worker process.

    Startup sequence:
      1. Initialize database tables (safe — idempotent)
      2. Connect to Redis (for publishing after DB saves)
      3. Start MQTT service (connect broker, start background thread)
      4. Start data router (async workers: queue → DB → Redis publish)
      5. Wait forever (until SIGTERM or SIGINT)

    Shutdown sequence (on SIGTERM/SIGINT):
      1. Stop data router (finish in-progress saves)
      2. Stop MQTT service (disconnect cleanly)
      3. Disconnect Redis
    """

    pid = os.getpid()
    logger.info("=" * 60)
    logger.info(f"  {settings.app_name} — MQTT Worker")
    logger.info(f"  Version: {settings.app_version}")
    logger.info(f"  PID: {pid}")
    logger.info("=" * 60)

    # ── 1. Database ─────────────────────────────────────────────────────────
    # Creates tables if they don't exist, seeds default data on first run.
    # Safe to run even if API workers already ran this — idempotent.
    logger.info("[MQTTWorker] Initializing database...")
    await init_db()

    # ── 2. Redis ─────────────────────────────────────────────────────────────
    # Connect the publisher pool — used by data_router after each DB save
    # to notify API workers of new data via Redis pub/sub.
    logger.info("[MQTTWorker] Connecting to Redis...")
    await redis_manager.connect()

    # ── 3. MQTT Service ──────────────────────────────────────────────────────
    # Single client ID — no PID suffix needed here because only ONE process
    # runs this worker. Clean, simple, professional.
    logger.info("[MQTTWorker] Starting MQTT service...")
    mqtt_service.start()

    # ── 4. Data Router ───────────────────────────────────────────────────────
    # Starts background async tasks that:
    #   - Read from shared_state queues (fed by MQTT on_message)
    #   - Save to PostgreSQL
    #   - Publish to Redis (signals API workers)
    logger.info("[MQTTWorker] Starting data router...")
    await data_router.start()

    logger.info("[MQTTWorker] ✅ All services started. Waiting for MQTT data...")
    logger.info(f"[MQTTWorker] MQTT broker: {settings.mqtt_broker_host}:{settings.mqtt_broker_port}")
    logger.info(f"[MQTTWorker] Redis:       {settings.redis_url}")
    logger.info("[MQTTWorker] Press Ctrl+C to stop")

    # ── 5. Wait forever ──────────────────────────────────────────────────────
    # Use asyncio.Event for clean shutdown on SIGTERM/SIGINT
    # Signal handling differs between platforms:
    #   Linux/Mac: loop.add_signal_handler() works (async-native)
    #   Windows:   must use signal.signal() (sync) — asyncio proactor
    #              does NOT support add_signal_handler()
    stop_event = asyncio.Event()

    # def _handle_signal(sig: signal.Signals) -> None:
    #     logger.info(f"[MQTTWorker] Received {sig.name} — initiating shutdown...")
    #     stop_event.set()

    # # Register signal handlers for graceful shutdown
    # loop = asyncio.get_running_loop()
    # for sig in (signal.SIGTERM, signal.SIGINT):
    #     loop.add_signal_handler(sig, _handle_signal, sig)

################### windows ############################
    def _handle_signal(sig_num, frame) -> None:
        try:
            sig_name = signal.Signals(sig_num).name
        except Exception:
            sig_name = str(sig_num)
        logger.info(f"[MQTTWorker] Received {sig_name} — initiating shutdown...")
        # Schedule from sync callback into async event loop safely
        try:
            asyncio.get_event_loop().call_soon_threadsafe(stop_event.set)
        except RuntimeError:
            stop_event.set()
 
    # signal.signal() works on BOTH Windows and Linux
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)
####################################################################

    # Block here until a shutdown signal arrives
    await stop_event.wait()

    # ── Shutdown ─────────────────────────────────────────────────────────────
    logger.info("[MQTTWorker] Shutting down gracefully...")

    # Stop data router first — let it finish any in-progress DB writes
    await data_router.stop()
    logger.info("[MQTTWorker] Data router stopped")

    # Stop MQTT — no new data after this
    mqtt_service.stop()
    logger.info("[MQTTWorker] MQTT service stopped")

    # Disconnect Redis
    await redis_manager.disconnect()
    logger.info("[MQTTWorker] Redis disconnected")

    logger.info(f"[MQTTWorker] PID={pid} shutdown complete ✅")


if __name__ == "__main__":
    asyncio.run(main())
