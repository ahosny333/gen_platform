"""
app/services/data_router.py
────────────────────────────
Background Data Router — Queue → Database
═══════════════════════════════════════════════════════════════════════════════

This is the MISSING BRIDGE that connects the MQTT pipeline to the database.

Full data flow after this fix:

  ESP32 PLC
     │
     │  MQTT publish  generator/gen_01/data
     ▼
  Mosquitto Broker
     │
     ▼
  mqtt_service.on_message()          [sync thread]
     │  → shared_state.update_from_mqtt()
     │  → puts payload into asyncio.Queue per device
     ▼
  data_router._process_queue()       [async background task]  ← THIS FILE
     │  → reads from asyncio.Queue
     │  → calls telemetry_service.save_reading()
     ▼
  Database (SQLite / PostgreSQL)
     │
     ▼
  REST API /history  serves data to frontend charts

Background Data Router — All Queues → Database
═══════════════════════════════════════════════════════════════════════════════

Updated to handle 3 queue types per device:

  telemetry queue      → telemetry_service.save_reading()
  event_state queue    → event_service.process_full_state()
  event_update queue   → event_service.process_single_update()

One supervisor spawns 3 workers per device (one per queue type).
Workers are completely independent — a slow DB write on one never
blocks the others.

Full data flow:

  ESP32
    ├── generator/{id}/data          → telemetry_queue  → telemetry_service → DB
    ├── generator/{id}/event/state   → event_state_queue → event_service → DB
    └── generator/{id}/event/update  → event_update_queue → event_service → DB


Why a separate background task instead of saving in on_message()?
──────────────────────────────────────────────────────────────────
  on_message() runs in the MQTT sync thread.
  Database operations in our app are ASYNC (asyncpg / aiosqlite).
  You cannot call async functions from a sync thread directly.
  Solution: on_message() puts data in a Queue (thread-safe),
  and this async background task reads from the Queue and does the DB work.
═══════════════════════════════════════════════════════════════════════════════
"""

import asyncio
from typing import Any, Dict

from app.core.logging import logger
from app.core.state import shared_state, DeviceState
from app.db.database import AsyncSessionLocal
from app.services.telemetry_service import telemetry_service
from app.services.event_service import event_service

class DataRouter:
    """
    Manages background asyncio tasks that drain the per-device queues
    and persist each telemetry reading to the database.

    One task runs per device — created dynamically as new devices
    appear in shared_state (first MQTT message from a new device_id).
    """

    def __init__(self) -> None:
        # Tracks which device_ids already have a running task
        # so we never start duplicates
        self._running_tasks: Dict[str, asyncio.Task] = {}
        self._running: bool = False
        self._supervisor_task: asyncio.Task = None

    # ══════════════════════════════════════════════════════════════════════════
    # Public API — called from main.py lifespan
    # ══════════════════════════════════════════════════════════════════════════

    async def start(self) -> None:
        """
        Start the supervisor loop.
        The supervisor watches for new devices and spawns a worker per device.
        Called once at application startup.
        """
        self._running = True
        self._supervisor_task = asyncio.create_task(
            self._supervisor_loop(),
            name="data_router_supervisor",
        )
        logger.info("[DataRouter] Started — supervising device queues")

    async def stop(self) -> None:
        """
        Cancel all running tasks gracefully.
        Called at application shutdown.
        """
        self._running = False

        # Cancel supervisor
        if self._supervisor_task:
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except asyncio.CancelledError:
                pass

        # Cancel all device workers
        for device_id, task in self._running_tasks.items():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            logger.debug(f"[DataRouter] Worker stopped for device: {device_id}")

        logger.info("[DataRouter] All tasks stopped cleanly")

    # ══════════════════════════════════════════════════════════════════════════
    # Supervisor Loop — watches for new devices
    # ══════════════════════════════════════════════════════════════════════════

    async def _supervisor_loop(self) -> None:
        """
        Runs every 2 seconds.
        Checks shared_state for any device_ids that don't have a
        worker task yet, and spawns one for each new device found.

        This means the system is fully dynamic:
          - First MQTT message from gen_01 → worker spawned for gen_01
          - First MQTT message from gen_03 → worker spawned for gen_03
          - No manual configuration needed when adding new generators
        """
        logger.debug("[DataRouter] Supervisor loop running")

        while self._running:
            try:
                for device_id in shared_state.list_device_ids():
                    # One task key per device+type combination
                    for worker_type in ("telemetry", "event_state", "event_update"):
                        task_key = f"{device_id}:{worker_type}"
                        if task_key not in self._running_tasks:
                            task = asyncio.create_task(
                                self._device_worker(device_id, worker_type),
                                name=f"router_{task_key}",
                            )
                            self._running_tasks[task_key] = task
                            logger.info(f"[DataRouter] Spawned {worker_type} worker for {device_id}")

                # Check every 2 seconds for new devices
                await asyncio.sleep(2)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[DataRouter] Supervisor error: {exc}")
                await asyncio.sleep(5)   # Wait before retrying

    # ══════════════════════════════════════════════════════════════════════════
    # Device Worker — one per device per queue type -- drains one device's queue into the database
    # ══════════════════════════════════════════════════════════════════════════

    async def _device_worker(self, device_id: str, worker_type: str) -> None:
        """
        Runs forever for one specific device.
        Waits for payloads in the device's asyncio.Queue,
        then saves each one to the database (Calls the correct service).

        Generic worker that drains one specific queue for one device.
        Calls the correct service based on worker_type.

        One worker per device means:
          - gen_01 slow DB write never blocks gen_02
          - Each device processed independently
          - Backpressure handled per-device
        """
        logger.info(f"[DataRouter] {worker_type} worker started for {device_id}")

        while self._running:
            try:
                state: DeviceState = shared_state.get_device(device_id)
                if not state:
                    await asyncio.sleep(1)
                    continue

                # Select which queue to drain based on worker type
                if worker_type == "telemetry":
                    queue = state.queue
                elif worker_type == "event_state":
                    queue = state.event_state_queue
                else:
                    queue = state.event_update_queue

                # Wait for next payload from the queue
                # timeout=5.0 means we check self._running every 5s
                # even if no data arrives (allows clean shutdown)
                try:
                    payload: Dict[str, Any] = await asyncio.wait_for(
                        queue.get(), timeout=5.0
                    )
                except asyncio.TimeoutError:
                    # No data in 5 seconds — loop back and check _running
                    continue

                # Route to correct service
                if worker_type == "telemetry":
                    await self._save_telemetry(device_id, payload)
                elif worker_type == "event_state":
                    await self._save_event_state(device_id, payload)
                else:
                    await self._save_event_update(device_id, payload)

                queue.task_done()

            except asyncio.CancelledError:
                logger.info(f"[DataRouter] Worker cancelled for: {device_id}")
                break
            except Exception as exc:
                logger.error(f"[DataRouter] {worker_type} worker error {device_id}: {exc}")
                # Brief pause before retrying to avoid tight error loop
                await asyncio.sleep(1)

    # ══════════════════════════════════════════════════════════════════════════
    # DB Persistence Methods
    # ══════════════════════════════════════════════════════════════════════════

    async def _save_telemetry(self, device_id: str, payload: Dict[str, Any]) -> None:
        """Save telemetry reading → telemetry_readings table."""
        try:
            async with AsyncSessionLocal() as session:
                await telemetry_service.save_reading(
                    db=session, device_id=device_id, payload=payload
                )
                await session.commit()
        except Exception as exc:
            logger.error(f"[DataRouter] Telemetry DB save failed {device_id}: {exc}")

    async def _save_event_state(self, device_id: str, payload: Dict[str, Any]) -> None:
        """
        Process full event state dump → device_last_events + events_history.

        Payload format: {"low_oil_pressure": false, "high_temp": true, ...}
        """
        try:
            async with AsyncSessionLocal() as session:
                await event_service.process_full_state(
                    db=session, device_id=device_id, payload=payload
                )
                await session.commit()
        except Exception as exc:
            logger.error(f"[DataRouter] Event state save failed {device_id}: {exc}")

    async def _save_event_update(self, device_id: str, payload: Dict[str, Any]) -> None:
        """
        Process single event update → device_last_events + events_history.

        Payload format: {"event": "fuel_low", "value": true}
        """
        try:
            event_name = payload.get("event")
            value = payload.get("value")

            if event_name is None or value is None:
                logger.warning(
                    f"[DataRouter] Invalid event/update payload for {device_id}: {payload}"
                )
                return

            if not isinstance(value, bool):
                logger.warning(
                    f"[DataRouter] event/update value must be bool, got {type(value)} — {device_id}"
                )
                return

            async with AsyncSessionLocal() as session:
                await event_service.process_single_update(
                    db=session,
                    device_id=device_id,
                    event_name=event_name,
                    new_value=value,
                )
                await session.commit()

        except Exception as exc:
            logger.error(f"[DataRouter] Event update save failed {device_id}: {exc}")



# ── Singleton ──────────────────────────────────────────────────────────────────
# Imported by main.py lifespan
data_router = DataRouter()
