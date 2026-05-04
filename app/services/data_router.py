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
                known_devices = shared_state.list_device_ids()

                for device_id in known_devices:
                    if device_id not in self._running_tasks:
                        # New device seen for the first time — spawn a worker
                        task = asyncio.create_task(
                            self._device_worker(device_id),
                            name=f"data_router_{device_id}",
                        )
                        self._running_tasks[device_id] = task
                        logger.info(
                            f"[DataRouter] Spawned worker for new device: {device_id}"
                        )

                # Check every 2 seconds for new devices
                await asyncio.sleep(2)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[DataRouter] Supervisor error: {exc}")
                await asyncio.sleep(5)   # Wait before retrying

    # ══════════════════════════════════════════════════════════════════════════
    # Device Worker — drains one device's queue into the database
    # ══════════════════════════════════════════════════════════════════════════

    async def _device_worker(self, device_id: str) -> None:
        """
        Runs forever for one specific device.
        Waits for payloads in the device's asyncio.Queue,
        then saves each one to the database.

        One worker per device means:
          - gen_01 slow DB write never blocks gen_02
          - Each device processed independently
          - Backpressure handled per-device
        """
        logger.info(f"[DataRouter] Worker started for device: {device_id}")

        while self._running:
            try:
                device_state: DeviceState = shared_state.get_device(device_id)

                if not device_state:
                    # Device disappeared from state (shouldn't happen, but safe)
                    await asyncio.sleep(1)
                    continue

                # Wait for next payload from the queue
                # timeout=5.0 means we check self._running every 5s
                # even if no data arrives (allows clean shutdown)
                try:
                    payload: Dict[str, Any] = await asyncio.wait_for(
                        device_state.queue.get(),
                        timeout=5.0,
                    )
                except asyncio.TimeoutError:
                    # No data in 5 seconds — loop back and check _running
                    continue

                # Save to database
                await self._save_to_db(device_id, payload)

                # Mark queue task as done
                device_state.queue.task_done()

            except asyncio.CancelledError:
                logger.info(f"[DataRouter] Worker cancelled for: {device_id}")
                break
            except Exception as exc:
                logger.error(
                    f"[DataRouter] Worker error for {device_id}: {exc}"
                )
                # Brief pause before retrying to avoid tight error loop
                await asyncio.sleep(1)

    # ══════════════════════════════════════════════════════════════════════════
    # Database Persistence
    # ══════════════════════════════════════════════════════════════════════════

    async def _save_to_db(
        self,
        device_id: str,
        payload: Dict[str, Any],
    ) -> None:
        """
        Open a database session and save the telemetry reading.

        We open a NEW session per save (not reusing one session forever)
        because long-lived sessions cause stale connection issues.
        AsyncSessionLocal() is cheap — it borrows from the connection pool.
        """
        try:
            async with AsyncSessionLocal() as session:
                reading = await telemetry_service.save_reading(
                    db=session,
                    device_id=device_id,
                    payload=payload,
                )
                await session.commit()

                logger.debug(
                    f"[DataRouter] Saved to DB — device={device_id} "
                    f"status={payload.get('status')} "
                    f"ts={payload.get('timestamp')}"
                )

        except Exception as exc:
            logger.error(
                f"[DataRouter] DB save failed for {device_id}: {exc} "
                f"| payload keys: {list(payload.keys())}"
            )
            # We don't re-raise — a failed save should not crash the worker
            # The reading is lost but the worker continues for future readings


# ── Singleton ──────────────────────────────────────────────────────────────────
# Imported by main.py lifespan
data_router = DataRouter()
