"""
app/models/user_device.py
──────────────────────────
User-Device Junction Table (Many-to-Many)
═══════════════════════════════════════════════════════════════════════════════

This table is the bridge between users and devices.
One row = one assignment (one user has access to one device).

Example rows in this table:
  user_id    | device_id
  -----------|----------
  admin_01   | gen_01
  admin_01   | gen_02
  user_01    | gen_01       ← same gen_01, different user
  user_02    | gen_01       ← same gen_01, third user
  user_02    | gen_03

Reading from left:
  admin_01 → can see gen_01, gen_02
  user_01  → can see gen_01
  user_02  → can see gen_01, gen_03

Reading from right:
  gen_01 → accessible by admin_01, user_01, user_02
  gen_02 → accessible by admin_01 only
  gen_03 → accessible by user_02 only

root role BYPASSES this table entirely — always sees all devices.
═══════════════════════════════════════════════════════════════════════════════
"""

from sqlalchemy import Column, String, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.sql import func

from app.db.database import Base


class UserDevice(Base):
    """
    Junction table — one row per user-device access assignment.
    """

    __tablename__ = "user_devices"

    # ── Composite Primary Key ──────────────────────────────────────────────────
    # Both columns together form the primary key.
    # This naturally prevents duplicate assignments
    # (same user_id + device_id combination cannot exist twice).

    user_id = Column(
        String,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
        # CASCADE: if a user is deleted from users table,
        # all their device assignments are automatically removed too.
    )

    device_id = Column(
        String,
        ForeignKey("devices.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
        # CASCADE: if a device is deleted from devices table,
        # all user assignments to it are automatically removed too.
    )

    # ── Metadata ───────────────────────────────────────────────────────────────
    assigned_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        # When this assignment was created — useful for audit trail
    )

    assigned_by = Column(
        String,
        nullable=True,
        # Which root user created this assignment
        # Stored as a user_id string (not FK to keep it simple)
    )

    # ── Explicit unique constraint (documents intent clearly) ─────────────────
    __table_args__ = (
        UniqueConstraint("user_id", "device_id", name="uq_user_device"),
    )

    def __repr__(self) -> str:
        return f"<UserDevice user={self.user_id} device={self.device_id}>"
