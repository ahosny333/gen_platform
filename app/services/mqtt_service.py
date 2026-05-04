"""
app/services/mqtt_service.py
─────────────────────────────
MQTT Background Service
═══════════════════════════════════════════════════════════════════════════════

Responsibilities:
  1. Connect to the MQTT broker at startup
  2. Subscribe to  generator/+/data  (all device data topics)
  3. On each message → parse JSON → push to shared state
  4. Provide publish() method for the Control API to send commands to devices
  5. Handle reconnection automatically

Threading model:
  paho-mqtt's loop_start() spawns a dedicated daemon thread for the network
  loop. This thread calls on_message() synchronously. We never block this
  thread — heavy work (DB writes) happens in FastAPI's async context via
  the asyncio queue.

Topic convention (matches ESP32 firmware):
  ESP32  →  Backend :  generator/{device_id}/data
  Backend → ESP32  :  generator/{device_id}/command
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
        Initialize and connect the MQTT client.
        Called from FastAPI lifespan on startup.
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
        print("------kkkkkk-----")
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
        Called from FastAPI lifespan on shutdown.
        """
        logger.info("[MQTT] Stopping MQTT service...")
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
            logger.info("[MQTT] MQTT client disconnected")

    def publish(self, device_id: str, payload: Dict[str, Any]) -> bool:
        """
        Publish a command payload to a device.
        Called by the Control API route (REST → MQTT → Device).

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
        Runs in the MQTT background thread — MUST be fast and non-blocking.

        Flow:
          1. Extract device_id from topic  (generator/gen_01/data → gen_01)
          2. Parse JSON payload
          3. Push to shared_state (thread-safe asyncio.Queue)
          4. Return immediately
        """
        topic: str = msg.topic

        # ── Extract device_id from topic ──────────────────────────────────────
        # Topic pattern: generator/{device_id}/data
        parts = topic.split("/")
        if len(parts) != 3:
            logger.warning(f"[MQTT] Unexpected topic format: {topic}")
            return

        device_id = parts[1]

        # ── Parse JSON ────────────────────────────────────────────────────────
        try:
            raw = msg.payload.decode("utf-8")
            payload: Dict[str, Any] = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.error(
                f"[MQTT] Failed to parse message from {topic}: {exc} "
                f"| raw={msg.payload[:200]}"
            )
            return

        logger.debug(
            f"[MQTT] ← {topic} | status={payload.get('status')} "
            f"| rpm={payload.get('rpm')} | ts={payload.get('timestamp')}"
        )

        # ── Push to shared state (bridges sync MQTT → async WebSocket) ────────
        shared_state.update_from_mqtt(device_id, payload)

    # ══════════════════════════════════════════════════════════════════════════
    # Private Helpers
    # ══════════════════════════════════════════════════════════════════════════

    def _subscribe_all(self) -> None:
        """Subscribe to all relevant topics after connect/reconnect."""
        topics = [
            (settings.mqtt_data_topic, 0),   # QoS 0 for telemetry (high freq)
        ]
        self._client.subscribe(topics)
        logger.info(f"[MQTT] Subscribed to topics: {[t[0] for t in topics]}")


# ── Singleton ──────────────────────────────────────────────────────────────────
# Imported by main.py (lifespan) and Control API route
mqtt_service = MQTTService()
