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

  2. Seeds 3 default accounts on first run:
    root  → root@generator.local  / root123   (super admin)
    admin → admin@generator.local / admin123  (internal user)
    user  → user@generator.local  / user123   (demo customer)

  3. Also seeds 2 demo devices assigned to demo customer.
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
        existing_user = result.scalar_one_or_none()

        default_users = [
            User(
                id="root_01",
                email="root@gmail.com",
                hashed_password=hash_password("root123"),
                full_name="Super Administrator",
                role="root",
                is_active=True,
            ),
            User(
                id="admin_01",
                email="admin@gmail.com",
                hashed_password=hash_password("admin123"),
                full_name="Platform Admin",
                role="admin",
                is_active=True,
            ),
            User(
                id="user_01",
                email="user@gmail.com",
                hashed_password=hash_password("user123"),
                full_name="Demo Customer",
                role="user",
                is_active=True,
            ),
        ]

        for u in default_users:
            session.add(u)
        await session.commit()

        logger.info("=" * 55)
        logger.info("[DB] Default users created:")
        logger.info("[DB]   root@generator.local  / root123  (root)")
        logger.info("[DB]   admin@generator.local / admin123 (admin)")
        logger.info("[DB]   user@generator.local  / user123  (user)")
        logger.info("[DB] Change all passwords before production!")
        logger.info("=" * 55)

async def seed_demo_devices() -> None:
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

        demo_devices = [
            Device(
                id="gen_01",
                name="Generator 1 - Site A",
                description="Main backup generator",
                location="Building A - Basement",
                owner_user_id=None,   # No owner = admin/root only
                is_active=True,
            ),
            Device(
                id="gen_02",
                name="Generator 2 - Site B",
                description="Secondary generator",
                location="Building B - Rooftop",
                owner_user_id=None,    # No owner = admin/root only
                is_active=True,
            ),
        ]

        for device in demo_devices:
            session.add(device)

        await session.commit()
        logger.info("[DB] Demo devices created: gen_01, gen_02")



async def init_db() -> None:
    """
    Master init function called from main.py on startup.
    Runs table creation then user seeding in order.
    """
    await create_tables()
    await seed_users()
    await seed_demo_devices()