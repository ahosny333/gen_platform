"""
app/core/state.py
──────────────────
Shared In-Memory State Bridge
═══════════════════════════════════════════════════════════════════════════════

This module is the CRITICAL GLUE between two concurrent worlds:

  ┌─────────────────────┐          ┌──────────────────────────┐
  │  MQTT Thread        │          │  FastAPI Async Event Loop │
  │  (sync / paho)      │          │  (async / uvicorn)        │
  │                     │          │                           │
  │  on_message()  ─────┼──────────▶  WebSocket handlers      │
  │  (sync write)       │  shared  │  (async readers)          │
  └─────────────────────┘  state   └──────────────────────────┘

Design decisions:
  - asyncio.Queue per device: thread-safe, non-blocking for async consumers
  - Latest snapshot dict: WebSocket can immediately send last known state
    to a newly connected client without waiting for the next MQTT message
  - WebSocket registry: allows the MQTT on_message to fan-out to ALL
    connected WebSocket clients for a given device
═══════════════════════════════════════════════════════════════════════════════
"""

import asyncio
from typing import Dict, Any, Set
from dataclasses import dataclass, field

from app.core.logging import logger


@dataclass
class DeviceState:
    """
    Per-device shared state container.

    Attributes:
        latest_payload:  The most recent telemetry dict received from MQTT.
                         Used to immediately hydrate a new WebSocket client.
        queue:           asyncio.Queue fed by the MQTT thread.
                         Each WebSocket handler consumes from this queue.
        websocket_clients: Active WebSocket connections subscribed to this device.
    """
    latest_payload: Dict[str, Any] = field(default_factory=dict)
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=100))
    websocket_clients: Set[Any] = field(default_factory=set)


class SharedStateManager:
    """
    Central registry of all device states.
    Single instance created at startup and injected into both
    the MQTT service and the WebSocket route handlers.
    """

    def __init__(self) -> None:
        self._devices: Dict[str, DeviceState] = {}

    # ── Device Registry ────────────────────────────────────────────────────────

    def get_or_create_device(self, device_id: str) -> DeviceState:
        """Return existing DeviceState or create a new one on first contact."""
        if device_id not in self._devices:
            self._devices[device_id] = DeviceState()
            logger.info(f"[State] New device registered in state: {device_id}")
        return self._devices[device_id]

    def get_device(self, device_id: str) -> DeviceState | None:
        """Return DeviceState if device is known, else None."""
        return self._devices.get(device_id)

    def list_device_ids(self) -> list[str]:
        """Return all device IDs currently tracked in memory."""
        return list(self._devices.keys())

    # ── MQTT → State (called from sync MQTT thread) ────────────────────────────

    def update_from_mqtt(self, device_id: str, payload: Dict[str, Any]) -> None:
        """
        Called by the MQTT on_message callback (sync context).

        1. Creates device state if first time seen
        2. Updates the latest snapshot
        3. Puts payload into the asyncio queue for WebSocket consumers

        Thread-safety note:
            asyncio.Queue.put_nowait() is safe to call from a non-async thread
            as long as the event loop is running. We use put_nowait() to avoid
            blocking the MQTT thread. If the queue is full (maxsize=100),
            we drop the oldest item first (sliding window behavior).
        """
        state = self.get_or_create_device(device_id)

        # Update latest snapshot (always — even on Modbus fail status=0)
        state.latest_payload = payload

        # Push to queue — if full, drop oldest to make room (never block MQTT)
        if state.queue.full():
            try:
                state.queue.get_nowait()  # discard oldest
                logger.debug(f"[State] Queue full for {device_id}, dropped oldest item")
            except asyncio.QueueEmpty:
                pass

        try:
            state.queue.put_nowait(payload)
            logger.debug(f"[State] Queued payload for device: {device_id}")
        except asyncio.QueueFull:
            logger.warning(f"[State] Could not queue payload for {device_id}")

    # ── WebSocket Client Registry ──────────────────────────────────────────────

    def register_websocket_client(self, device_id: str, websocket: Any) -> None:
        """Register a new WebSocket connection for a device."""
        state = self.get_or_create_device(device_id)
        state.websocket_clients.add(websocket)
        logger.info(
            f"[State] WebSocket client registered for {device_id} "
            f"(total: {len(state.websocket_clients)})"
        )

    def unregister_websocket_client(self, device_id: str, websocket: Any) -> None:
        """Remove a WebSocket connection when it closes."""
        state = self.get_or_create_device(device_id)
        state.websocket_clients.discard(websocket)
        logger.info(
            f"[State] WebSocket client disconnected from {device_id} "
            f"(remaining: {len(state.websocket_clients)})"
        )

    def get_websocket_clients(self, device_id: str) -> Set[Any]:
        """Return all active WebSocket clients for a device."""
        state = self.get_device(device_id)
        return state.websocket_clients if state else set()

    # ── Debug ──────────────────────────────────────────────────────────────────

    def summary(self) -> Dict[str, Any]:
        """Return a debug summary of current state (used in /health endpoint)."""
        return {
            device_id: {
                "has_data": bool(state.latest_payload),
                "queue_size": state.queue.qsize(),
                "ws_clients": len(state.websocket_clients),
                "last_status": state.latest_payload.get("status"),
            }
            for device_id, state in self._devices.items()
        }


# ── Singleton ──────────────────────────────────────────────────────────────────
# Imported by both mqtt_service.py and websocket routes
shared_state = SharedStateManager()
