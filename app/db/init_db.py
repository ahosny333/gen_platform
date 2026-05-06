"""
app/db/init_db.py
──────────────────
Database Initialization & Seeding
═══════════════════════════════════════════════════════════════════════════════

Called ONCE at application startup (from main.py lifespan).

Does three things:
  1. CREATE TABLES — reads all ORM models and creates the tables in
     PostgreSQL if they don't exist yet. Safe to run multiple times
     (uses CREATE IF NOT EXISTS under the hood).

  2. Seeds  on first run:
    Users:   root, admin_01, user_01, user_02
    Devices: gen_01, gen_02, gen_03

  root sees ALL devices (bypasses junction table)
═══════════════════════════════════════════════════════════════════════════════
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import logger
from app.core.security import hash_password
from app.db.database import engine, Base, AsyncSessionLocal

# Import ALL models here so Base knows about them before create_all()
from app.models.user import User  # noqa: F401
from app.models.device import Device                # noqa: F401
from app.models.telemetry import TelemetryReading   # noqa: F401
from app.models.user_device import UserDevice       # noqa: F401  ← NEW
from app.models.device_event import DeviceLastEvent, EventHistory  # noqa: F401       # noqa: F401  ← NEW

settings = get_settings()


async def create_tables() -> None:
    """
    Create all database tables defined in ORM models.

    SQLAlchemy reads every class that inherits from Base
    and issues CREATE TABLE IF NOT EXISTS for each one.

    This replaces Django's `python manage.py migrate` for simple setups.
    (For production schema migrations, we'll add Alembic in a later step)
    """
    logger.info("[DB] Creating database tables...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("[DB] Tables created successfully")


async def seed_users() -> None:
    """
    Create the first admin user if no users exist in the database.

    This runs every startup but only inserts if the table is empty.
    Change the credentials below or move them to .env for production.
    """
    async with AsyncSessionLocal() as session:
        # Check if any user already exists
        result = await session.execute(select(User).limit(1))
        if result.scalar_one_or_none():
            logger.info("[DB] Users exist — skipping seed")
            return

        users = [
            User(id="root_01",   email="root@gmail.com",
                 hashed_password=hash_password("root123"),
                 full_name="Super Administrator", role="root",  is_active=True),
            User(id="admin_01",  email="admin@gmail.com",
                 hashed_password=hash_password("admin123"),
                 full_name="Platform Admin",      role="admin", is_active=True),
            User(id="user_01",   email="customer1@gmail.com",
                 hashed_password=hash_password("user123"),
                 full_name="Customer One",        role="user",  is_active=True),
            User(id="user_02",   email="customer2@gmail.com",
                 hashed_password=hash_password("user456"),
                 full_name="Customer Two",        role="user",  is_active=True),
        ]
        for u in users:
            session.add(u)
        await session.commit()

        logger.info("=" * 60)
        logger.info("[DB] Default users created:")
        logger.info("[DB]   root@gmail.com       / root123  (root)")
        logger.info("[DB]   admin@gmail.com      / admin123 (admin)")
        logger.info("[DB]   customer1@gmail.com  / user123  (user)")
        logger.info("[DB]   customer2@gmail.com  / user456  (user)")
        logger.info("[DB] Change all passwords before production!")
        logger.info("=" * 60)

async def seed_devices() -> None:
    """
    Create 2 demo generator devices if no devices exist yet.

    These match the device_ids used by the MQTT simulator
    so everything connects end-to-end out of the box.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Device).limit(1))
        existing = result.scalar_one_or_none()

        if existing:
            logger.info("[DB] Devices already exist — skipping device seed")
            return

        devices = [
            Device(id="gen_01", name="Generator 1 - Site A",
                   description="Main backup generator",
                   location="Building A - Basement", is_active=True),
            Device(id="gen_02", name="Generator 2 - Site B",
                   description="Secondary generator",
                   location="Building B - Rooftop",  is_active=True),
            Device(id="gen_03", name="Generator 3 - Site C",
                   description="Emergency unit",
                   location="Building C - Ground",   is_active=True),
        ]
        for d in devices:
            session.add(d)
        await session.commit()
        logger.info("[DB] Demo devices created: gen_01, gen_02, gen_03")

async def seed_assignments() -> None:
    """
    Seed the user_devices junction table.

    Assignment map:
      admin_01 → gen_01, gen_02          (sees 2 devices)
      user_01  → gen_01                  (sees 1 device)
      user_02  → gen_01, gen_03          (sees 2 devices, shares gen_01 with others)
      root_01  → NOT in table            (bypasses table, sees ALL)

    This demonstrates:
      - Same device (gen_01) accessible by multiple users
      - One user (admin_01) has multiple devices
      - root is not in this table at all
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(UserDevice).limit(1))
        if result.scalar_one_or_none():
            logger.info("[DB] Assignments exist — skipping seed")
            return

        assignments = [
            UserDevice(user_id="admin_01", device_id="gen_01", assigned_by="root_01"),
            UserDevice(user_id="admin_01", device_id="gen_02", assigned_by="root_01"),
            UserDevice(user_id="user_01",  device_id="gen_01", assigned_by="root_01"),
            UserDevice(user_id="user_02",  device_id="gen_01", assigned_by="root_01"),
            UserDevice(user_id="user_02",  device_id="gen_03", assigned_by="root_01"),
        ]
        for a in assignments:
            session.add(a)
        await session.commit()

        logger.info("[DB] Device assignments seeded:")
        logger.info("[DB]   admin_01 → gen_01, gen_02")
        logger.info("[DB]   user_01  → gen_01")
        logger.info("[DB]   user_02  → gen_01, gen_03")
        logger.info("[DB]   root_01  → ALL (bypasses table)")


async def init_db() -> None:
    """
    Master init function called from main.py on startup.
    Runs table creation then user seeding in order.
    """
    await create_tables()
    await seed_users()
    await seed_devices()
    await seed_assignments()