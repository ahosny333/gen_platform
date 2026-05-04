"""
app/schemas/admin_devices.py
─────────────────────────────
Device Management Schemas (Root Admin)
═══════════════════════════════════════════════════════════════════════════════

Shapes for the device management endpoints:
  CreateDeviceRequest → POST /api/admin/devices/
  UpdateDeviceRequest → PUT  /api/admin/devices/{device_id}
  AssignDeviceRequest → POST /api/admin/devices/{device_id}/assign

Note: DeviceResponse for reading is already in schemas/devices.py
These schemas are specifically for CREATE and UPDATE operations.
═══════════════════════════════════════════════════════════════════════════════
"""

from typing import Optional
from pydantic import BaseModel, Field


class CreateDeviceRequest(BaseModel):
    """
    Body for POST /api/admin/devices/
    Root creates a new generator device.

    device_id must match what the ESP32 uses in its MQTT topic.
    Example: if ESP32 publishes to generator/gen_05/data
             then device_id = "gen_05"

    Example body:
    {
        "device_id": "gen_05",
        "name": "Generator 5 - Site C",
        "description": "Emergency backup unit",
        "location": "Tower C - Level B2",
        "owner_user_id": "user_abc123"
    }
    """
    device_id: str = Field(
        ...,
        example="gen_05",
        description=(
            "Must match the device_id in ESP32 MQTT topic: "
            "generator/{device_id}/data"
        ),
    )
    name: str = Field(
        ...,
        example="Generator 5 - Site C",
    )
    description: Optional[str] = Field(
        default=None,
        example="Emergency backup unit",
    )
    location: Optional[str] = Field(
        default=None,
        example="Tower C - Level B2",
    )
    owner_user_id: Optional[str] = Field(
        default=None,
        example="user_abc123",
        description="Assign to a customer user. Leave null for admin-only access.",
    )


class UpdateDeviceRequest(BaseModel):
    """
    Body for PUT /api/admin/devices/{device_id}
    All fields optional — send only what needs to change.

    Example (rename only):
    {
        "name": "Generator 5 - Relocated to Site D"
    }

    Example (reassign to different customer):
    {
        "owner_user_id": "user_xyz789"
    }

    Example (unassign from customer — make admin-only):
    {
        "owner_user_id": null
    }
    """
    name: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    location: Optional[str] = Field(default=None)
    owner_user_id: Optional[str] = Field(
        default=None,
        description="Set to null to remove customer assignment",
    )
    is_active: Optional[bool] = Field(
        default=None,
        description="Set false to deactivate (soft delete)",
    )


class AssignDeviceRequest(BaseModel):
    """
    Body for POST /api/admin/devices/{device_id}/assign
    Quickly assign or unassign a device to a customer.

    Example (assign):
    { "owner_user_id": "user_abc123" }

    Example (unassign):
    { "owner_user_id": null }
    """
    owner_user_id: Optional[str] = Field(
        default=None,
        example="user_abc123",
        description="User ID to assign to, or null to unassign",
    )
