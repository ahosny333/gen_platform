"""
app/core/dependencies.py
─────────────────────────
Reusable FastAPI Dependencies
═══════════════════════════════════════════════════════════════════════════════

Dependencies are functions that FastAPI runs BEFORE your route handler.
They handle cross-cutting concerns like authentication and authorization.

Think of them like Flask's @login_required decorator — but more flexible:
  - They can be applied per-route or per-router
  - They can return values (the current user) to the route handler
  - They can raise HTTP exceptions to block access

Usage in any route:
    from app.core.dependencies import require_auth, require_admin

    @router.get("/devices")
    async def get_devices(current_user = Depends(require_auth)):
        # current_user is available here — guaranteed to be authenticated
        ...

    @router.post("/command")
    async def send_command(current_user = Depends(require_admin)):
        # Only admin users reach this code
        ...
═══════════════════════════════════════════════════════════════════════════════
"""

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.core.security import decode_access_token
from app.db.database import get_db
from app.models.user import User

# ── Token Extractor ────────────────────────────────────────────────────────────
# HTTPBearer reads the "Authorization: Bearer <token>" header automatically.
# auto_error=False means we handle the missing token ourselves (better error msg)
bearer_scheme = HTTPBearer(auto_error=False)


async def require_auth(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Dependency that verifies a JWT token and returns the current user.

    What it does step by step:
      1. Reads the Authorization: Bearer <token> header
      2. Decodes and verifies the JWT signature + expiry
      3. Extracts user_id from the token payload
      4. Loads the full User object from DB
      5. Returns the User to the route handler

    If anything fails → raises HTTP 401 (Unauthorized)
    The route handler never runs if this raises.
    """

    # ── Check header exists ────────────────────────────────────────────────────
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "Authorization header missing"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    # ── Decode JWT ─────────────────────────────────────────────────────────────
    token = credentials.credentials
    payload = decode_access_token(token)

    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "Token is invalid or expired"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    # ── Load user from DB ──────────────────────────────────────────────────────
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "Invalid token payload"},
        )

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "User not found or disabled"},
        )

    return user


async def require_admin(
    current_user: User = Depends(require_auth),
) -> User:
    """
    Dependency that requires the user to be an admin.
    Builds on top of require_auth — first authenticates, then checks role.

    Used for control commands (Start/Stop) — only admins can send these.
    External customers (role="user") will get HTTP 403 Forbidden.
    """
    if current_user.role != "admin":
        logger.warning(
            f"[Auth] Access denied — user {current_user.id} "
            f"(role={current_user.role}) attempted admin action"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "Admin access required for this action"},
        )
    return current_user
