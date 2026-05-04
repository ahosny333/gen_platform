"""
app/services/telemetry_service.py
───────────────────────────────────
Telemetry Persistence Service
═══════════════════════════════════════════════════════════════════════════════

Responsible for:
  1. Saving each MQTT message as a TelemetryReading row in the DB
  2. Updating the Device's last_reading + last_seen_at snapshot
  3. Being called from the WebSocket broadcaster (Step 4)

This service is the bridge between the live MQTT stream and the database.

Data flow:
  MQTT on_message()
       ↓
  shared_state.update_from_mqtt()   ← already done in Step 1
       ↓
  telemetry_service.save_reading()  ← THIS FILE handles DB persistence
       ↓
  PostgreSQL / SQLite
═══════════════════════════════════════════════════════════════════════════════
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.models.device import Device
from app.models.telemetry import TelemetryReading


class TelemetryService:

    async def save_reading(
        self,
        db: AsyncSession,
        device_id: str,
        payload: Dict[str, Any],
    ) -> Optional[TelemetryReading]:
        """
        Persist one MQTT telemetry message to the database.

        Steps:
          1. Parse timestamp from payload (fallback to server time)
          2. Insert a new TelemetryReading row with full JSON payload
          3. Update Device.last_reading and Device.last_seen_at
          4. Return the saved reading

        Args:
            db:        Active database session (injected by FastAPI)
            device_id: The generator ID from the MQTT topic
            payload:   The full parsed MQTT JSON message

        Returns:
            The saved TelemetryReading object, or None on error.
        """

        # ── Step 1: Parse timestamp ────────────────────────────────────────────
        timestamp = self._parse_timestamp(payload)

        # ── Step 2: Save telemetry reading ────────────────────────────────────
        reading = TelemetryReading(
            device_id=device_id,
            timestamp=timestamp,
            status=payload.get("status"),      # Indexed separately for fast queries
            payload=json.dumps(payload),       # Full raw payload as JSON string
        )
        db.add(reading)

        # ── Step 3: Update device last_reading snapshot ────────────────────────
        # We do this so the Device List API can show current state
        # without querying the telemetry table every time
        await db.execute(
            update(Device)
            .where(Device.id == device_id)
            .values(
                last_reading=json.dumps(payload),
                last_seen_at=timestamp,
            )
        )

        await db.flush()   # Write to DB within this transaction (no commit yet)

        logger.debug(
            f"[Telemetry] Saved reading for {device_id} "
            f"| status={payload.get('status')} | ts={timestamp}"
        )

        return reading

    async def get_device_history(
        self,
        db: AsyncSession,
        device_id: str,
        from_time: datetime,
        to_time: datetime,
        limit: int = 1000,
        status_filter: Optional[int] = None,
    ) -> list[TelemetryReading]:
        """
        Query historical telemetry readings for a device within a time range.

        Args:
            db:            Active database session
            device_id:     Which device to query
            from_time:     Start of time range (inclusive)
            to_time:       End of time range (inclusive)
            limit:         Max rows to return (default 1000)
            status_filter: Optional — filter to specific status code

        Returns:
            List of TelemetryReading objects ordered by timestamp ascending.
        """

        # Build query — always filter by device + time range
        query = (
            select(TelemetryReading)
            .where(TelemetryReading.device_id == device_id)
            .where(TelemetryReading.timestamp >= from_time)
            .where(TelemetryReading.timestamp <= to_time)
            .order_by(TelemetryReading.timestamp.asc())
            .limit(limit)
        )

        # Optionally filter by status (e.g. only get alarm readings)
        if status_filter is not None:
            query = query.where(TelemetryReading.status == status_filter)

        result = await db.execute(query)
        readings = result.scalars().all()

        logger.debug(
            f"[Telemetry] History query for {device_id}: "
            f"{len(readings)} readings between {from_time} and {to_time}"
        )

        return readings

    def _parse_timestamp(self, payload: Dict[str, Any]) -> datetime:
        """
        Extract timestamp from MQTT payload.
        Falls back to current server time if missing or unparseable.

        The ESP32 sends: "timestamp": "2026-04-13T10:30:05Z"
        We parse it to a Python datetime object for DB storage.
        """
        raw_ts = payload.get("timestamp")
        if raw_ts:
            try:
                # Handle both "Z" suffix and "+00:00" format
                ts = raw_ts.replace("Z", "+00:00")
                return datetime.fromisoformat(ts)
            except (ValueError, AttributeError):
                logger.warning(
                    f"[Telemetry] Could not parse timestamp '{raw_ts}' "
                    "— using server time"
                )

        # Fallback: use server receive time
        return datetime.now(timezone.utc)


# ── Singleton ──────────────────────────────────────────────────────────────────
telemetry_service = TelemetryService()
