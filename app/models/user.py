"""
app/models/user.py
───────────────────
User Database Model
═══════════════════════════════════════════════════════════════════════════════

This file defines the `users` table structure using SQLAlchemy ORM.

Instead of writing SQL like:
    CREATE TABLE users (
        id VARCHAR PRIMARY KEY,
        email VARCHAR UNIQUE NOT NULL,
        ...
    );

We write a Python class. SQLAlchemy translates it to SQL automatically.
This is the same concept as Django models or Flask-SQLAlchemy models.
═══════════════════════════════════════════════════════════════════════════════
"""

from sqlalchemy import Boolean, Column, String, DateTime
from sqlalchemy.sql import func

from app.db.database import Base


class User(Base):
    """
    Represents a row in the `users` table.

    Roles:
        admin  → internal company user, full access + control commands
        user   → external customer, view-only access to assigned devices
    """

    __tablename__ = "users"

    # ── Columns ────────────────────────────────────────────────────────────────

    id = Column(
        String,
        primary_key=True,
        index=True,
        # Example value: "user_01", "user_02"
        # We set this manually so IDs are human-readable
    )

    email = Column(
        String,
        unique=True,        # No two users can have the same email
        nullable=False,
        index=True,         # Indexed for fast lookup on login
    )

    hashed_password = Column(
        String,
        nullable=False,
        # NEVER stores plain text — always bcrypt hash
        # Example: "$2b$12$KIXjJ8p9z3Qw7Y5vM..."
    )

    full_name = Column(
        String,
        nullable=True,
    )

    role = Column(
        String,
        nullable=False,
        default="user",
        # Allowed values: "admin" | "user"
        # admin → can send Start/Stop commands
        # user  → read-only access
    )

    is_active = Column(
        Boolean,
        default=True,
        nullable=False,
        # False = account disabled (soft delete — we never delete users)
    )

    # ── Timestamps ─────────────────────────────────────────────────────────────

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),  # PostgreSQL sets this automatically
        nullable=False,
    )

    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),        # Auto-updates on every row change
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email} role={self.role}>"
