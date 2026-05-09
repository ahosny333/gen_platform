"""
app/services/mqtt_service.py
─────────────────────────────
MQTT Service — Runs ONLY in mqtt_worker.py
═══════════════════════════════════════════════════════════════════════════════

IMPORTANT: This service now runs in ONLY ONE process (mqtt_worker.py).
           No PID suffix on client_id — clean single connection.

Two responsibilities:
  1. SUBSCRIBE — receive data from ESP32 devices, push to shared_state queues
  2. PUBLISH   — send commands to devices (called via Redis from API workers)

Topics subscribed (receiving from ESP32):
  generator/+/data           → telemetry readings
  generator/+/event/state    → full event state dump
  generator/+/event/update   → single event change

    on_message() routes each message to the correct shared_state method
    based on the topic pattern it matched.
  3. On each message → parse JSON → push to shared state
  4. Provide publish() method for the Control API to send commands to devices
  5. Handle reconnection automatically

Threading model:
  paho-mqtt's loop_start() spawns a dedicated daemon thread for the network
  loop. This thread calls on_message() synchronously. We never block this
  thread — heavy work (DB writes) happens in FastAPI's async context via
  the asyncio queue.

Topic published (sending commands to ESP32):
  generator/{device_id}/command

Command flow for multi-process architecture:
  API Worker receives POST /api/devices/{id}/command
        ↓
  API Worker publishes command to Redis channel "cmd:{device_id}"
        ↓
  mqtt_worker's CommandListener receives from Redis
        ↓
  mqtt_service.publish() sends to ESP32 via MQTT
        ↓
  ESP32 executes command
═══════════════════════════════════════════════════════════════════════════════
"""

import json
import time
import threading
from typing import Any, Dict

import paho.mqtt.client as mqtt

from app.core.config import get_settings
from app.core.logging import logger
from app.core.state import shared_state

settings = get_settings()


class MQTTService:
    """
    Manages the lifecycle of the paho-mqtt client as a background service.
    Instantiated once at application startup, torn down at shutdown.
    """

    def __init__(self) -> None:
        self._client: mqtt.Client = None
        self._connected: bool = False
        self._lock = threading.Lock()

    # ══════════════════════════════════════════════════════════════════════════
    # Public API
    # ══════════════════════════════════════════════════════════════════════════

    def start(self) -> None:
        """
        Connect to MQTT broker with a single clean client ID.
        No PID suffix needed — only ONE process runs this.
        """
        logger.info("[MQTT] Starting MQTT service...")
        self._client = mqtt.Client(
            client_id=settings.mqtt_client_id,
            clean_session=True,
            protocol=mqtt.MQTTv311,
        )

        # Attach callbacks
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.on_subscribe = self._on_subscribe

        # Authentication (if broker requires it)
        if settings.mqtt_username:
            self._client.username_pw_set(
                settings.mqtt_username,
                settings.mqtt_password,
            )

        # Reconnection: paho will retry automatically with this config
        self._client.reconnect_delay_set(min_delay=2, max_delay=30)

        # Connect (non-blocking)
        print(f"Connecting to MQTT broker at {settings.mqtt_broker_host}:{settings.mqtt_broker_port}...")
        try:
            self._client.connect(
                host=settings.mqtt_broker_host,
                port=settings.mqtt_broker_port,
                keepalive=settings.mqtt_keepalive,
            )
        except Exception as exc:
            logger.error(f"[MQTT] Initial connection failed: {exc}")
            logger.warning("[MQTT] Will retry automatically via reconnect loop")

        # Start the network loop in its own daemon thread
        self._client.loop_start()
        logger.info("[MQTT] Network loop started in background thread")

    def stop(self) -> None:
        """
        Gracefully disconnect and stop the MQTT loop.
        """
        logger.info("[MQTT] Stopping MQTT service...")
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
            logger.info("[MQTT] MQTT client disconnected")

    def publish(self, device_id: str, payload: Dict[str, Any]) -> bool:
        """
        Publish a command payload to a device.
        Called by CommandListener in mqtt_worker.py after receiving
        a command request from an API worker via Redis.

        Args:
            device_id: Target device identifier
            payload:   Command dict, e.g. {"cmd": "start"}

        Returns:
            True if published successfully, False otherwise
        """
        if not self._connected:
            logger.error(f"[MQTT] Cannot publish — not connected to broker")
            return False

        topic = settings.get_command_topic(device_id)
        message = json.dumps(payload)

        with self._lock:
            result = self._client.publish(
                topic=topic,
                payload=message,
                qos=1,          # At least once delivery for commands
                retain=False,
            )

        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            logger.info(f"[MQTT] Published to {topic}: {message}")
            return True
        else:
            logger.error(
                f"[MQTT] Publish failed to {topic} "
                f"(rc={result.rc}: {mqtt.error_string(result.rc)})"
            )
            return False

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ══════════════════════════════════════════════════════════════════════════
    # paho Callbacks (run in the MQTT background thread)
    # ══════════════════════════════════════════════════════════════════════════

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: dict,
        rc: int,
    ) -> None:
        """
        Fired when the client connects (or reconnects) to the broker.
        rc=0 means success. Any other value indicates a failure.
        """
        if rc == 0:
            self._connected = True
            logger.info(
                f"[MQTT] Connected to broker at "
                f"{settings.mqtt_broker_host}:{settings.mqtt_broker_port}"
            )
            # Re-subscribe on every connect (handles reconnects transparently)
            self._subscribe_all()
        else:
            self._connected = False
            reason = {
                1: "Incorrect protocol version",
                2: "Invalid client identifier",
                3: "Server unavailable",
                4: "Bad username or password",
                5: "Not authorized",
            }.get(rc, f"Unknown error (rc={rc})")
            logger.error(f"[MQTT] Connection refused: {reason}")

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        rc: int,
    ) -> None:
        """
        Fired on disconnect. rc=0 is a clean disconnect (we called stop()).
        Any other value is unexpected — paho will attempt to reconnect.
        """
        self._connected = False
        if rc == 0:
            logger.info("[MQTT] Disconnected cleanly")
        else:
            logger.warning(
                f"[MQTT] Unexpected disconnect (rc={rc}). "
                "Reconnect will be attempted automatically..."
            )

    def _on_subscribe(
        self,
        client: mqtt.Client,
        userdata: Any,
        mid: int,
        granted_qos: tuple,
    ) -> None:
        logger.info(
            f"[MQTT] Subscription confirmed — "
            f"topic: {settings.mqtt_data_topic} | qos: {granted_qos}"
        )

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: Any,
        msg: mqtt.MQTTMessage,
    ) -> None:
        """
        Fired for every incoming MQTT message.
        Route incoming MQTT message to correct shared_state queue.
        Runs in the MQTT background thread — MUST be fast and non-blocking.

        Flow:
          1. Extract device_id from topic  ex: (generator/gen_01/data → gen_01)
          2. Parse JSON payload
          3. Push to shared_state (thread-safe asyncio.Queue)
            Topic patterns and routing:
            generator/{id}/data           → shared_state.update_from_mqtt()
            generator/{id}/event/state    → shared_state.update_event_state()
            generator/{id}/event/update   → shared_state.update_single_event()
          4. Return immediately
        """
        topic: str = msg.topic

        # ── Extract device_id from topic ──────────────────────────────────────
        # Topic pattern: generator/{device_id}/data
        parts = topic.split("/")
        # ── Parse JSON payload ─────────────────────────────────────────────────
        try:
            raw = msg.payload.decode("utf-8")
            payload: Dict[str, Any] = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.error(f"[MQTT] Failed to parse {topic}: {exc}")
            return

        # ── Route by topic pattern ─────────────────────────────────────────────

        # Pattern: generator/{device_id}/data  (3 parts)
        if len(parts) == 3 and parts[0] == "generator" and parts[2] == "data":
            device_id = parts[1]
            logger.debug(f"[MQTT] ← telemetry {device_id} status={payload.get('status')}")
            shared_state.update_from_mqtt(device_id, payload)

        # Pattern: generator/{device_id}/event/state  (4 parts)
        elif (len(parts) == 4 and parts[0] == "generator"
              and parts[2] == "event" and parts[3] == "state"):
            device_id = parts[1]
            logger.debug(f"[MQTT] ← event/state {device_id} events={list(payload.keys())}")
            shared_state.update_event_state(device_id, payload)

        # Pattern: generator/{device_id}/event/update  (4 parts)
        elif (len(parts) == 4 and parts[0] == "generator"
              and parts[2] == "event" and parts[3] == "update"):
            device_id = parts[1]
            logger.debug(
                f"[MQTT] ← event/update {device_id} "
                f"event={payload.get('event')} value={payload.get('value')}"
            )
            shared_state.update_single_event(device_id, payload)

        else:
            logger.warning(f"[MQTT] Unrecognized topic pattern: {topic}")
       

    # ══════════════════════════════════════════════════════════════════════════
    # Private Helpers
    # ══════════════════════════════════════════════════════════════════════════

    def _subscribe_all(self) -> None:
        """Subscribe to all 3 topic patterns after connect/reconnect."""
        topics = [
            ("generator/+/data",         0),  # QoS 0 — high frequency telemetry
            ("generator/+/event/state",  1),  # QoS 1 — periodic full state dump
            ("generator/+/event/update", 1),  # QoS 1 — spontaneous alarm changes
        ]
        self._client.subscribe(topics)
        logger.info(f"[MQTT] Subscribed to: {[t[0] for t in topics]}")

# ── Singleton ──────────────────────────────────────────────────────────────────
# Imported by main.py (lifespan) and Control API route
mqtt_service = MQTTService()
