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

Key change in assign endpoint:
  Old: assign ONE user to a device
  New: set the COMPLETE user list for a device (replaces all assignments)

  POST /api/admin/devices/{device_id}/assign
  Body: { "user_ids": ["admin_01", "user_01", "user_02"] }
  → deletes all existing assignments for this device
  → creates new assignments for each user_id in the list
  → send [] to remove all assignments

Note:
  Regular device reading (GET /api/devices/) is in routes/devices.py
  and is accessible to all authenticated users based on their role.
  This file handles MANAGEMENT operations — create, edit, delete, assign.
═══════════════════════════════════════════════════════════════════════════════
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_root
from app.core.logging import logger
from app.db.database import get_db
from app.models.device import Device
from app.models.user import User
from app.models.user_device import UserDevice
from app.schemas.admin_devices import (
    CreateDeviceRequest,
    UpdateDeviceRequest,
    AssignDeviceRequest,
)
from app.schemas.devices import DeviceResponse, DeviceListResponse
from app.api.routes.devices import _build_device_response

router = APIRouter()

async def _get_user_ids_for_device(db: AsyncSession, device_id: str):
    result = await db.execute(
        select(UserDevice.user_id).where(UserDevice.device_id == device_id)
    )
    return [row[0] for row in result.fetchall()]

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
    # Check device_id not taken
    existing = await db.execute(
        select(Device).where(Device.id == request.device_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": f"Device ID '{request.device_id}' already exists"},
        )

    new_device = Device(
        id=request.device_id, name=request.name,
        description=request.description, location=request.location,
        is_active=True,
    )
    db.add(new_device)
    await db.flush()

    # Assign users immediately if provided
    assigned = []
    for user_id in request.user_ids:
        user_check = await db.execute(
            select(User).where(User.id == user_id, User.is_active == True)
        )
        if user_check.scalar_one_or_none():
            db.add(UserDevice(
                user_id=user_id, device_id=request.device_id,
                assigned_by=current_user.id,
            ))
            assigned.append(user_id)
        else:
            logger.warning(f"[Admin] User '{user_id}' not found — skipped in assignment")

    await db.flush()
    logger.info(f"[Admin] Device created: {request.device_id} assigned_to={assigned}")
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
    if request.name        is not None: device.name        = request.name
    if request.description is not None: device.description = request.description
    if request.location    is not None: device.location    = request.location
    if request.is_active   is not None: device.is_active   = request.is_active
    await db.flush()
    logger.info(f"[Admin] Device updated: {device_id}")
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
    logger.info(f"[Admin] Device deactivated: {device_id}")
    return {"message": f"Device '{device_id}' deactivated", "device_id": device_id}



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
    """
    Replaces the COMPLETE user access list for this device.

    How it works:
      1. Delete ALL existing assignments for this device
      2. Create new assignments for each user_id in the request list
      3. Invalid/inactive user_ids are skipped with a warning

    Examples:
      {"user_ids": ["admin_01", "user_01"]}  → these 2 users get access
      {"user_ids": []}                        → remove ALL user access
    """
    await _get_device_or_404(db, device_id)

    # Step 1: Delete all existing assignments for this device
    await db.execute(
        delete(UserDevice).where(UserDevice.device_id == device_id)
    )

    # Step 2: Create new assignments
    assigned = []
    skipped = []
    for user_id in request.user_ids:
        user_check = await db.execute(
            select(User).where(User.id == user_id, User.is_active == True)
        )
        if user_check.scalar_one_or_none():
            db.add(UserDevice(
                user_id=user_id, device_id=device_id,
                assigned_by=current_user.id,
            ))
            assigned.append(user_id)
        else:
            skipped.append(user_id)
            logger.warning(f"[Admin] User '{user_id}' not found — skipped in assignment")

    await db.flush()

    logger.info(
        f"[Admin] Device {device_id} access updated — "
        f"assigned={assigned} skipped={skipped}"
    )

    # Refresh device and return
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one()
    return _build_device_response(device)


# ══════════════════════════════════════════════════════════════════════════════
# GET /api/admin/devices/{device_id}/users  — Who has access to this device?
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/devices/{device_id}/users",
            summary="List users with access to this device")
async def get_device_users(
    device_id: str,
    current_user: User = Depends(require_root),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns the list of users currently assigned to this device.
    Useful for the root management UI to show who can see a device.
    """
    await _get_device_or_404(db, device_id)
    user_ids = await _get_user_ids_for_device(db, device_id)

    # Fetch user details for each assigned user
    users_info = []
    for uid in user_ids:
        result = await db.execute(select(User).where(User.id == uid))
        u = result.scalar_one_or_none()
        if u:
            users_info.append({
                "user_id": u.id,
                "email": u.email,
                "full_name": u.full_name,
                "role": u.role,
                "is_active": u.is_active,
            })

    return {
        "device_id": device_id,
        "assigned_users": users_info,
        "total": len(users_info),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Private Helper
# ══════════════════════════════════════════════════════════════════════════════

async def _get_device_or_404(db: AsyncSession, device_id: str) -> Device:
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": f"Device '{device_id}' not found"},
        )
    return device
