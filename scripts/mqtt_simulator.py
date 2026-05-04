"""
scripts/mqtt_simulator.py
──────────────────────────
ESP32 Data Simulator for Local Development
═══════════════════════════════════════════════════════════════════════════════

Simulates 2 generators publishing realistic telemetry to the MQTT broker
every 5 seconds. Use this to test the full data pipeline without hardware.

Usage:
    python scripts/mqtt_simulator.py

    # Simulate specific device only:
    python scripts/mqtt_simulator.py --device gen_01

    # Custom broker:
    python scripts/mqtt_simulator.py --host 192.168.1.100 --port 1883
═══════════════════════════════════════════════════════════════════════════════
"""

import json
import time
import math
import random
import argparse
from datetime import datetime, timezone

import paho.mqtt.client as mqtt


# ── Simulator Configuration ────────────────────────────────────────────────────

DEVICES = [
    {"device_id": "gen_01", "name": "Generator 1 - Site A"},
    {"device_id": "gen_02", "name": "Generator 2 - Site B"},
]

PUBLISH_INTERVAL = 5   # seconds between each message


# ── Realistic Data Generation ──────────────────────────────────────────────────

def generate_telemetry(device_id: str, tick: int) -> dict:
    """
    Generate realistic-looking telemetry that slowly oscillates over time.
    Uses sine waves with different frequencies to mimic real sensor noise.
    """
    t = tick * 0.1

    # Simulate occasional Modbus fail (status=0) every ~50 messages
    if tick % 50 == 0 and tick > 0:
        print(f"  [{device_id}] Simulating Modbus FAIL (status=0)")
        return {
            "device_id": device_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": 0,
        }

    # Normal running data
    rpm_base = 1500 if device_id == "gen_01" else 1800
    return {
        "device_id": device_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": 1,       # 1=running, 2=alarm, 0=modbus fail
        "mode": 2,         # 1=auto, 2=manual, 3=stop

        # Mechanical
        "rpm":    round(rpm_base + 50 * math.sin(t) + random.uniform(-5, 5)),
        "oil_p":  round(5000 + 200 * math.sin(t * 0.3) + random.uniform(-50, 50)),
        "cool_t": round(85 + 5 * math.sin(t * 0.2) + random.uniform(-1, 1), 1),
        "oil_t":  round(90 + 3 * math.sin(t * 0.15) + random.uniform(-1, 1), 1),
        "fuel_l": round(max(10, 80 - tick * 0.05 + random.uniform(-0.5, 0.5)), 1),

        # Electrical
        "f":      round(50 + 0.2 * math.sin(t * 2) + random.uniform(-0.05, 0.05), 2),
        "v": [
            round(220 + 2 * math.sin(t) + random.uniform(-1, 1), 1),        # L1-N
            round(220 + 2 * math.sin(t + 2.09) + random.uniform(-1, 1), 1), # L2-N
            round(220 + 2 * math.sin(t + 4.19) + random.uniform(-1, 1), 1), # L3-N
            round(380 + 3 * math.sin(t) + random.uniform(-1, 1), 1),        # L1-L2
            round(380 + 3 * math.sin(t + 2.09) + random.uniform(-1, 1), 1), # L2-L3
            round(380 + 3 * math.sin(t + 4.19) + random.uniform(-1, 1), 1), # L3-L1
        ],
        "a": [
            round(100 + 5 * math.sin(t * 1.1) + random.uniform(-2, 2), 1),  # L1
            round(102 + 5 * math.sin(t * 1.2) + random.uniform(-2, 2), 1),  # L2
            round(98  + 5 * math.sin(t * 1.3) + random.uniform(-2, 2), 1),  # L3
            round(0.5 + random.uniform(-0.1, 0.1), 2),                       # N
        ],
        "w": [
            round(21000 + 1000 * math.sin(t) + random.uniform(-100, 100)),   # P1
            round(21500 + 1000 * math.sin(t + 1) + random.uniform(-100, 100)),# P2
            round(20800 + 1000 * math.sin(t + 2) + random.uniform(-100, 100)),# P3
        ],

        # System
        "bat_v": round(24 + 0.5 * math.sin(t * 0.05) + random.uniform(-0.1, 0.1), 2),
        "cgh_v": round(27 + 0.3 * math.sin(t * 0.05) + random.uniform(-0.1, 0.1), 2),
    }


# ── MQTT Callbacks ─────────────────────────────────────────────────────────────

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"✅ Connected to MQTT broker")
    else:
        print(f"❌ Connection failed (rc={rc})")


def on_publish(client, userdata, mid):
    pass  # Silently confirm publish


# ── Main Loop ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="ESP32 MQTT Simulator")
    parser.add_argument("--host",   default="localhost", help="MQTT broker host")
    parser.add_argument("--port",   default=1883,        type=int)
    parser.add_argument("--device", default=None,        help="Simulate single device")
    args = parser.parse_args()

    devices = DEVICES
    if args.device:
        devices = [d for d in DEVICES if d["device_id"] == args.device]
        if not devices:
            print(f"❌ Unknown device: {args.device}")
            return

    # Set up MQTT client
    client = mqtt.Client(client_id="esp32_simulator", clean_session=True)
    client.on_connect = on_connect
    client.on_publish = on_publish
    client.connect(args.host, args.port, keepalive=60)
    client.loop_start()

    time.sleep(1)  # Wait for connection

    print(f"\n🚀 Simulating {len(devices)} device(s) — publishing every {PUBLISH_INTERVAL}s")
    print(f"   Broker: {args.host}:{args.port}")
    print(f"   Topic:  generator/{{device_id}}/data")
    print("   Press Ctrl+C to stop\n")

    tick = 0
    try:
        while True:
            for device in devices:
                device_id = device["device_id"]
                topic = f"generator/{device_id}/data"
                payload = generate_telemetry(device_id, tick)
                message = json.dumps(payload)

                result = client.publish(topic, message, qos=0)
                status = payload.get("status")
                rpm    = payload.get("rpm", "—")
                fuel   = payload.get("fuel_l", "—")
                print(
                    f"  [{device_id}] tick={tick:04d} | "
                    f"status={status} | rpm={rpm} | fuel={fuel}%"
                )

            tick += 1
            time.sleep(PUBLISH_INTERVAL)

    except KeyboardInterrupt:
        print("\n\n🛑 Simulator stopped.")
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
