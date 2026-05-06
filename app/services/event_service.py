"""
app/services/event_service.py
──────────────────────────────
Event & Alarm Persistence Service
═══════════════════════════════════════════════════════════════════════════════

Handles all database logic for the two event tables:
  device_last_events  → upsert current state
  events_history      → append change log

Called from data_router when event MQTT messages arrive.

Two entry points:

  process_full_state(device_id, payload)
  ────────────────────────────────────────
  Called when: generator/{device_id}/event/state arrives
  Payload:     {"low_oil_pressure": false, "high_temp": true, "fuel_low": false}
  Action:      For each event in payload:
                 - Upsert device_last_events (update value + timestamp)
                 - If value CHANGED from what was stored → insert events_history
                 - If value same → skip history (avoid noise on periodic syncs)

  process_single_update(device_id, event_name, value)
  ─────────────────────────────────────────────────────
  Called when: generator/{device_id}/event/update arrives
  Payload:     {"event": "fuel_low", "value": true}
  Action:      - Upsert device_last_events for this one event
               - ALWAYS insert into events_history (this is a real change)
═══════════════════════════════════════════════════════════════════════════════
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.models.device_event import DeviceLastEvent, EventHistory


class EventService:

    # ══════════════════════════════════════════════════════════════════════════
    # Entry Point 1: Full state dump from event/state topic
    # ══════════════════════════════════════════════════════════════════════════

    async def process_full_state(
        self,
        db: AsyncSession,
        device_id: str,
        payload: Dict[str, Any],
    ) -> None:
        """
        Process a full event state dump from the ESP32.

        Expected payload format:
            {
                "low_oil_pressure": false,
                "high_temp": true,
                "fuel_low": false,
                "any_future_event": false
            }

        For each event key in the payload:
          1. Load current stored value from device_last_events
          2. Compare with new value
          3. Always upsert device_last_events with new value + timestamp
          4. Only write to events_history if value CHANGED
             (prevents flooding history table on periodic full-state syncs)
        """
        now = datetime.now(timezone.utc)
        history_entries = []
        upserted = 0

        for event_name, new_value in payload.items():

            # Only process boolean values — skip metadata fields
            if not isinstance(new_value, bool):
                logger.debug(
                    f"[Events] Skipping non-boolean field "
                    f"'{event_name}'={new_value} for {device_id}"
                )
                continue

            # Load current stored value for comparison
            existing = await self._get_current_event(db, device_id, event_name)
            previous_value = existing.value if existing else None

            # Upsert device_last_events
            if existing:
                existing.value = new_value
                existing.last_updated = now
            else:
                db.add(DeviceLastEvent(
                    device_id=device_id,
                    event_name=event_name,
                    value=new_value,
                    last_updated=now,
                ))
            upserted += 1

            # Only log to history if value actually changed
            # (or if this is the first time we've seen this event)
            if previous_value is None or previous_value != new_value:
                history_entries.append(EventHistory(
                    device_id=device_id,
                    event_name=event_name,
                    value=new_value,
                    timestamp=now,
                    source="state",
                ))
                logger.debug(
                    f"[Events] State change detected — "
                    f"{device_id}/{event_name}: {previous_value} → {new_value}"
                )

        # Bulk insert history entries
        for entry in history_entries:
            db.add(entry)

        await db.flush()

        logger.info(
            f"[Events] Full state processed — device={device_id} "
            f"events={upserted} changes={len(history_entries)}"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Entry Point 2: Single event update from event/update topic
    # ══════════════════════════════════════════════════════════════════════════

    async def process_single_update(
        self,
        db: AsyncSession,
        device_id: str,
        event_name: str,
        new_value: bool,
    ) -> None:
        """
        Process a spontaneous single-event update from the ESP32.

        This is called when the ESP32 detects a change and immediately
        sends it without waiting for the next periodic full state dump.

        Action:
          1. Upsert device_last_events for this one event
          2. ALWAYS insert into events_history
             (spontaneous updates ARE real changes by definition)
        """
        now = datetime.now(timezone.utc)

        # Upsert device_last_events
        existing = await self._get_current_event(db, device_id, event_name)

        if existing:
            existing.value = new_value
            existing.last_updated = now
        else:
            db.add(DeviceLastEvent(
                device_id=device_id,
                event_name=event_name,
                value=new_value,
                last_updated=now,
            ))

        # Always log to history — this is a confirmed real change
        db.add(EventHistory(
            device_id=device_id,
            event_name=event_name,
            value=new_value,
            timestamp=now,
            source="update",
        ))

        await db.flush()

        logger.info(
            f"[Events] Single update — device={device_id} "
            f"event={event_name} value={new_value}"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Query: Get current event state for a device
    # ══════════════════════════════════════════════════════════════════════════

    async def get_device_events(
        self,
        db: AsyncSession,
        device_id: str,
    ) -> List[DeviceLastEvent]:
        """
        Return all current event states for a device.
        Used by GET /api/devices/{device_id}/events

        Returns a flat list — frontend groups or filters as needed.
        """
        result = await db.execute(
            select(DeviceLastEvent)
            .where(DeviceLastEvent.device_id == device_id)
            .order_by(DeviceLastEvent.event_name.asc())
        )
        return result.scalars().all()

    async def get_active_alarms(
        self,
        db: AsyncSession,
        device_id: str,
    ) -> List[DeviceLastEvent]:
        """
        Return only events that are currently TRUE (active alarms).
        Useful for the alarm indicator on the dashboard.
        """
        result = await db.execute(
            select(DeviceLastEvent)
            .where(
                DeviceLastEvent.device_id == device_id,
                DeviceLastEvent.value == True,
            )
            .order_by(DeviceLastEvent.last_updated.desc())
        )
        return result.scalars().all()

    # ══════════════════════════════════════════════════════════════════════════
    # Query: Get event history for a device
    # ══════════════════════════════════════════════════════════════════════════

    async def get_event_history(
        self,
        db: AsyncSession,
        device_id: str,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        event_name: Optional[str] = None,
        limit: int = 500,
    ) -> List[EventHistory]:
        """
        Return historical event log for a device.
        Used by GET /api/devices/{device_id}/events/history

        All filters are optional:
          from_time  → only events after this time
          to_time    → only events before this time
          event_name → filter to one specific event (e.g. "high_temp" only)
          limit      → max rows (default 500)
        """
        query = (
            select(EventHistory)
            .where(EventHistory.device_id == device_id)
            .order_by(EventHistory.timestamp.desc())  # newest first
            .limit(limit)
        )

        if from_time:
            query = query.where(EventHistory.timestamp >= from_time)
        if to_time:
            query = query.where(EventHistory.timestamp <= to_time)
        if event_name:
            query = query.where(EventHistory.event_name == event_name)

        result = await db.execute(query)
        return result.scalars().all()

    # ══════════════════════════════════════════════════════════════════════════
    # Private Helper
    # ══════════════════════════════════════════════════════════════════════════

    async def _get_current_event(
        self,
        db: AsyncSession,
        device_id: str,
        event_name: str,
    ) -> Optional[DeviceLastEvent]:
        """Fetch one specific event row for a device."""
        result = await db.execute(
            select(DeviceLastEvent).where(
                DeviceLastEvent.device_id == device_id,
                DeviceLastEvent.event_name == event_name,
            )
        )
        return result.scalar_one_or_none()


# ── Singleton ──────────────────────────────────────────────────────────────────
event_service = EventService()
