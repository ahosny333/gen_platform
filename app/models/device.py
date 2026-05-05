"""
app/models/device.py
─────────────────────
Device Database Model
═══════════════════════════════════════════════════════════════════════════════

Represents a physical generator with its IoT controller.

Design decision — NO hardcoded sensor columns:
  Instead of columns like rpm=Float, oil_pressure=Float, etc.,
  we store the latest raw telemetry as JSON in `last_reading`.
  This means when the ESP32 starts sending new variables, you
  just update the frontend — zero database changes needed.

  Historical readings use the same pattern in telemetry_readings.py
  New:  user_devices table has multiple rows per device (many users)
═══════════════════════════════════════════════════════════════════════════════
"""

from sqlalchemy import Boolean, Column, String, DateTime, Text, ForeignKey
from sqlalchemy.sql import func

from app.db.database import Base


class Device(Base):
    """
    Represents a row in the `devices` table.
    One row = one physical generator unit.
    """

    __tablename__ = "devices"

    # ── Identity ───────────────────────────────────────────────────────────────
    id = Column(
        String,
        primary_key=True,
        index=True,
        # Example: "gen_01", "gen_02"
        # Matches the device_id used in MQTT topics: generator/gen_01/data
    )

    name = Column(
        String,
        nullable=False,
        # Example: "Generator 1 - Site A"
    )

    description = Column(
        String,
        nullable=True,
        # Example: "Main backup generator - Floor 3"
    )

    location = Column(
        String,
        nullable=True,
        # Example: "Building A - Basement"
    )

    # ── NOTE: No owner_user_id here anymore ───────────────────────────────────
    # Access control is handled by the user_devices junction table.
    # root role bypasses that table and sees all devices directly.

    # # ── Ownership ──────────────────────────────────────────────────────────────
    # owner_user_id = Column(
    #     String,
    #     ForeignKey("users.id"),
    #     nullable=True,
    #     # Which customer this device belongs to.
    #     # NULL = internal device (visible to all admins)
    #     # Set to a user_id = only that customer can see it
    # )

    # ── Status ─────────────────────────────────────────────────────────────────
    is_active = Column(
        Boolean,
        default=True,
        nullable=False,
        # False = device decommissioned (soft delete)
    )

    # ── Last Known Telemetry (JSON string) ────────────────────────────────────
    last_reading = Column(
        Text,
        nullable=True,
        # Stores the FULL last MQTT payload as a JSON string.
        # Example value stored:
        # '{"status":1,"rpm":1784,"oil_p":5068,"cool_t":85.2,...}'
        #
        # WHY JSON AND NOT SEPARATE COLUMNS?
        # Because your ESP32 firmware will evolve. New sensors get added.
        # With JSON we store EVERYTHING without touching the database schema.
        # The frontend reads what it needs from the JSON dynamically.
    )

    last_seen_at = Column(
        DateTime(timezone=True),
        nullable=True,
        # Updated every time an MQTT message arrives from this device.
        # Used to determine if device is "offline" (no message for X minutes)
    )

    # ── Timestamps ─────────────────────────────────────────────────────────────
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<Device id={self.id} name={self.name}>"
