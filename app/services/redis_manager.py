"""
app/services/redis_manager.py
──────────────────────────────
Redis Connection Manager
═══════════════════════════════════════════════════════════════════════════════

Manages two Redis connections per worker process:

  publisher  → used to PUBLISH messages to channels
               (called from data_router after saving to DB)

  subscriber → used to SUBSCRIBE and receive messages
               (used by redis_subscriber.py background task)

Why two separate connections?
  Redis protocol rule: once a connection enters SUBSCRIBE mode,
  it can ONLY receive — it cannot publish anymore.
  So we always need two separate connections: one for each direction.

Connection pools:
  We use redis.asyncio (fully async, non-blocking).
  The publisher uses a connection pool (shared, efficient).
  The subscriber uses a dedicated single connection (required for pub/sub).

Channel naming convention:
  device:gen_01  → telemetry + events for gen_01
  device:gen_02  → telemetry + events for gen_02

  All message types share one channel per device to keep things simple.
  The "type" field inside the JSON message distinguishes telemetry vs events.
═══════════════════════════════════════════════════════════════════════════════
"""
import asyncio
import json
from typing import Any, Dict, AsyncIterator

import redis.asyncio as aioredis

from app.core.config import get_settings
from app.core.logging import logger

settings = get_settings()


class RedisManager:
    """
    Manages Redis connections for the platform.
    One instance per worker process — created at startup.
    """

    def __init__(self) -> None:
        self._publisher: aioredis.Redis | None = None
        self._pool: aioredis.ConnectionPool | None = None

    # ══════════════════════════════════════════════════════════════════════════
    # Lifecycle
    # ══════════════════════════════════════════════════════════════════════════

    async def connect(self) -> None:
        """
        Create the Redis connection pool for publishing.
        Called once at application startup in each worker process.
        """
        try:
            self._pool = aioredis.ConnectionPool.from_url(
                settings.redis_url,
                max_connections=20,          # Pool of 20 connections per worker
                decode_responses=True,       # Return strings not bytes
                socket_connect_timeout=5,    # 5s to establish connection
                socket_keepalive=True,       # Keep connections alive
                retry_on_timeout=True,       # Auto-retry on timeout
            )
            self._publisher = aioredis.Redis(connection_pool=self._pool)

            # Test the connection
            await self._publisher.ping()
            logger.info(f"[Redis] Connected to {settings.redis_url}")

        except Exception as exc:
            logger.error(f"[Redis] Connection failed: {exc}")
            raise

    async def disconnect(self) -> None:
        """Close all Redis connections gracefully on shutdown."""
        if self._publisher:
            await self._publisher.aclose()
        if self._pool:
            await self._pool.aclose()
        logger.info("[Redis] Disconnected")

    # ══════════════════════════════════════════════════════════════════════════
    # Publishing
    # ══════════════════════════════════════════════════════════════════════════

    async def publish(self, device_id: str, message: Dict[str, Any]) -> int:
        """
        Publish a message to the Redis channel for a device.

        Called by ws_manager after broadcast_* is triggered.
        Any worker subscribed to this channel will receive it
        and deliver it to its local WebSocket clients.

        Args:
            device_id: The generator ID (e.g. "gen_01")
            message:   The full message dict to broadcast

        Returns:
            Number of subscribers that received the message (0 = no one listening)
        """
        if not self._publisher:
            logger.error("[Redis] Cannot publish — not connected")
            return 0

        channel = settings.get_redis_channel(device_id)
        try:
            payload_str = json.dumps(message, default=str)
            receivers = await self._publisher.publish(channel, payload_str)
            logger.debug(
                f"[Redis] Published to {channel} "
                f"| receivers={receivers} "
                f"| type={message.get('type')}"
            )
            return receivers
        except Exception as exc:
            logger.error(f"[Redis] Publish failed on {channel}: {exc}")
            return 0

    # ══════════════════════════════════════════════════════════════════════════
    # Subscribing
    # ══════════════════════════════════════════════════════════════════════════

    async def get_subscriber(self) -> aioredis.Redis:
        """
        Create a DEDICATED subscriber connection.

        IMPORTANT: This must be a fresh connection — NOT from the pool.
        Once subscribe() is called, this connection can only receive messages.
        """
        subscriber = aioredis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_keepalive=True,
        )
        return subscriber

    async def subscribe_to_all_devices(
        self,
        subscriber: aioredis.Redis,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Subscribe to ALL device channels using pattern subscription.
        Pattern: "device:*" matches device:gen_01, device:gen_02, etc.

        This is a generator — yields one message dict at a time forever.

        Implementation note — why get_message() instead of pubsub.listen():
          pubsub.listen() is an async generator that can silently stall
          on Linux with multi-worker uvicorn because it holds the event loop
          without yielding between iterations.

          get_message(timeout=0) is non-blocking — it checks for a message
          and immediately returns None if none is available. We then
          await asyncio.sleep(0.01) which yields control back to the event
          loop, allowing other coroutines (WebSocket handlers, REST requests)
          to run. This is the correct production pattern for Redis pub/sub
          in asyncio applications.
        """
        pubsub = subscriber.pubsub()
        pattern = f"{settings.redis_channel_prefix}:*"

        await pubsub.psubscribe(pattern)
        logger.info(f"[Redis] Subscribed to pattern: {pattern}")

        try:
            while True:
                # Non-blocking check — returns None immediately if no message
                raw_message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=0.1,  # wait up to 100ms for a message
                )

                if raw_message is None:
                    # No message yet — yield control to event loop briefly
                    await asyncio.sleep(0.01)
                    continue

                # Only process pattern-match messages
                if raw_message.get("type") != "pmessage":
                    await asyncio.sleep(0.01)
                    continue

                try:
                    data = json.loads(raw_message["data"])
                    yield data
                except (json.JSONDecodeError, KeyError) as exc:
                    logger.warning(f"[Redis] Failed to parse message: {exc}")
                    continue

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(f"[Redis] Subscriber error: {exc}")
            raise
        finally:
            try:
                await pubsub.punsubscribe(pattern)
                await pubsub.aclose()
            except Exception:
                pass

    # ══════════════════════════════════════════════════════════════════════════
    # Health
    # ══════════════════════════════════════════════════════════════════════════

    async def ping(self) -> bool:
        """Check if Redis is reachable."""
        try:
            await self._publisher.ping()
            return True
        except Exception:
            return False

    async def get_info(self) -> Dict[str, Any]:
        """Return Redis server info for the health endpoint."""
        try:
            info = await self._publisher.info("server")
            return {
                "connected": True,
                "version": info.get("redis_version"),
                "uptime_seconds": info.get("uptime_in_seconds"),
            }
        except Exception as exc:
            return {"connected": False, "error": str(exc)}


# ── Singleton ──────────────────────────────────────────────────────────────────
redis_manager = RedisManager()
