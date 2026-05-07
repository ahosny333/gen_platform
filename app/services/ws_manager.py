"""
app/services/ws_manager.py
───────────────────────────
WebSocket Connection Manager
═══════════════════════════════════════════════════════════════════════════════

Manages all active WebSocket connections and broadcasts messages to clients.

THIS IS THE ONLY FILE THAT CHANGES WHEN UPGRADING TO REDIS (Option B).
Everything else — routes, data_router, frontend — stays exactly the same.

Option A (now) — In-process broadcast:
  Works perfectly for single Uvicorn worker.
  All connections live in this process's memory.
  Broadcast = loop through local dict and send.

Option B (future Redis upgrade) — Cross-process broadcast:
  Replace _broadcast_local() with Redis pub/sub.
  Subscribe each worker to Redis channels.
  When any worker receives MQTT data → publish to Redis →
  all workers receive it → each broadcasts to its own local clients.
  The public interface (connect/disconnect/broadcast) stays IDENTICAL.

Connection structure:
  _connections = {
      "gen_01": {
          websocket_obj_1,   ← User A watching gen_01
          websocket_obj_2,   ← User B watching gen_01
      },
      "gen_02": {
          websocket_obj_3,   ← User C watching gen_02
      }
  }

Message types sent to frontend:
  {"type": "telemetry", "device_id": "gen_01", "data": {...}}
  {"type": "event_update", "device_id": "gen_01", "data": {...}}
  {"type": "error", "message": "..."}
  {"type": "connected", "device_id": "gen_01", "message": "..."}
═══════════════════════════════════════════════════════════════════════════════
"""

import json
import asyncio
from typing import Any, Dict, Set

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.core.logging import logger


class WebSocketManager:
    """
    Manages WebSocket connections per device.

    Designed for clean upgrade path:
      - All public methods stay the same in Option A and B
      - Only _broadcast_local() gets replaced with Redis in Option B
    """

    def __init__(self) -> None:
        # device_id → set of active WebSocket connections
        self._connections: Dict[str, Set[WebSocket]] = {}
        # Lock per device to prevent concurrent modification during broadcast
        self._locks: Dict[str, asyncio.Lock] = {}

    # ══════════════════════════════════════════════════════════════════════════
    # Connection Lifecycle
    # ══════════════════════════════════════════════════════════════════════════

    async def connect(self, device_id: str, websocket: WebSocket) -> None:
        """
        Accept and register a new WebSocket connection for a device.
        Called when frontend opens ws://server/ws/{device_id}
        """
        await websocket.accept()

        if device_id not in self._connections:
            self._connections[device_id] = set()
            self._locks[device_id] = asyncio.Lock()

        self._connections[device_id].add(websocket)

        count = len(self._connections[device_id])
        logger.info(
            f"[WSManager] Client connected — device={device_id} "
            f"total_clients={count}"
        )

        # Send confirmation to the newly connected client
        await self._send_to_client(websocket, {
            "type": "connected",
            "device_id": device_id,
            "message": f"Subscribed to live data for {device_id}",
            "clients_watching": count,
        })

    async def disconnect(self, device_id: str, websocket: WebSocket) -> None:
        """
        Remove a WebSocket connection when client disconnects.
        Safe to call even if connection was already removed.
        """
        if device_id in self._connections:
            self._connections[device_id].discard(websocket)
            remaining = len(self._connections[device_id])
            logger.info(
                f"[WSManager] Client disconnected — device={device_id} "
                f"remaining_clients={remaining}"
            )
            # Clean up empty sets to free memory
            if remaining == 0:
                del self._connections[device_id]
                del self._locks[device_id]

    # ══════════════════════════════════════════════════════════════════════════
    # Broadcasting
    # ══════════════════════════════════════════════════════════════════════════

    async def broadcast_telemetry(
        self,
        device_id: str,
        payload: Dict[str, Any],
    ) -> None:
        """
        Broadcast a telemetry reading to all clients watching this device.

        Frontend receives:
        {
            "type": "telemetry",
            "device_id": "gen_01",
            "data": {
                "status": 1,
                "rpm": 1784,
                "oil_p": 5068,
                "cool_t": 85.2,
                ... all sensor fields
            }
        }
        """
        message = {
            "type": "telemetry",
            "device_id": device_id,
            "data": payload,
        }
        await self._broadcast_local(device_id, message)

    async def broadcast_event_update(
        self,
        device_id: str,
        event_name: str,
        value: bool,
    ) -> None:
        """
        Broadcast a single event change to all clients watching this device.

        Frontend receives:
        {
            "type": "event_update",
            "device_id": "gen_01",
            "data": {
                "event_name": "high_temp",
                "value": true
            }
        }
        """
        message = {
            "type": "event_update",
            "device_id": device_id,
            "data": {
                "event_name": event_name,
                "value": value,
            },
        }
        await self._broadcast_local(device_id, message)

    async def broadcast_event_state(
        self,
        device_id: str,
        events: Dict[str, bool],
    ) -> None:
        """
        Broadcast a full event state snapshot to all clients watching this device.
        Sent when ESP32 publishes a full state dump.

        Frontend receives:
        {
            "type": "event_state",
            "device_id": "gen_01",
            "data": {
                "low_oil_pressure": false,
                "high_temp": true,
                "fuel_low": false
            }
        }
        """
        message = {
            "type": "event_state",
            "device_id": device_id,
            "data": events,
        }
        await self._broadcast_local(device_id, message)

    # ══════════════════════════════════════════════════════════════════════════
    # Stats & Health
    # ══════════════════════════════════════════════════════════════════════════

    def get_connection_count(self, device_id: str) -> int:
        """Return number of active WebSocket clients for a device."""
        return len(self._connections.get(device_id, set()))

    def get_all_stats(self) -> Dict[str, int]:
        """Return client count per device — used in /health endpoint."""
        return {
            device_id: len(clients)
            for device_id, clients in self._connections.items()
        }

    def total_connections(self) -> int:
        """Total active WebSocket connections across all devices."""
        return sum(len(c) for c in self._connections.values())

    # ══════════════════════════════════════════════════════════════════════════
    # Internal Broadcast — THIS METHOD SWAPS FOR REDIS IN OPTION B
    # ══════════════════════════════════════════════════════════════════════════

    async def _broadcast_local(
        self,
        device_id: str,
        message: Dict[str, Any],
    ) -> None:
        """
        Send message to all local WebSocket clients for a device.

        OPTION A — local in-process broadcast (current implementation)
        OPTION B — replace this with: await redis.publish(f"device:{device_id}", json.dumps(message))
                   Then add a Redis subscriber loop that calls _send_to_local_clients()

        Uses a lock per device to prevent:
          - Concurrent modification of the connections set
          - Race conditions when a client disconnects mid-broadcast
        """
        if device_id not in self._connections:
            return  # No clients watching this device

        if not self._connections[device_id]:
            return  # Empty set

        async with self._locks[device_id]:
            message_str = json.dumps(message, default=str)

            # Copy set to avoid "set changed size during iteration" error
            # if a client disconnects exactly during broadcast
            clients_snapshot = set(self._connections[device_id])

            dead_clients = set()

            for websocket in clients_snapshot:
                success = await self._send_to_client(websocket, message_str, pre_serialized=True)
                if not success:
                    dead_clients.add(websocket)

            # Clean up any dead connections found during broadcast
            for dead in dead_clients:
                self._connections[device_id].discard(dead)
                logger.debug(f"[WSManager] Removed dead connection for {device_id}")

    # ══════════════════════════════════════════════════════════════════════════
    # Send to Single Client
    # ══════════════════════════════════════════════════════════════════════════

    async def _send_to_client(
        self,
        websocket: WebSocket,
        message: Any,
        pre_serialized: bool = False,
    ) -> bool:
        """
        Send a message to one WebSocket client.

        Returns True on success, False if the connection is dead.
        Never raises — a failed send should not crash the broadcaster.
        """
        try:
            # Check connection is still open before attempting send
            if websocket.client_state != WebSocketState.CONNECTED:
                return False

            if pre_serialized:
                await websocket.send_text(message)
            else:
                await websocket.send_text(json.dumps(message, default=str))

            return True

        except WebSocketDisconnect:
            return False
        except RuntimeError:
            # "Cannot call send on a closed connection"
            return False
        except Exception as exc:
            logger.warning(f"[WSManager] Send failed: {exc}")
            return False


# ══════════════════════════════════════════════════════════════════════════════
# Singleton
# ══════════════════════════════════════════════════════════════════════════════
# Imported by websocket route AND data_router
ws_manager = WebSocketManager()
