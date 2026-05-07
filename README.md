# Generator Monitoring IoT Platform — Backend

## Stack
| Layer | Technology |
|---|---|
| Web Framework | FastAPI + Uvicorn |
| MQTT Client | paho-mqtt (background thread) |
| Database | SQLite (demo) → TimescaleDB (production) |
| Auth | PyJWT + passlib |
| Config | pydantic-settings (.env) |

---

## Project Structure

```
generator_platform/
│
├── app/
│   ├── main.py                  ← FastAPI app + lifespan (startup/shutdown)
│   │
│   ├── core/
│   │   ├── config.py            ← Settings from .env (pydantic-settings)
│   │   ├── logging.py           ← Structured logger
│   │   └── state.py             ← Shared memory bridge (MQTT ↔ WebSocket)
│   │
│   ├── services/
│   │   └── mqtt_service.py      ← MQTT background thread + publish API
│   │
│   ├── api/
│   │   └── routes/              ← Route handlers (added in Steps 2–5)
│   │       ├── auth.py
│   │       ├── devices.py
│   │       ├── websocket.py
│   │       └── commands.py
│   │
│   ├── models/                  ← SQLAlchemy ORM models (Step 2+)
│   ├── schemas/                 ← Pydantic request/response schemas (Step 2+)
│   └── db/                      ← Database engine + session (Step 2+)
│
├── scripts/
│   └── mqtt_simulator.py        ← Simulates ESP32 without hardware
│
├── tests/                       ← Unit & integration tests
├── .env                         ← Environment variables (DO NOT COMMIT)
├── requirements.txt
└── run.sh
```

---

## Quick Start

### 1. Install dependencies
```bash
cd generator_platform
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Start MQTT broker (Mosquitto)
```bash
# Linux
sudo apt install mosquitto mosquitto-clients
sudo systemctl start mosquitto

# macOS
brew install mosquitto
brew services start mosquitto

# Docker (easiest)
docker run -it -p 1883:1883 eclipse-mosquitto
```

### 3. Configure environment
Edit `.env` — at minimum set `SECRET_KEY` and verify `MQTT_BROKER_HOST`.

### 4. Start the backend
```bash
# Development (auto-reload)
bash run.sh --dev

# Or directly:
uvicorn app.main:app --reload
```

### 5. Verify it's running
```bash
curl http://localhost:8000/health
```
Expected response:
```json
{
  "status": "ok",
  "version": "1.0.0",
  "mqtt_connected": true,
  "devices": {}
}
```

### 6. Simulate ESP32 data (no hardware needed)
```bash
python scripts/mqtt_simulator.py
```
Then check health again — you'll see devices appear in state.

---

## Data Flow (Step 1 scope)

```
ESP32 PLC / Simulator
        │
        │  MQTT  generator/gen_01/data
        ▼
  Mosquitto Broker
        │
        ▼
  MQTTService.on_message()          ← background thread
        │
        ▼
  SharedStateManager.update_from_mqtt()
        │  asyncio.Queue (thread-safe bridge)
        ▼
  WebSocket handlers                ← async (Step 4)
        │
        ▼
  React Frontend                    ← (Step 6)
```

---

## The Complete Data Pipeline with websocket push to frontend

```
ESP32
  │
  │ MQTT
  ▼
Mosquitto Broker
  │
  ▼
mqtt_service.on_message()          [sync thread]
  │  → parses topic
  │  → puts in correct shared_state queue
  ▼
data_router worker                 [async background task]
  │
  ├── 1. Save to database          [persistence]
  │       telemetry_readings
  │       device_last_events
  │       events_history
  │
  └── 2. Broadcast via ws_manager  [real-time push]  ← NEW
              │
              └── all WebSocket clients watching this device
                  receive the message instantly
```

## What's Next

| Step | Description |
|---|---|
| ✅ Step 1 | Project structure + MQTT bridge + shared state |
| Step 2 | Auth API — `/api/auth/login` + JWT |
| Step 3 | Device & History REST APIs |
| Step 4 | WebSocket live push endpoint `/ws/{device_id}` |
| Step 5 | Control API — REST → MQTT publish |
| Step 6 | React Frontend Dashboard |
