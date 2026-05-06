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

Updated to support 3 message types:

  telemetry queue  → live readings from generator/{device_id}/data
  event_state queue → full event dumps from generator/{device_id}/event/state
  event_update queue → single changes from generator/{device_id}/event/update

Each has its own asyncio.Queue so they never mix or block each other.
The data_router reads from all three queues and routes to the right service.

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
    Per-device shared state container - holds all queues and latest snapshots.

    Attributes:
        latest_payload:  The most recent telemetry dict received from MQTT.
                         Used to immediately hydrate a new WebSocket client.
        queue:           asyncio.Queue fed by the MQTT thread.
                         Each WebSocket handler consumes from this queue.
                         Three separate queues for three message types:
                            queue              → telemetry readings (sensor data)
                            event_state_queue  → full event state dumps (all events at once)
                            event_update_queue → single event changes (one event changed)
        websocket_clients: Active WebSocket connections subscribed to this device.
    """
    # ── Telemetry (sensor readings) ────────────────────────────────────────────
    latest_payload: Dict[str, Any] = field(default_factory=dict)
    queue: asyncio.Queue = field(
        default_factory=lambda: asyncio.Queue(maxsize=100)
    )

    # ── Events (alarms and digital states) ────────────────────────────────────
    latest_events: Dict[str, Any] = field(default_factory=dict)
    event_state_queue: asyncio.Queue = field(
        default_factory=lambda: asyncio.Queue(maxsize=50)
    )
    event_update_queue: asyncio.Queue = field(
        default_factory=lambda: asyncio.Queue(maxsize=50)
    )
    # ── WebSocket clients ──────────────────────────────────────────────────────
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
        state.latest_payload = payload
        self._safe_put(state.queue, payload, device_id, "telemetry")

    # ── Event state update (from generator/{id}/event/state) ──────────────────

    def update_event_state(self, device_id: str, payload: Dict[str, Any]) -> None:
        """Push full event state dump into device's event_state queue."""
        state = self.get_or_create_device(device_id)
        state.latest_events = payload
        self._safe_put(state.event_state_queue, payload, device_id, "event_state")

    # ── Single event update (from generator/{id}/event/update) ────────────────

    def update_single_event(self, device_id: str, payload: Dict[str, Any]) -> None:
        """Push single event change into device's event_update queue."""
        state = self.get_or_create_device(device_id)
        self._safe_put(state.event_update_queue, payload, device_id, "event_update")

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
                "telemetry_queue": state.queue.qsize(),
                "event_state_queue": state.event_state_queue.qsize(),
                "event_update_queue": state.event_update_queue.qsize(),
                "ws_clients": len(state.websocket_clients),
                "last_status": state.latest_payload.get("status"),
                "active_events": {
                    k: v for k, v in state.latest_events.items() if v is True
                },
            }
            for device_id, state in self._devices.items()
        }

    # ── Private helper ─────────────────────────────────────────────────────────

    def _safe_put(
        self,
        queue: asyncio.Queue,
        payload: Dict[str, Any],
        device_id: str,
        queue_type: str,
    ) -> None:
        """Put payload in queue, dropping oldest if full. Never blocks."""
        if queue.full():
            try:
                queue.get_nowait()
                logger.debug(f"[State] {queue_type} queue full for {device_id}, dropped oldest")
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning(f"[State] Could not queue {queue_type} for {device_id}")



# ── Singleton ──────────────────────────────────────────────────────────────────
# Imported by both mqtt_service.py and websocket routes
shared_state = SharedStateManager()
