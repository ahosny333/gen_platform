"""
app/api/routes/users.py
────────────────────────
User Management Routes — Root Only
═══════════════════════════════════════════════════════════════════════════════

All endpoints here require role = root.
Admin and User roles receive HTTP 403 on all these endpoints.

Endpoints:
  GET    /api/users/           → list all users
  POST   /api/users/           → create new user
  GET    /api/users/{user_id}  → get single user details
  PUT    /api/users/{user_id}  → update user (role, name, password, status)
  DELETE /api/users/{user_id}  → deactivate user (soft delete)
═══════════════════════════════════════════════════════════════════════════════
"""

import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_root
from app.core.logging import logger
from app.core.security import hash_password
from app.db.database import get_db
from app.models.device import Device
from app.models.user import User
from app.models.user_device import UserDevice
from app.schemas.users import (
    CreateUserRequest,
    UpdateUserRequest,
    UserResponse,
    UserListResponse,
)

router = APIRouter()

async def _get_assigned_device_ids(db: AsyncSession, user_id: str) -> List[str]:
    """Return list of device IDs assigned to a user via junction table."""
    result = await db.execute(
        select(UserDevice.device_id).where(UserDevice.user_id == user_id)
    )
    return [row[0] for row in result.fetchall()]

# ══════════════════════════════════════════════════════════════════════════════
# GET /api/users/  — List all users
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/",
    response_model=UserListResponse,
    summary="List all users",
    description="Root only. Returns all user accounts with their assigned devices.",
)
async def list_users(
    current_user: User = Depends(require_root),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    users = result.scalars().all()

    user_responses = []
    for user in users:
        assigned = await _get_assigned_device_ids(db, user.id)
        user_responses.append(UserResponse(
            id=user.id, email=user.email, full_name=user.full_name,
            role=user.role, is_active=user.is_active,
            created_at=user.created_at, assigned_devices=assigned,
        ))

    return UserListResponse(total=len(user_responses), users=user_responses)



# ══════════════════════════════════════════════════════════════════════════════
# POST /api/users/  — Create new user
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create new user",
    description="Root only. Creates a new user account with specified role.",
)
async def create_user(
    request: CreateUserRequest,
    current_user: User = Depends(require_root),
    db: AsyncSession = Depends(get_db),
):
    # Check email not taken
    existing = await db.execute(select(User).where(User.email == request.email))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": f"Email '{request.email}' already registered"},
        )

    new_id = f"{request.role}_{uuid.uuid4().hex[:8]}"
    new_user = User(
        id=new_id, email=request.email,
        hashed_password=hash_password(request.password),
        full_name=request.full_name, role=request.role, is_active=True,
    )
    db.add(new_user)
    await db.flush()

    # Assign devices if provided
    assigned = []
    for device_id in request.device_ids:
        dev = await db.execute(
            select(Device).where(Device.id == device_id, Device.is_active == True)
        )
        if dev.scalar_one_or_none():
            db.add(UserDevice(
                user_id=new_id, device_id=device_id,
                assigned_by=current_user.id,
            ))
            assigned.append(device_id)
        else:
            logger.warning(f"[Users] Device '{device_id}' not found during user creation — skipped")

    await db.flush()
    logger.info(f"[Users] Created user id={new_id} email={request.email} devices={assigned}")

    return UserResponse(
        id=new_user.id, email=new_user.email, full_name=new_user.full_name,
        role=new_user.role, is_active=new_user.is_active,
        created_at=new_user.created_at, assigned_devices=assigned,
    )



# ══════════════════════════════════════════════════════════════════════════════
# GET /api/users/{user_id}  — Get single user
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/{user_id}",
    response_model=UserResponse,
    summary="Get user details",
)
async def get_user(
    user_id: str,
    current_user: User = Depends(require_root),
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user_or_404(db, user_id)
    assigned = await _get_assigned_device_ids(db, user.id)
    return UserResponse(
        id=user.id, email=user.email, full_name=user.full_name,
        role=user.role, is_active=user.is_active,
        created_at=user.created_at, assigned_devices=assigned,
    )


# ══════════════════════════════════════════════════════════════════════════════
# PUT /api/users/{user_id}  — Update user
# ══════════════════════════════════════════════════════════════════════════════

@router.put(
    "/{user_id}",
    response_model=UserResponse,
    summary="Update user",
    description=(
        "Root only. Update any field. "
        "Send only the fields you want to change — others stay unchanged."
    ),
)
async def update_user(
    user_id: str,
    request: UpdateUserRequest,
    current_user: User = Depends(require_root),
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user_or_404(db, user_id)

    if user_id == current_user.id and request.is_active is False:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "You cannot deactivate your own account"},
        )

    if request.email is not None:
        dup = await db.execute(
            select(User).where(User.email == request.email, User.id != user_id)
        )
        if dup.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": f"Email '{request.email}' already in use"},
            )
        user.email = request.email

    if request.full_name  is not None: user.full_name  = request.full_name
    if request.role       is not None: user.role       = request.role
    if request.is_active  is not None: user.is_active  = request.is_active
    if request.password   is not None: user.hashed_password = hash_password(request.password)

    await db.flush()
    logger.info(f"[Users] Updated user id={user_id}")

    assigned = await _get_assigned_device_ids(db, user.id)
    return UserResponse(
        id=user.id, email=user.email, full_name=user.full_name,
        role=user.role, is_active=user.is_active,
        created_at=user.created_at, assigned_devices=assigned,
    )


# ══════════════════════════════════════════════════════════════════════════════
# DELETE /api/users/{user_id}  — Deactivate user (soft delete)
# ══════════════════════════════════════════════════════════════════════════════

@router.delete(
    "/{user_id}",
    summary="Deactivate user",
    description=(
        "Root only. Deactivates the user account (soft delete). "
        "The account is disabled but data is preserved. "
        "Use PUT to reactivate if needed."
    ),
)
async def delete_user(
    user_id: str,
    current_user: User = Depends(require_root),
    db: AsyncSession = Depends(get_db),
):
    if user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "You cannot deactivate your own account"},
        )
    user = await _get_user_or_404(db, user_id)
    user.is_active = False
    await db.flush()
    logger.info(f"[Users] Deactivated user id={user_id}")
    return {"message": f"User '{user.email}' deactivated", "user_id": user_id}



# ══════════════════════════════════════════════════════════════════════════════
# Private Helper
# ══════════════════════════════════════════════════════════════════════════════

async def _get_user_or_404(db: AsyncSession, user_id: str) -> User:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": f"User '{user_id}' not found"},
        )
    return user