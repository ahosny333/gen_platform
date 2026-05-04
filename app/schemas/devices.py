"""
app/schemas/devices.py
───────────────────────
Device & Telemetry API Schemas (Pydantic)
═══════════════════════════════════════════════════════════════════════════════

Defines the shape of data returned by:
  GET /api/devices              → list of DeviceResponse
  GET /api/devices/{id}         → single DeviceResponse
  GET /api/devices/{id}/history → list of TelemetryResponse

KEY DESIGN — Dynamic payload field:
  TelemetryResponse has a `payload` field typed as Dict[str, Any].
  This means it returns WHATEVER JSON is stored — no hardcoded fields.
  When ESP32 adds new sensors, the API automatically returns them.
  Frontend reads what it needs dynamically.
═══════════════════════════════════════════════════════════════════════════════
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Device Schemas ─────────────────────────────────────────────────────────────

class DeviceResponse(BaseModel):
    """
    Shape of a single device returned by the API.

    Example response:
    {
        "device_id": "gen_01",
        "name": "Generator 1 - Site A",
        "description": "Main backup generator",
        "location": "Building A - Basement",
        "status": "running",
        "last_seen_at": "2026-04-13T10:30:05Z",
        "last_reading": {
            "rpm": 1784,
            "fuel_l": 64,
            "cool_t": 85.2,
            ... all fields from ESP32
        }
    }
    """

    device_id: str = Field(example="gen_01")
    name: str = Field(example="Generator 1 - Site A")
    description: Optional[str] = Field(default=None)
    location: Optional[str] = Field(default=None)

    # Derived status string — computed from last_reading data
    # "running" | "alarm" | "offline" | "comm_error"
    status: str = Field(example="running")

    last_seen_at: Optional[datetime] = Field(default=None)

    # The full last telemetry snapshot — dynamic, no hardcoded fields
    # Frontend reads rpm, fuel_l, cool_t etc. from here
    last_reading: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Full last telemetry payload. Fields vary by ESP32 firmware version.",
        example={
            "status": 1,
            "rpm": 1784,
            "oil_p": 5068,
            "cool_t": 85.2,
            "fuel_l": 64,
            "bat_v": 24.2,
            "f": 50.4,
        }
    )

    class Config:
        from_attributes = True   # Allows building from SQLAlchemy model objects


class DeviceListResponse(BaseModel):
    """Wrapper for the devices list endpoint."""
    total: int = Field(example=3)
    devices: List[DeviceResponse]


# ── Telemetry / History Schemas ────────────────────────────────────────────────

class TelemetryResponse(BaseModel):
    """
    Shape of a single historical reading returned by the history API.

    Example response item:
    {
        "id": 1042,
        "device_id": "gen_01",
        "timestamp": "2026-04-13T10:30:05Z",
        "status": 1,
        "payload": {
            "rpm": 1784,
            "oil_p": 5068,
            "cool_t": 85.2,
            "fuel_l": 64,
            "v": [219.5, 220.6, 218.5],
            "a": [99.1, 102.8, 98.4],
            "w": [20260, 21422, 21138],
            ... all fields stored, including future ones
        }
    }

    The `payload` field is fully dynamic — it returns exactly what
    was stored from the MQTT message. No fields are hardcoded here.
    """

    id: int = Field(example=1042)
    device_id: str = Field(example="gen_01")
    timestamp: datetime
    status: Optional[int] = Field(
        default=None,
        description="0=comm error, 1=running, 2=alarm"
    )

    # Dynamic — returns all fields from ESP32 payload
    payload: Dict[str, Any] = Field(
        description="Complete telemetry snapshot. All fields are dynamic.",
    )

    class Config:
        from_attributes = True


class TelemetryHistoryResponse(BaseModel):
    """Wrapper for the history list endpoint."""
    device_id: str
    from_time: datetime
    to_time: datetime
    total_readings: int
    readings: List[TelemetryResponse]


# ── Query Parameter Schema ─────────────────────────────────────────────────────

class HistoryQueryParams(BaseModel):
    """
    Validated query parameters for the history endpoint.
    Used internally — FastAPI reads these from the URL query string.

    Example URL:
    GET /api/devices/gen_01/history
        ?from=2026-04-13T00:00:00Z
        &to=2026-04-13T23:59:59Z
        &limit=500
        &status=1
    """

    # Time range
    from_time: datetime = Field(alias="from")
    to_time: datetime = Field(alias="to")

    # Optional filters
    limit: int = Field(
        default=1000,
        ge=1,
        le=10000,
        description="Max readings to return. Default 1000, max 10000."
    )
    status: Optional[int] = Field(
        default=None,
        description="Filter by status code. E.g. status=2 returns only alarms."
    )
