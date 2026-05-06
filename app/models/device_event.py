"""
app/models/device_event.py
───────────────────────────
Event & Alarm Database Models
═══════════════════════════════════════════════════════════════════════════════

Two tables:

  1. device_last_events
     ──────────────────
     Stores the CURRENT state of every event for every device.
     Acts like a live status board — always shows latest values.
     One row per (device_id + event_name) combination.

     device_id | event_name       | value | last_updated
     ----------|------------------|-------|-------------
     gen_01    | low_oil_pressure | False | t1
     gen_01    | high_temp        | True  | t1
     gen_01    | fuel_low         | False | t2
     gen_02    | high_temp        | False | t1

     Key property: event_name is a STRING column — not a fixed set of columns.
     New events from ESP32 just become new rows. Zero schema changes ever.

  2. events_history
     ───────────────
     Append-only log of every event VALUE CHANGE.
     Never updated — only INSERTed into.
     Used for alarm history, audit trail, and trend analysis.

     id | device_id | event_name       | value | timestamp
     ---|-----------|------------------|-------|----------
     1  | gen_01    | high_temp        | True  | t1  ← alarm ON
     2  | gen_01    | fuel_low         | True  | t2  ← alarm ON
     3  | gen_01    | high_temp        | False | t3  ← alarm cleared
═══════════════════════════════════════════════════════════════════════════════
"""

from sqlalchemy import (
    Boolean, Column, String, DateTime, Integer,
    ForeignKey, UniqueConstraint, Index,
)
from sqlalchemy.sql import func

from app.db.database import Base


# ══════════════════════════════════════════════════════════════════════════════
# Table 1: device_last_events — current state snapshot
# ══════════════════════════════════════════════════════════════════════════════

class DeviceLastEvent(Base):
    """
    Stores the latest value of each event per device.
    UPSERTED on every incoming event message — never grows unboundedly.

    One row = one unique (device_id, event_name) pair.
    """

    __tablename__ = "device_last_events"

    # ── Primary Key: composite (device_id + event_name) ───────────────────────
    # This naturally enforces: only ONE row per device per event name.
    # If gen_01/high_temp already exists → UPDATE it, don't insert a duplicate.

    device_id = Column(
        String,
        ForeignKey("devices.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
        # CASCADE: device deleted → all its event rows auto-removed
    )

    event_name = Column(
        String,
        primary_key=True,
        nullable=False,
        # Examples: "low_oil_pressure", "high_temp", "fuel_low"
        # Any string — fully dynamic, new events just become new rows
    )

    # ── Current Value ──────────────────────────────────────────────────────────
    value = Column(
        Boolean,
        nullable=False,
        # True  = alarm/event is ACTIVE
        # False = alarm/event is CLEARED / normal
    )

    # ── Timestamp ──────────────────────────────────────────────────────────────
    last_updated = Column(
        DateTime(timezone=True),
        nullable=False,
        # When this event's value was last received from ESP32
        # Updated every time a new message arrives (even if value unchanged)
    )

    # ── Explicit unique constraint (documents intent) ─────────────────────────
    __table_args__ = (
        UniqueConstraint("device_id", "event_name", name="uq_device_event"),
    )

    def __repr__(self) -> str:
        return (
            f"<DeviceLastEvent device={self.device_id} "
            f"event={self.event_name} value={self.value}>"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Table 2: events_history — append-only event change log
# ══════════════════════════════════════════════════════════════════════════════

class EventHistory(Base):
    """
    Append-only log of event value changes.
    A new row is inserted every time an event changes value.

    Written when:
      - ESP32 sends event/update topic (spontaneous change) → ALWAYS log
      - ESP32 sends event/state topic (full dump)           → log only if
                                                              value changed
    """

    __tablename__ = "events_history"

    # ── Auto-increment ID ──────────────────────────────────────────────────────
    id = Column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    # ── What happened ──────────────────────────────────────────────────────────
    device_id = Column(
        String,
        ForeignKey("devices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        # Indexed for fast history queries by device
    )

    event_name = Column(
        String,
        nullable=False,
        # Same flexible string approach — no hardcoded alarm types
    )

    value = Column(
        Boolean,
        nullable=False,
        # The NEW value at the time of this change
        # True  = event became active (alarm triggered)
        # False = event became cleared (alarm resolved)
    )

    # ── When it happened ───────────────────────────────────────────────────────
    timestamp = Column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
        # Indexed for fast time-range queries
    )

    # ── Source of the log entry ────────────────────────────────────────────────
    source = Column(
        String,
        nullable=True,
        default="update",
        # "update" = came from event/update topic (spontaneous change)
        # "state"  = came from event/state topic (full dump, value changed)
    )

    # ── Composite index for the most common query ──────────────────────────────
    # "give me all events for gen_01 between t1 and t2"
    __table_args__ = (
        Index("ix_events_history_device_time", "device_id", "timestamp"),
    )

    def __repr__(self) -> str:
        return (
            f"<EventHistory id={self.id} device={self.device_id} "
            f"event={self.event_name} value={self.value} ts={self.timestamp}>"
        )
