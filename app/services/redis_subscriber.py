"""
app/services/redis_subscriber.py
──────────────────────────────────
Redis Subscriber — Cross-Worker WebSocket Delivery
═══════════════════════════════════════════════════════════════════════════════

This is the BRIDGE between Redis pub/sub and local WebSocket clients.

One instance runs per worker process. It:
  1. Opens a dedicated Redis subscriber connection
  2. Subscribes to pattern "device:*" (catches ALL devices)
  3. Loops forever receiving messages from Redis
  4. Delivers each message to THIS worker's local WebSocket clients

Why this is critical for multi-worker production:

  Worker 1 (PID 1001) — has WebSocket clients for gen_01
  Worker 2 (PID 1002) — receives MQTT data for gen_01
  Worker 3 (PID 1003) — has WebSocket clients for gen_01
  Worker 4 (PID 1004) — has no clients for gen_01

  Without Redis subscriber:
    Worker 2 saves to DB, broadcasts locally → nobody on Worker 2
    Workers 1 and 3 clients never receive it ❌

  With Redis subscriber (this file):
    Worker 2 saves to DB → publishes to Redis channel "device:gen_01"
    Redis delivers to ALL workers subscribed to "device:*"
    Worker 1 subscriber receives → delivers to its local gen_01 clients ✅
    Worker 2 subscriber receives → delivers to its local gen_01 clients ✅
    Worker 3 subscriber receives → delivers to its local gen_01 clients ✅
    Worker 4 subscriber receives → no local clients → does nothing ✅

Message flow per worker:

  Redis channel "device:gen_01"
          │
          ▼
  redis_subscriber._listen_loop()       ← this file, runs in each worker
          │
          ▼
  ws_manager.broadcast_to_local_clients()  ← send to THIS worker's WS clients
          │
          ▼
  WebSocket clients connected to THIS worker
═══════════════════════════════════════════════════════════════════════════════
"""

import asyncio
import os
from typing import Dict, Any

from app.core.logging import logger
from app.services.redis_manager import redis_manager
from app.services.ws_manager import ws_manager


class RedisSubscriber:
    """
    Background task that bridges Redis pub/sub → local WebSocket clients.
    One instance per worker process.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running: bool = False

    async def start(self) -> None:
        """Start the Redis subscriber background task."""
        self._running = True
        self._task = asyncio.create_task(
            self._listen_loop(),
            name="redis_subscriber",
        )
        worker_pid = os.getpid()
        logger.info(f"[RedisSubscriber] Started on worker PID={worker_pid}")

    async def stop(self) -> None:
        """Cancel the subscriber task gracefully."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[RedisSubscriber] Stopped")

    # ══════════════════════════════════════════════════════════════════════════
    # Main Listen Loop
    # ══════════════════════════════════════════════════════════════════════════

    async def _listen_loop(self) -> None:
        """
        Connect to Redis, subscribe to all device channels,
        and forward each message to local WebSocket clients.

        Auto-reconnects if Redis connection drops.
        """
        worker_pid = os.getpid()

        while self._running:
            subscriber_conn = None
            try:
                # Get a dedicated subscriber connection
                subscriber_conn = await redis_manager.get_subscriber()

                logger.info(
                    f"[RedisSubscriber] PID={worker_pid} "
                    f"listening on pattern device:*"
                )

                # This async generator yields messages forever
                async for message in redis_manager.subscribe_to_all_devices(subscriber_conn):
                    if not self._running:
                        break

                    await self._deliver_to_local_clients(message)

            except asyncio.CancelledError:
                break

            except Exception as exc:
                logger.error(
                    f"[RedisSubscriber] PID={worker_pid} error: {exc}. "
                    f"Reconnecting in 3s..."
                )
                # Wait before reconnecting to avoid tight error loop
                await asyncio.sleep(3)

            finally:
                # Always close the subscriber connection on exit/error
                if subscriber_conn:
                    try:
                        await subscriber_conn.aclose()
                    except Exception:
                        pass

    # ══════════════════════════════════════════════════════════════════════════
    # Message Delivery
    # ══════════════════════════════════════════════════════════════════════════

    async def _deliver_to_local_clients(self, message: Dict[str, Any]) -> None:
        """
        Route a Redis message to the correct local WebSocket broadcast method.

        The message has a "type" field that tells us what kind of data it is:
          "telemetry"    → broadcast_to_local_clients() with telemetry format
          "event_update" → broadcast_to_local_clients() with event format
          "event_state"  → broadcast_to_local_clients() with event state format

        We call broadcast_to_local_clients() directly — NOT the full broadcast_*
        methods — because those would publish back to Redis again, causing an
        infinite loop (Redis → local delivery → Redis → local delivery → ...)
        """
        device_id = message.get("device_id")
        msg_type = message.get("type")

        if not device_id:
            logger.warning(f"[RedisSubscriber] Message missing device_id: {message}")
            return

        # Only deliver if this worker has clients for this device
        # (avoids unnecessary work on workers with no clients)
        local_count = ws_manager.get_connection_count(device_id)
        if local_count == 0:
            return

        logger.debug(
            f"[RedisSubscriber] Delivering {msg_type} for {device_id} "
            f"to {local_count} local client(s)"
        )

        # Send directly to local clients — bypass Redis publish
        await ws_manager.broadcast_to_local_clients(device_id, message)


# ── Singleton ──────────────────────────────────────────────────────────────────
redis_subscriber = RedisSubscriber()
