"""
app/api/routes/devices.py
──────────────────────────
Device & History API Routes
═══════════════════════════════════════════════════════════════════════════════

Endpoints in this file:
  GET  /api/devices                          → list all devices for current user
  GET  /api/devices/{device_id}              → single device details
  GET  /api/devices/{device_id}/history      → historical telemetry data
  GET  /api/devices/{device_id}/status       → current live status only

All endpoints require a valid JWT token (require_auth dependency).
═══════════════════════════════════════════════════════════════════════════════
"""

import json
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_auth
from app.core.logging import logger
from app.db.database import get_db
from app.models.device import Device
from app.models.user import User
from app.schemas.devices import (
    DeviceResponse,
    DeviceListResponse,
    TelemetryHistoryResponse,
    TelemetryResponse,
)
from app.services.telemetry_service import telemetry_service

router = APIRouter()

# ── Helper: Offline threshold ──────────────────────────────────────────────────
# A device is considered "offline" if no message received for this long
OFFLINE_THRESHOLD_MINUTES = 5


def _derive_status(device: Device) -> str:
    """
    Compute a human-readable status string from the device's last known state.

    Logic (matches your Backend API spec Section 7):
      1. No data ever received           → "offline"
      2. Last seen > OFFLINE_THRESHOLD   → "offline"
      3. last_reading status == 0        → "comm_error" (Modbus fail)
      4. last_reading status == 2        → "alarm"
      5. last_reading status == 1        → "running"
      6. Anything else                   → "offline"
    """
    # No data ever received
    if not device.last_seen_at or not device.last_reading:
        return "offline"

    # Check if device has gone silent
    now = datetime.now(timezone.utc)
    last_seen = device.last_seen_at

    # Make last_seen timezone-aware if it isn't (SQLite quirk)
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)

    if (now - last_seen) > timedelta(minutes=OFFLINE_THRESHOLD_MINUTES):
        return "offline"

    # Parse last reading
    try:
        reading = json.loads(device.last_reading)
    except (json.JSONDecodeError, TypeError):
        return "offline"

    raw_status = reading.get("status")

    if raw_status == 0:
        return "comm_error"   # Modbus communication failure
    elif raw_status == 2:
        return "alarm"
    elif raw_status == 1:
        return "running"
    else:
        return "offline"


def _build_device_response(device: Device) -> DeviceResponse:
    """
    Convert a SQLAlchemy Device object into a DeviceResponse schema.
    Parses the JSON last_reading string back into a dict for the response.
    """
    last_reading_dict = None
    if device.last_reading:
        try:
            last_reading_dict = json.loads(device.last_reading)
        except (json.JSONDecodeError, TypeError):
            pass

    return DeviceResponse(
        device_id=device.id,
        name=device.name,
        description=device.description,
        location=device.location,
        status=_derive_status(device),
        last_seen_at=device.last_seen_at,
        last_reading=last_reading_dict,
    )


# ══════════════════════════════════════════════════════════════════════════════
# GET /api/devices  — List all devices
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/",
    response_model=DeviceListResponse,
    summary="Get all devices",
    description=(
        "Returns all generators the current user has access to. "
        "Admins see all devices. External users see only assigned devices."
    ),
)
async def get_devices(
    current_user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """
    Role-based device listing:
      - Admin role   → returns ALL active devices
      - User role    → returns only devices assigned to this user
    """
    logger.info(
        f"[Devices] GET /devices — user={current_user.id} "
        f"role={current_user.role}"
    )

    # Build query based on role
    if current_user.role == "admin":
        # Admin sees everything
        query = select(Device).where(Device.is_active == True)
    else:
        # External user sees only their assigned devices
        query = select(Device).where(
            Device.is_active == True,
            Device.owner_user_id == current_user.id,
        )

    result = await db.execute(query)
    devices = result.scalars().all()

    device_responses = [_build_device_response(d) for d in devices]

    return DeviceListResponse(
        total=len(device_responses),
        devices=device_responses,
    )


# ══════════════════════════════════════════════════════════════════════════════
# GET /api/devices/{device_id}  — Single device details
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/{device_id}",
    response_model=DeviceResponse,
    summary="Get single device",
)
async def get_device(
    device_id: str,
    current_user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    device = await _get_device_or_404(db, device_id, current_user)
    return _build_device_response(device)


# ══════════════════════════════════════════════════════════════════════════════
# GET /api/devices/{device_id}/history  — Historical telemetry
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/{device_id}/history",
    response_model=TelemetryHistoryResponse,
    summary="Get historical telemetry",
    description=(
        "Returns time-series telemetry data for charts and trend analysis. "
        "Each reading contains the full raw payload — all fields included. "
        "New sensor fields added to ESP32 firmware appear automatically."
    ),
)
async def get_device_history(
    device_id: str,
    # Query parameters — e.g. ?from=2026-04-13T00:00:00Z&to=2026-04-13T23:59:59Z
    from_time: datetime = Query(
        default=None,
        alias="from",
        description="Start time (ISO 8601). Defaults to 24 hours ago.",
        example="2026-04-13T00:00:00Z",
    ),
    to_time: datetime = Query(
        default=None,
        alias="to",
        description="End time (ISO 8601). Defaults to now.",
        example="2026-04-13T23:59:59Z",
    ),
    limit: int = Query(
        default=1000,
        ge=1,
        le=10000,
        description="Max readings to return.",
    ),
    status_filter: Optional[int] = Query(
        default=None,
        alias="status",
        description="Filter by status code (0=comm_error, 1=running, 2=alarm).",
    ),
    current_user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    # Verify access
    await _get_device_or_404(db, device_id, current_user)

    # Default time range: last 24 hours
    now = datetime.now(timezone.utc)
    if not to_time:
        to_time = now
    if not from_time:
        from_time = now - timedelta(hours=24)

    logger.info(
        f"[Devices] History query — device={device_id} "
        f"from={from_time} to={to_time} limit={limit}"
    )

    readings = await telemetry_service.get_device_history(
        db=db,
        device_id=device_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        status_filter=status_filter,
    )

    # Parse payload JSON string back to dict for each reading
    reading_responses = []
    for r in readings:
        try:
            payload_dict = json.loads(r.payload)
        except (json.JSONDecodeError, TypeError):
            payload_dict = {}

        reading_responses.append(TelemetryResponse(
            id=r.id,
            device_id=r.device_id,
            timestamp=r.timestamp,
            status=r.status,
            payload=payload_dict,
        ))

    return TelemetryHistoryResponse(
        device_id=device_id,
        from_time=from_time,
        to_time=to_time,
        total_readings=len(reading_responses),
        readings=reading_responses,
    )


# ══════════════════════════════════════════════════════════════════════════════
# GET /api/devices/{device_id}/status  — Quick status check
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/{device_id}/status",
    summary="Get device live status",
    description="Lightweight endpoint — returns just the current status string.",
)
async def get_device_status(
    device_id: str,
    current_user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    device = await _get_device_or_404(db, device_id, current_user)
    return {
        "device_id": device_id,
        "status": _derive_status(device),
        "last_seen_at": device.last_seen_at,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Private Helper
# ══════════════════════════════════════════════════════════════════════════════

async def _get_device_or_404(
    db: AsyncSession,
    device_id: str,
    current_user: User,
) -> Device:
    """
    Fetch a device by ID, checking both existence and user access.
    Raises HTTP 404 if not found or user doesn't have access.

    NOTE: We return 404 (not 403) even for unauthorized access.
    This prevents attackers from enumerating valid device IDs.
    """
    query = select(Device).where(
        Device.id == device_id,
        Device.is_active == True,
    )

    # Non-admin users can only see their own devices
    if current_user.role != "admin":
        query = query.where(Device.owner_user_id == current_user.id)

    result = await db.execute(query)
    device = result.scalar_one_or_none()

    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": f"Device '{device_id}' not found"},
        )

    return device
