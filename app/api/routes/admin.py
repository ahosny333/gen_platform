"""
app/api/routes/admin.py
────────────────────────
Device Management Routes — Root Only
═══════════════════════════════════════════════════════════════════════════════

All endpoints here require role = root.

Endpoints:
  POST   /api/admin/devices/                      → create new device
  PUT    /api/admin/devices/{device_id}            → edit device
  DELETE /api/admin/devices/{device_id}            → deactivate device
  POST   /api/admin/devices/{device_id}/assign     → assign device to user
  GET    /api/admin/devices/                       → list ALL devices (incl inactive)

Note:
  Regular device reading (GET /api/devices/) is in routes/devices.py
  and is accessible to all authenticated users based on their role.
  This file handles MANAGEMENT operations — create, edit, delete, assign.
═══════════════════════════════════════════════════════════════════════════════
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_root
from app.core.logging import logger
from app.db.database import get_db
from app.models.device import Device
from app.models.user import User
from app.schemas.admin_devices import (
    CreateDeviceRequest,
    UpdateDeviceRequest,
    AssignDeviceRequest,
)
from app.schemas.devices import DeviceResponse, DeviceListResponse
from app.api.routes.devices import _build_device_response

router = APIRouter()


# ══════════════════════════════════════════════════════════════════════════════
# GET /api/admin/devices/  — List ALL devices including inactive
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/devices/",
    response_model=DeviceListResponse,
    summary="List all devices (admin view)",
    description=(
        "Root only. Returns ALL devices including inactive ones. "
        "Also shows owner user info. "
        "Use GET /api/devices/ for the regular filtered view."
    ),
)
async def admin_list_devices(
    current_user: User = Depends(require_root),
    db: AsyncSession = Depends(get_db),
):
    logger.info(f"[Admin] GET /admin/devices — root={current_user.id}")

    # Return ALL devices regardless of is_active status
    result = await db.execute(
        select(Device).order_by(Device.created_at.desc())
    )
    devices = result.scalars().all()

    device_responses = [_build_device_response(d) for d in devices]

    return DeviceListResponse(
        total=len(device_responses),
        devices=device_responses,
    )


# ══════════════════════════════════════════════════════════════════════════════
# POST /api/admin/devices/  — Create new device
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/devices/",
    response_model=DeviceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create new device",
    description=(
        "Root only. Register a new generator device. "
        "The device_id must match what the ESP32 uses in its MQTT topic."
    ),
)
async def create_device(
    request: CreateDeviceRequest,
    current_user: User = Depends(require_root),
    db: AsyncSession = Depends(get_db),
):
    logger.info(
        f"[Admin] Creating device — id={request.device_id} "
        f"name='{request.name}' by root={current_user.id}"
    )

    # Check device_id not already taken
    existing = await db.execute(
        select(Device).where(Device.id == request.device_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": f"Device ID '{request.device_id}' already exists"},
        )

    # If owner_user_id provided, verify that user exists
    if request.owner_user_id:
        user_check = await db.execute(
            select(User).where(
                User.id == request.owner_user_id,
                User.is_active == True,
            )
        )
        if not user_check.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": (
                        f"User '{request.owner_user_id}' not found or inactive. "
                        "Cannot assign device to non-existent user."
                    )
                },
            )

    new_device = Device(
        id=request.device_id,
        name=request.name,
        description=request.description,
        location=request.location,
        owner_user_id=request.owner_user_id,
        is_active=True,
    )

    db.add(new_device)
    await db.flush()

    logger.info(f"[Admin] ✅ Device created: {request.device_id}")

    return _build_device_response(new_device)


# ══════════════════════════════════════════════════════════════════════════════
# PUT /api/admin/devices/{device_id}  — Update device
# ══════════════════════════════════════════════════════════════════════════════

@router.put(
    "/devices/{device_id}",
    response_model=DeviceResponse,
    summary="Update device",
    description="Root only. Update device info. Send only fields to change.",
)
async def update_device(
    device_id: str,
    request: UpdateDeviceRequest,
    current_user: User = Depends(require_root),
    db: AsyncSession = Depends(get_db),
):
    device = await _get_device_or_404(db, device_id)

    if request.name is not None:
        device.name = request.name

    if request.description is not None:
        device.description = request.description

    if request.location is not None:
        device.location = request.location

    if request.is_active is not None:
        device.is_active = request.is_active

    # owner_user_id can be set to None (unassign) or a new user ID
    if "owner_user_id" in request.model_fields_set:
        if request.owner_user_id is not None:
            # Verify the new owner exists
            user_check = await db.execute(
                select(User).where(
                    User.id == request.owner_user_id,
                    User.is_active == True,
                )
            )
            if not user_check.scalar_one_or_none():
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "error": f"User '{request.owner_user_id}' not found or inactive"
                    },
                )
        device.owner_user_id = request.owner_user_id

    await db.flush()

    logger.info(
        f"[Admin] ✅ Device updated: {device_id} "
        f"by root={current_user.id}"
    )

    return _build_device_response(device)


# ══════════════════════════════════════════════════════════════════════════════
# DELETE /api/admin/devices/{device_id}  — Deactivate device
# ══════════════════════════════════════════════════════════════════════════════

@router.delete(
    "/devices/{device_id}",
    summary="Deactivate device",
    description=(
        "Root only. Soft-deletes the device — marks it inactive. "
        "Historical telemetry data is preserved. "
        "MQTT data from the device will still arrive but be ignored."
    ),
)
async def delete_device(
    device_id: str,
    current_user: User = Depends(require_root),
    db: AsyncSession = Depends(get_db),
):
    device = await _get_device_or_404(db, device_id)
    device.is_active = False
    await db.flush()

    logger.info(
        f"[Admin] Device deactivated: {device_id} "
        f"by root={current_user.id}"
    )

    return {
        "message": f"Device '{device_id}' has been deactivated",
        "device_id": device_id,
    }


# ══════════════════════════════════════════════════════════════════════════════
# POST /api/admin/devices/{device_id}/assign  — Assign to customer
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/devices/{device_id}/assign",
    response_model=DeviceResponse,
    summary="Assign device to customer",
    description=(
        "Root only. Quickly assign or unassign a device to a customer user. "
        "Send owner_user_id to assign, or null to unassign."
    ),
)
async def assign_device(
    device_id: str,
    request: AssignDeviceRequest,
    current_user: User = Depends(require_root),
    db: AsyncSession = Depends(get_db),
):
    device = await _get_device_or_404(db, device_id)

    if request.owner_user_id is not None:
        # Verify target user exists and is a 'user' role (customers only)
        user_check = await db.execute(
            select(User).where(
                User.id == request.owner_user_id,
                User.is_active == True,
            )
        )
        target_user = user_check.scalar_one_or_none()
        if not target_user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": f"User '{request.owner_user_id}' not found or inactive"
                },
            )
        device.owner_user_id = request.owner_user_id
        action = f"assigned to user '{request.owner_user_id}'"
    else:
        device.owner_user_id = None
        action = "unassigned (now admin-only)"

    await db.flush()

    logger.info(
        f"[Admin] Device {device_id} {action} "
        f"by root={current_user.id}"
    )

    return _build_device_response(device)


# ══════════════════════════════════════════════════════════════════════════════
# Private Helper
# ══════════════════════════════════════════════════════════════════════════════

async def _get_device_or_404(db: AsyncSession, device_id: str) -> Device:
    """Fetch device by ID regardless of is_active status (admin view)."""
    result = await db.execute(
        select(Device).where(Device.id == device_id)
    )
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": f"Device '{device_id}' not found"},
        )
    return device
