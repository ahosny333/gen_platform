"""
app/main.py
────────────
FastAPI Application Entry Point
═══════════════════════════════════════════════════════════════════════════════

This process handles ONLY:
  - REST API requests
  - WebSocket connections
  - Redis subscriber (receive from MQTT worker, push to WebSocket clients)

This process does NOT:
  - Connect to MQTT broker  (that's mqtt_worker.py)
  - Write telemetry to DB   (that's mqtt_worker.py)
  - Run the data router     (that's mqtt_worker.py)

Startup per API worker:
  1. Redis publisher  → for publishing commands (Step 5)
  2. Redis subscriber → receive device data → push to local WS clients

Run modes:
  Development:  uvicorn app.main:app --reload
  Production:   gunicorn app.main:app --workers 4 -k uvicorn.workers.UvicornWorker
                OR: bash run.sh --api

Complete system requires TWO terminal/service windows:
  Terminal 1: python -m app.mqtt_worker    ← handles MQTT + DB writes
  Terminal 2: bash run.sh --api            ← handles HTTP + WebSocket

Multi-worker production startup:
  gunicorn -k uvicorn.workers.UvicornWorker app.main:app --workers 4
  OR
  uvicorn app.main:app --workers 4

Route map:
  POST   /api/auth/login                        public
  GET    /api/devices/                          all authenticated users
  GET    /api/devices/{id}/history              all authenticated users
  GET    /api/devices/{id}/events               all authenticated users
  GET    /api/devices/{id}/events/history       all authenticated users
  POST   /api/devices/{id}/command              admin + root  (Step 5)
  GET    /api/users/                            root only
  POST   /api/admin/devices/                    root only
  WS     /ws/{device_id}?token=JWT              all authenticated users
═══════════════════════════════════════════════════════════════════════════════
"""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.logging import logger
#from app.core.state import shared_state
#from app.services.mqtt_service import mqtt_service
#from app.services.data_router import data_router
from app.services.ws_manager import ws_manager
from app.services.redis_manager import redis_manager
from app.services.redis_subscriber import redis_subscriber
from app.db.init_db import init_db
settings = get_settings()


# ══════════════════════════════════════════════════════════════════════════════
# Lifespan — Startup & Shutdown
# ══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manages startup and shutdown of background services.
    Code before `yield` runs on startup.
    Code after `yield` runs on shutdown.
    """
    pid = os.getpid()
    logger.info("=" * 60)
    logger.info(f"  {settings.app_name} — API Worker")
    logger.info(f"  Version: {settings.app_version}")
    logger.info(f"  PID: {pid}")
    logger.info("=" * 60)

    # ── 1. Redis Publisher ───────────────────────────────────────────────────
    # Used by:
    #   - redis_subscriber to publish commands (Step 5)
    #   - health check to verify Redis connectivity
    logger.info(f"[API] Connecting to Redis...")
    await redis_manager.connect()

    # ── 2. Redis Subscriber ──────────────────────────────────────────────────
    # Listens on Redis channels for device data published by mqtt_worker.
    # Delivers received messages to THIS worker's local WebSocket clients.
    logger.info(f"[API] Starting Redis subscriber...")
    await redis_subscriber.start()

    logger.info(f"[API] Worker PID={pid} ready")
    logger.info(f"[API] Docs:      http://localhost:8000/docs")
    logger.info(f"[API] WebSocket: ws://localhost:8000/ws/{{device_id}}?token=JWT")
    logger.info(f"[API] MQTT worker must also be running separately")

    yield

    logger.info(f"[API] Worker PID={pid} shutting down...")
    await redis_subscriber.stop()
    await redis_manager.disconnect()
    logger.info(f"[API] Worker PID={pid} shutdown complete")


# ══════════════════════════════════════════════════════════════════════════════
# FastAPI App Instance
# ══════════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "IoT Generator Monitoring Platform — "
        "Real-time monitoring, control, and analytics for industrial generators."
    ),
    docs_url="/docs",       # Swagger UI at /docs
    redoc_url="/redoc",     # ReDoc at /redoc
    lifespan=lifespan,
)


# ══════════════════════════════════════════════════════════════════════════════
# Middleware
# ══════════════════════════════════════════════════════════════════════════════

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ══════════════════════════════════════════════════════════════════════════════
# Routes
# ══════════════════════════════════════════════════════════════════════════════

# ── Health Check (no auth required) ──────────────────────────────────────────


@app.get("/health", tags=["System"], summary="Platform health check")
async def health_check():
    """
    Returns status of this API worker process.
    Note: mqtt_connected will always be False here (MQTT runs in mqtt_worker).
    Check mqtt_worker logs for MQTT connection status.
    """
    redis_info = await redis_manager.get_info()
    return JSONResponse({
        "status": "ok",
        "process": "api_worker",
        "version": settings.app_version,
        "pid": os.getpid(),
        "redis": redis_info,
        "websocket_local_connections": ws_manager.get_all_stats(),
    })


# ── Future route registrations (Steps 2–5) ───────────────────────────────────
from app.api.routes import auth, devices, users, admin, events, websocket

app.include_router(auth.router, prefix="/api/auth", tags=["Auth"])
app.include_router(devices.router, prefix="/api/devices", tags=["Devices"])  # ← NEW
app.include_router(events.router,  prefix="/api/devices", tags=["Events & Alarms"])
app.include_router(users.router,   prefix="/api/users",   tags=["User Management (Root)"])
app.include_router(admin.router,   prefix="/api/admin",   tags=["Admin Management (Root)"])
app.include_router(websocket.router, tags=["WebSocket"])     # ← NEW Step 4

# from app.api.routes import auth, devices, websocket, commands
# app.include_router(auth.router,     prefix="/api/auth",    tags=["Auth"])
# app.include_router(devices.router,  prefix="/api/devices", tags=["Devices"])
# app.include_router(websocket.router,                       tags=["WebSocket"])
# app.include_router(commands.router, prefix="/api/devices", tags=["Commands"])
