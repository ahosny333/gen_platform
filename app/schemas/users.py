"""
app/schemas/users.py
─────────────────────
User Management Schemas
═══════════════════════════════════════════════════════════════════════════════

Shapes for:
  CreateUserRequest   → POST /api/users/
  UpdateUserRequest   → PUT  /api/users/{user_id}
  UserResponse        → returned by all user endpoints
  UserListResponse    → returned by GET /api/users/
═══════════════════════════════════════════════════════════════════════════════
"""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, EmailStr, Field


# ── Allowed role values ────────────────────────────────────────────────────────
# Literal type means Pydantic rejects any value not in this list
UserRole = Literal["root", "admin", "user"]


# ── Request Schemas ────────────────────────────────────────────────────────────

class CreateUserRequest(BaseModel):
    """
    Body for POST /api/users/
    Root sends this to create a new user account.

    Example:
    {
        "email": "customer@company.com",
        "password": "securepass123",
        "full_name": "Ahmed Hassan",
        "role": "user"
    }
    """
    email: EmailStr = Field(
        ...,
        example="customer@company.com",
    )
    password: str = Field(
        ...,
        min_length=6,
        example="securepass123",
        description="Minimum 6 characters",
    )
    full_name: Optional[str] = Field(
        default=None,
        example="Ahmed Hassan",
    )
    role: UserRole = Field(
        default="user",
        example="user",
        description="Role: root | admin | user",
    )


class UpdateUserRequest(BaseModel):
    """
    Body for PUT /api/users/{user_id}
    All fields are optional — send only what needs to change.

    Example (change role only):
    {
        "role": "admin"
    }

    Example (change name and deactivate):
    {
        "full_name": "Ahmed Hassan (inactive)",
        "is_active": false
    }
    """
    email: Optional[EmailStr] = Field(default=None)
    full_name: Optional[str] = Field(default=None)
    role: Optional[UserRole] = Field(default=None)
    is_active: Optional[bool] = Field(default=None)
    password: Optional[str] = Field(
        default=None,
        min_length=6,
        description="Only include to change the password",
    )


# ── Response Schemas ───────────────────────────────────────────────────────────

class UserResponse(BaseModel):
    """
    Shape of a user object returned by the API.
    Never includes hashed_password.
    """
    id: str = Field(example="user_abc123")
    email: EmailStr = Field(example="customer@company.com")
    full_name: Optional[str] = Field(default=None, example="Ahmed Hassan")
    role: str = Field(example="user")
    is_active: bool = Field(example=True)
    created_at: datetime

    # Devices assigned to this user (populated in list/detail endpoints)
    assigned_devices: Optional[List[str]] = Field(
        default=None,
        description="List of device IDs assigned to this user",
        example=["gen_01", "gen_03"],
    )

    class Config:
        from_attributes = True


class UserListResponse(BaseModel):
    """Wrapper for the users list endpoint."""
    total: int = Field(example=5)
    users: List[UserResponse]
