"""
app/api/routes/events.py
─────────────────────────
Event & Alarm REST API Routes
═══════════════════════════════════════════════════════════════════════════════

Endpoints:
  GET /api/devices/{device_id}/events
      → Current state of ALL events for this device
      → Returns device_last_events table rows
      → Used by dashboard to show alarm panel

  GET /api/devices/{device_id}/events/active
      → Only events currently TRUE (active alarms only)
      → Lightweight — used for alarm badge/indicator

  GET /api/devices/{device_id}/events/history
      → Historical event log from events_history table
      → Supports time range and event_name filters
      → Used for alarm history page
═══════════════════════════════════════════════════════════════════════════════
"""

from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_auth
from app.core.logging import logger
from app.db.database import get_db
from app.models.user import User
from app.services.event_service import event_service
from app.api.routes.devices import _get_device_or_404

router = APIRouter()


# ══════════════════════════════════════════════════════════════════════════════
# GET /api/devices/{device_id}/events  — All current event states
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/{device_id}/events",
    summary="Get current event states",
    description=(
        "Returns the current value of ALL events for this device. "
        "Each row is one event with its latest true/false value. "
        "New events from ESP32 appear automatically without any backend changes."
    ),
)
async def get_device_events(
    device_id: str,
    current_user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """
    Example response:
    {
        "device_id": "gen_01",
        "total": 3,
        "events": [
            {"event_name": "fuel_low",         "value": false, "last_updated": "..."},
            {"event_name": "high_temp",         "value": true,  "last_updated": "..."},
            {"event_name": "low_oil_pressure",  "value": false, "last_updated": "..."}
        ]
    }
    """
    # Verify device access (uses role-based check from devices.py)
    await _get_device_or_404(db, device_id, current_user)

    events = await event_service.get_device_events(db, device_id)

    return {
        "device_id": device_id,
        "total": len(events),
        "events": [
            {
                "event_name":   e.event_name,
                "value":        e.value,
                "last_updated": e.last_updated,
            }
            for e in events
        ],
    }


# ══════════════════════════════════════════════════════════════════════════════
# GET /api/devices/{device_id}/events/active  — Active alarms only
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/{device_id}/events/active",
    summary="Get active alarms only",
    description=(
        "Lightweight endpoint — returns only events currently TRUE. "
        "Use this for the alarm indicator badge on the dashboard."
    ),
)
async def get_active_alarms(
    device_id: str,
    current_user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """
    Example response (only true events):
    {
        "device_id": "gen_01",
        "active_alarm_count": 1,
        "active_alarms": [
            {"event_name": "high_temp", "value": true, "last_updated": "..."}
        ]
    }
    """
    await _get_device_or_404(db, device_id, current_user)

    active = await event_service.get_active_alarms(db, device_id)

    return {
        "device_id": device_id,
        "active_alarm_count": len(active),
        "active_alarms": [
            {
                "event_name":   e.event_name,
                "value":        e.value,
                "last_updated": e.last_updated,
            }
            for e in active
        ],
    }


# ══════════════════════════════════════════════════════════════════════════════
# GET /api/devices/{device_id}/events/history  — Event change log
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/{device_id}/events/history",
    summary="Get event history log",
    description=(
        "Returns the historical log of event value changes. "
        "Each row is one state change (alarm ON or alarm OFF). "
        "Filter by time range and/or specific event name."
    ),
)
async def get_event_history(
    device_id: str,
    from_time: datetime = Query(
        default=None, alias="from",
        description="Start time (ISO 8601). Defaults to 24 hours ago.",
        example="2026-04-13T00:00:00Z",
    ),
    to_time: datetime = Query(
        default=None, alias="to",
        description="End time (ISO 8601). Defaults to now.",
        example="2026-04-13T23:59:59Z",
    ),
    event_name: Optional[str] = Query(
        default=None,
        description="Filter to specific event. E.g. 'high_temp'",
        example="high_temp",
    ),
    limit: int = Query(
        default=500, ge=1, le=5000,
        description="Max records to return (default 500, max 5000).",
    ),
    current_user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """
    Example response:
    {
        "device_id": "gen_01",
        "from_time": "2026-04-13T00:00:00Z",
        "to_time":   "2026-04-13T23:59:59Z",
        "total": 3,
        "history": [
            {"id": 3, "event_name": "high_temp",  "value": false, "timestamp": "t3", "source": "update"},
            {"id": 2, "event_name": "fuel_low",   "value": true,  "timestamp": "t2", "source": "update"},
            {"id": 1, "event_name": "high_temp",  "value": true,  "timestamp": "t1", "source": "state"}
        ]
    }
    """
    await _get_device_or_404(db, device_id, current_user)

    # Default time range: last 24 hours
    now = datetime.now(timezone.utc)
    if not to_time:   to_time   = now
    if not from_time: from_time = now - timedelta(hours=24)

    logger.info(
        f"[Events] History query — device={device_id} "
        f"from={from_time} to={to_time} event={event_name}"
    )

    history = await event_service.get_event_history(
        db=db,
        device_id=device_id,
        from_time=from_time,
        to_time=to_time,
        event_name=event_name,
        limit=limit,
    )

    return {
        "device_id":  device_id,
        "from_time":  from_time,
        "to_time":    to_time,
        "total":      len(history),
        "history": [
            {
                "id":         h.id,
                "event_name": h.event_name,
                "value":      h.value,
                "timestamp":  h.timestamp,
                "source":     h.source,
            }
            for h in history
        ],
    }
