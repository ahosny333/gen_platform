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
Updated for Many-to-Many:
  AssignDeviceRequest now accepts a LIST of user IDs instead of one user.
═══════════════════════════════════════════════════════════════════════════════
"""

from typing import List,Optional
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
        "user_ids": ["user_abc123"]
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
    # Optional: assign to users immediately on creation
    user_ids: List[str] = Field(
        default=[],
        example=["admin_01", "user_01"],
        description="List of user IDs to assign this device to on creation.",
    )


class UpdateDeviceRequest(BaseModel):
    """
    Body for PUT /api/admin/devices/{device_id}
    All fields optional — send only what needs to change.

    Example (rename only):
    {
        "name": "Generator 5 - Relocated to Site D"
    }

    Does NOT change user assignments (use /assign endpoint for that).
    """
    name: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    location: Optional[str] = Field(default=None)
    is_active: Optional[bool] = Field(
        default=None,
        description="Set false to deactivate (soft delete)",
    )


class AssignDeviceRequest(BaseModel):
    """
    Body for POST /api/admin/devices/{device_id}/assign

    Replaces the FULL user list for this device.
    Whatever you send here becomes the complete access list.

    Example — give gen_01 access to 3 users:
    { "user_ids": ["admin_01", "user_01", "user_02"] }

    Example — remove ALL user access (unassign everyone):
    { "user_ids": [] }

    Example — give access to only one user:
    { "user_ids": ["user_03"] }
    """
    user_ids: List[str] = Field(
        ...,
        example=["admin_01", "user_01", "user_02"],
        description=(
            "Complete list of user IDs that should have access to this device. "
            "Replaces existing assignments entirely. "
            "Send empty list [] to remove all user access."
        ),
    )