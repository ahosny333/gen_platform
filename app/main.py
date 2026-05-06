"""
app/main.py
────────────
FastAPI Application Entry Point
═══════════════════════════════════════════════════════════════════════════════

Startup sequence:
  1. FastAPI app is created with lifespan context manager
  2. On startup:  MQTT service connects + background loop starts
  3. Data router starts (queue → database background task)
  4. Routes are registered (auth, devices, websocket, health)
  5. Uvicorn serves the app
  6. On shutdown: MQTT service disconnects cleanly

The lifespan pattern (replacing deprecated @app.on_event) is the
FastAPI-recommended approach for managing background services.

Role-based route access summary:
  /api/auth/       → public (no auth)
  /api/devices/    → all authenticated users (filtered by role)
  /api/users/      → root only
  /api/admin/      → root only
  /api/devices/{id}/command → admin + root only (Step 5)
═══════════════════════════════════════════════════════════════════════════════
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.logging import logger
from app.core.state import shared_state
from app.services.mqtt_service import mqtt_service
from app.services.data_router import data_router
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
    # ── Startup ───────────────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info(f"  {settings.app_name} v{settings.app_version}")
    logger.info(f"  Debug mode: {settings.debug}")
    logger.info("=" * 60)

    # Initialize database — create tables + seed admin user
    await init_db()                                   # ← NEW
    
    # Start MQTT service (connects broker, starts background thread)
    mqtt_service.start()

    # 3. Start data router — async background task that reads from
    #    shared_state queues and saves each reading to the database
    #    THIS IS THE FIX — connects MQTT pipeline to the database
    await data_router.start()                                 

    logger.info("[App] Application startup complete. Ready to serve requests.")

    yield  # ← Application runs here

    # ── Shutdown ──────────────────────────────────────────────────────────────
    logger.info("[App] Shutting down...")
    # Stop data router first — let it finish any in-progress DB writes
    await data_router.stop()   
    # Then stop MQTT — no new data after this point
    mqtt_service.stop()
    logger.info("[App] Shutdown complete.")


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
    Returns platform status including:
    - MQTT broker connection status
    - Active device count and their live data state
    - WebSocket client count per device
    """
    return JSONResponse({
        "status": "ok",
        "version": settings.app_version,
        "mqtt_connected": mqtt_service.is_connected,
        "data_router_running": data_router._running,
        "devices": shared_state.summary(),
    })


# ── Future route registrations (Steps 2–5) ───────────────────────────────────
from app.api.routes import auth, devices, users, admin, events

app.include_router(auth.router, prefix="/api/auth", tags=["Auth"])
app.include_router(devices.router, prefix="/api/devices", tags=["Devices"])  # ← NEW
app.include_router(events.router,  prefix="/api/devices", tags=["Events & Alarms"])
app.include_router(users.router,   prefix="/api/users",   tags=["User Management (Root)"])
app.include_router(admin.router,   prefix="/api/admin",   tags=["Admin Management (Root)"])

# from app.api.routes import auth, devices, websocket, commands
# app.include_router(auth.router,     prefix="/api/auth",    tags=["Auth"])
# app.include_router(devices.router,  prefix="/api/devices", tags=["Devices"])
# app.include_router(websocket.router,                       tags=["WebSocket"])
# app.include_router(commands.router, prefix="/api/devices", tags=["Commands"])
