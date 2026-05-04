"""
app/db/database.py
───────────────────
Database Engine & Session Management
═══════════════════════════════════════════════════════════════════════════════

This file sets up the SQLAlchemy async engine.
Think of it as the "database connection manager" for the entire app.

Two things are created here:
  1. engine       — the single connection pool to PostgreSQL
  2. AsyncSession — a factory that creates database sessions per request

How a session works (same concept as Flask-SQLAlchemy):
  - A session is like a "conversation" with the database
  - You open it at the start of a request
  - Do your queries inside it
  - Commit (save) or rollback (undo) at the end
  - Close it — connection returns to the pool
═══════════════════════════════════════════════════════════════════════════════
"""

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings
from app.core.logging import logger

settings = get_settings()

# ── Engine ─────────────────────────────────────────────────────────────────────
# The engine manages the connection pool to PostgreSQL.
# pool_pre_ping=True: tests connections before using them (handles DB restarts)
# echo=True in debug: prints every SQL query to console (helpful for dev)
# engine = create_async_engine(
#     url=settings.database_url,
#     echo=settings.debug,         # Set to False in production
#     pool_pre_ping=True,
#     pool_size=10,                # Max 10 simultaneous DB connections
#     max_overflow=20,             # Allow 20 extra connections under heavy load
# )
engine = create_async_engine(
    url=settings.database_url,
    echo=settings.debug,         # Set to False in production
    pool_pre_ping=True,
)

# ── Session Factory ────────────────────────────────────────────────────────────
# AsyncSessionLocal is a factory — calling it gives you a new session object.
# expire_on_commit=False: keep objects usable after commit (important for async)
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# ── Base Class for All ORM Models ──────────────────────────────────────────────
# All your model classes (User, Device, TelemetryReading) will inherit from this.
# SQLAlchemy uses it to track which classes = which tables.
class Base(DeclarativeBase):
    pass


# ── Dependency: Get DB Session (injected into route handlers) ─────────────────
async def get_db() -> AsyncSession:
    """
    FastAPI dependency that provides a database session to route handlers.

    Usage in a route:
        @router.get("/something")
        async def my_route(db: AsyncSession = Depends(get_db)):
            result = await db.execute(...)

    This is equivalent to Flask-SQLAlchemy's `db.session` but:
      - Opens a fresh session per request
      - Automatically commits on success
      - Automatically rolls back on any exception
      - Always closes the session when request is done

    The `async with` + `yield` pattern is FastAPI's way of doing
    what Flask does with @app.teardown_appcontext.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception as exc:
            await session.rollback()
            logger.error(f"[DB] Session error, rolling back: {exc}")
            raise
