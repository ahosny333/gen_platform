"""
app/api/routes/websocket.py
────────────────────────────
WebSocket Endpoint
═══════════════════════════════════════════════════════════════════════════════

Endpoint:
  WS /ws/{device_id}?token=<JWT>

Frontend connects like this:
  const ws = new WebSocket("ws://localhost:8000/ws/gen_01?token=eyJhbG...")

Why token in query param (not header)?
  The browser WebSocket API does NOT support custom headers.
  Sending the JWT as a query parameter is the standard solution.
  We validate it exactly the same way as REST endpoints.

Message flow after connection:
  1. Frontend connects → server validates JWT + device access
  2. Server sends {"type": "connected", ...} confirmation
  3. Server immediately sends last known telemetry snapshot
     (so frontend shows data instantly, before next MQTT message)
  4. Every new MQTT message → data_router → ws_manager.broadcast → this client
  5. Frontend disconnects or token expires → connection closes cleanly

Frontend connects with one line:
const ws = new WebSocket(`ws://localhost:8000/ws/gen_01?token=${jwt_token}`)
The endpoint does 5 things in order:
1. Validate JWT from ?token= query param
   (browser WebSocket API doesn't support custom headers)
2. Check device access  (same role rules as REST API)
   root  → any device
   admin/user → only assigned devices
3. Register connection in ws_manager
4. Send immediate snapshot  ← very important for UX
   Frontend shows real data instantly, not blank gauges
5. Keep alive with ping every 30 seconds
   Prevents Nginx from closing idle connections
   Detects dead connections automatically

Heartbeat / keepalive:
  We send a ping every 30 seconds.
  If client doesn't respond → connection is dead → remove it.
  This prevents ghost connections from accumulating.

Messages Frontend Receives
javascript// 1. Connection confirmed (sent immediately on connect)
{"type": "connected", "device_id": "gen_01", "clients_watching": 2}

// 2. Immediate data snapshot (sent right after connect)
{"type": "telemetry", "device_id": "gen_01", "data": {...}, "snapshot": true}

// 3. Live telemetry (every MQTT message, ~5 seconds)
{"type": "telemetry", "device_id": "gen_01", "data": {
    "status": 1, "rpm": 1784, "oil_p": 5068, ...
}}

// 4. Single alarm change
{"type": "event_update", "device_id": "gen_01", "data": {
    "event_name": "high_temp", "value": true
}}

// 5. Full event state dump
{"type": "event_state", "device_id": "gen_01", "data": {
    "high_temp": true, "fuel_low": false, ...
}}

// 6. Server keepalive ping
{"type": "ping"}
// Frontend should respond: {"type": "pong"}
═══════════════════════════════════════════════════════════════════════════════
"""

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from sqlalchemy import select
from starlette.websockets import WebSocketState

from app.core.logging import logger
from app.core.security import decode_access_token
from app.core.state import shared_state
from app.db.database import AsyncSessionLocal
from app.models.device import Device
from app.models.user import User
from app.models.user_device import UserDevice
from app.services.ws_manager import ws_manager

router = APIRouter()

# Ping interval in seconds — keeps connection alive through proxies
PING_INTERVAL = 30


# ══════════════════════════════════════════════════════════════════════════════
# WebSocket Endpoint
# ══════════════════════════════════════════════════════════════════════════════

@router.websocket("/ws/{device_id}")
async def websocket_endpoint(
    device_id: str,
    websocket: WebSocket,
    token: str = Query(..., description="JWT token for authentication"),
):
    """
    WebSocket endpoint for live device telemetry.

    Frontend connects:
        const ws = new WebSocket(`ws://server/ws/gen_01?token=${jwt_token}`)

    Received message types:
        {"type": "connected",     "device_id": "gen_01", ...}
        {"type": "telemetry",     "device_id": "gen_01", "data": {...}}
        {"type": "event_update",  "device_id": "gen_01", "data": {...}}
        {"type": "event_state",   "device_id": "gen_01", "data": {...}}
        {"type": "error",         "message": "..."}
        {"type": "pong"}
    """

    # ── Step 1: Validate JWT token ─────────────────────────────────────────────
    user = await _authenticate_websocket(websocket, token)
    if not user:
        return  # _authenticate_websocket already closed the connection

    # ── Step 2: Verify device access ───────────────────────────────────────────
    has_access = await _check_device_access(device_id, user)
    if not has_access:
        await websocket.accept()
        await websocket.send_text(json.dumps({
            "type": "error",
            "message": f"Access denied to device '{device_id}'",
        }))
        await websocket.close(code=4403)
        logger.warning(
            f"[WS] Access denied — user={user.id} device={device_id}"
        )
        return

    # ── Step 3: Register connection ────────────────────────────────────────────
    await ws_manager.connect(device_id, websocket)
    logger.info(
        f"[WS] Connection established — user={user.id} "
        f"role={user.role} device={device_id}"
    )

    # ── Step 4: Send immediate snapshot (don't make client wait for next MQTT) ─
    await _send_initial_snapshot(websocket, device_id)

    # ── Step 5: Keep connection alive + handle incoming messages ───────────────
    try:
        ping_task = asyncio.create_task(
            _ping_loop(websocket, device_id, user.id)
        )

        try:
            # Listen for messages from client
            # (frontend can send {"type": "ping"} for keepalive)
            while True:
                try:
                    raw = await asyncio.wait_for(
                        websocket.receive_text(),
                        timeout=60.0,  # 60s timeout — ping should arrive every 30s
                    )
                    await _handle_client_message(websocket, device_id, raw)

                except asyncio.TimeoutError:
                    # No message for 60s — check if connection is still alive
                    if websocket.client_state != WebSocketState.CONNECTED:
                        break
                    continue

        except WebSocketDisconnect:
            logger.info(
                f"[WS] Client disconnected — user={user.id} device={device_id}"
            )
        finally:
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass

    finally:
        # Always unregister on exit — no matter how connection ended
        await ws_manager.disconnect(device_id, websocket)
        logger.info(
            f"[WS] Connection cleaned up — user={user.id} device={device_id} "
            f"remaining={ws_manager.get_connection_count(device_id)}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Private Helpers
# ══════════════════════════════════════════════════════════════════════════════

async def _authenticate_websocket(
    websocket: WebSocket,
    token: str,
) -> User | None:
    """
    Validate JWT token before accepting the WebSocket connection.
    Returns the User if valid, None if invalid (connection is closed).
    """
    payload = decode_access_token(token)

    if not payload:
        await websocket.accept()
        await websocket.send_text(json.dumps({
            "type": "error",
            "message": "Invalid or expired token",
        }))
        await websocket.close(code=4401)
        logger.warning("[WS] Rejected connection — invalid token")
        return None

    user_id = payload.get("sub")

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.id == user_id, User.is_active == True)
        )
        user = result.scalar_one_or_none()

    if not user:
        await websocket.accept()
        await websocket.send_text(json.dumps({
            "type": "error",
            "message": "User not found or disabled",
        }))
        await websocket.close(code=4401)
        return None

    return user


async def _check_device_access(device_id: str, user: User) -> bool:
    """
    Check if user has access to the requested device.
    root → always yes
    admin/user → must be in user_devices junction table
    """
    async with AsyncSessionLocal() as db:
        if user.role == "root":
            # Root can access any active device
            result = await db.execute(
                select(Device).where(
                    Device.id == device_id,
                    Device.is_active == True,
                )
            )
            return result.scalar_one_or_none() is not None

        else:
            # admin and user: check junction table
            result = await db.execute(
                select(UserDevice).where(
                    UserDevice.user_id == user.id,
                    UserDevice.device_id == device_id,
                )
            )
            return result.scalar_one_or_none() is not None


async def _send_initial_snapshot(websocket: WebSocket, device_id: str) -> None:
    """
    Send the last known telemetry immediately after connection.
    This means the frontend shows real data instantly instead of
    showing empty gauges until the next MQTT message arrives.
    """
    device_state = shared_state.get_device(device_id)

    if device_state and device_state.latest_payload:
        try:
            await websocket.send_text(json.dumps({
                "type": "telemetry",
                "device_id": device_id,
                "data": device_state.latest_payload,
                "snapshot": True,  # Tells frontend this is cached, not live
            }, default=str))
            logger.debug(f"[WS] Sent initial snapshot for {device_id}")
        except Exception as exc:
            logger.warning(f"[WS] Failed to send initial snapshot: {exc}")

    # Also send last known event state if available
    if device_state and device_state.latest_events:
        try:
            await websocket.send_text(json.dumps({
                "type": "event_state",
                "device_id": device_id,
                "data": device_state.latest_events,
                "snapshot": True,
            }, default=str))
        except Exception:
            pass


async def _handle_client_message(
    websocket: WebSocket,
    device_id: str,
    raw: str,
) -> None:
    """
    Handle messages sent FROM the frontend TO the server.

    Currently handles:
      {"type": "ping"}  → respond with {"type": "pong"}

    Future: could handle subscription changes, filter requests, etc.
    """
    try:
        msg = json.loads(raw)
        msg_type = msg.get("type")

        if msg_type == "ping":
            await websocket.send_text(json.dumps({"type": "pong"}))

        else:
            logger.debug(f"[WS] Unknown message type from client: {msg_type}")

    except json.JSONDecodeError:
        logger.debug(f"[WS] Non-JSON message received: {raw[:100]}")


async def _ping_loop(
    websocket: WebSocket,
    device_id: str,
    user_id: str,
) -> None:
    """
    Send a ping every PING_INTERVAL seconds.

    Purpose:
      - Keeps the TCP connection alive through load balancers and Nginx
        (which close idle connections after ~60 seconds by default)
      - Detects dead connections that didn't send a proper close frame
        (e.g. browser tab closed abruptly, network dropped)

    If send fails → connection is dead → task exits → outer loop detects disconnect.
    """
    try:
        while True:
            await asyncio.sleep(PING_INTERVAL)

            if websocket.client_state != WebSocketState.CONNECTED:
                break

            try:
                await websocket.send_text(json.dumps({"type": "ping"}))
                logger.debug(f"[WS] Ping sent — device={device_id} user={user_id}")
            except Exception:
                break  # Connection is dead

    except asyncio.CancelledError:
        pass  # Normal shutdown
