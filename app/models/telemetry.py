"""
app/models/telemetry.py
────────────────────────
Telemetry Reading Database Model
═══════════════════════════════════════════════════════════════════════════════

Stores ONE row per MQTT message received from a device.
This is the historical data table — queried by the History API.

KEY DESIGN DECISION — Flexible JSON storage:
─────────────────────────────────────────────
  Option A (rigid):
    rpm FLOAT, oil_pressure FLOAT, coolant_temp FLOAT, fuel_level FLOAT ...
    Problem: When ESP32 adds a new sensor → ALTER TABLE needed → downtime

  Option B (flexible) ← WE USE THIS:
    payload JSON  →  stores the complete raw reading as JSON string
    Benefit: ESP32 can add/remove/rename any field, zero DB changes.
    The API returns the raw JSON, frontend decides what to display.

  We also keep a few INDEXED columns (device_id, timestamp, status)
  for fast querying — you can't efficiently query inside JSON.
═══════════════════════════════════════════════════════════════════════════════
"""

from sqlalchemy import Column, String, DateTime, Text, Integer, ForeignKey, Index
from sqlalchemy.sql import func

from app.db.database import Base


class TelemetryReading(Base):
    """
    Represents one row in the `telemetry_readings` table.
    One row = one MQTT message = one snapshot in time from one device.
    """

    __tablename__ = "telemetry_readings"

    # ── Primary Key ────────────────────────────────────────────────────────────
    id = Column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    # ── Indexed Columns (for fast querying) ────────────────────────────────────
    device_id = Column(
        String,
        ForeignKey("devices.id"),
        nullable=False,
        index=True,
        # Indexed: we always filter by device_id in history queries
    )

    timestamp = Column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
        # Indexed: we always filter/sort by time in history queries
        # Value comes from the MQTT payload timestamp field.
        # If missing from payload, we use server receive time.
    )

    status = Column(
        Integer,
        nullable=True,
        # Copied from payload for fast status filtering.
        # 0 = Modbus fail, 1 = running, 2 = alarm
        # Stored separately so we can query:
        #   "give me all alarm events in last 7 days"
        # without parsing JSON for every row.
    )

    # ── Full Raw Payload (flexible JSON) ──────────────────────────────────────
    payload = Column(
        Text,
        nullable=False,
        # The COMPLETE raw MQTT message stored as JSON string.
        # Example:
        # '{
        #    "device_id": "gen_01",
        #    "timestamp": "2026-04-13T10:30:05Z",
        #    "status": 1,
        #    "rpm": 1784,
        #    "oil_p": 5068,
        #    "cool_t": 85.2,
        #    "fuel_l": 64,
        #    "v": [219.5, 220.6, 218.5, 378.1, 378.2, 380.0],
        #    "a": [99.1, 102.8, 98.4, 0.7],
        #    "w": [20260, 21422, 21138],
        #    "bat_v": 24.2,
        #    "f": 50.4,
        #    ... any future fields also stored here automatically
        # }'
    )

    # ── Composite Index for history queries ────────────────────────────────────
    # Most history queries look like:
    #   WHERE device_id = 'gen_01' AND timestamp BETWEEN x AND y
    # A composite index on both columns makes this extremely fast.
    __table_args__ = (
        Index("ix_telemetry_device_time", "device_id", "timestamp"),
    )

    def __repr__(self) -> str:
        return (
            f"<TelemetryReading device={self.device_id} "
            f"ts={self.timestamp} status={self.status}>"
        )
