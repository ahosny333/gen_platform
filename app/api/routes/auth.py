"""
app/api/routes/auth.py
───────────────────────
Authentication Routes
═══════════════════════════════════════════════════════════════════════════════

Currently contains:
  POST /api/auth/login  → validates credentials, returns JWT token

This is the ONLY public endpoint in the entire platform.
All other endpoints require a valid JWT token in the header.
═══════════════════════════════════════════════════════════════════════════════
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.core.security import verify_password, create_access_token
from app.db.database import get_db
from app.models.user import User
from app.schemas.auth import LoginRequest, LoginResponse, UserInfo, ErrorResponse

# ── Router ─────────────────────────────────────────────────────────────────────
# APIRouter is Flask's Blueprint equivalent.
# We define routes here and register them in main.py with a prefix.
router = APIRouter()


# ══════════════════════════════════════════════════════════════════════════════
# POST /api/auth/login
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/login",
    response_model=LoginResponse,       # FastAPI validates our response shape
    responses={
        401: {"model": ErrorResponse},  # Documents the error response in /docs
    },
    summary="User Login",
    description="Authenticate with email and password. Returns a JWT token.",
)
async def login(
    request: LoginRequest,              # Pydantic validates incoming JSON
    db: AsyncSession = Depends(get_db), # Injects a DB session automatically
):
    """
    Login flow — step by step:

    1. Receive email + password from frontend
    2. Look up user by email in database
    3. If not found → return 401 (same error as wrong password — security)
    4. Verify the password against the stored bcrypt hash
    5. If wrong → return 401
    6. If correct → create JWT token with user ID + role inside
    7. Return token + user info to frontend

    The frontend then stores the token and sends it in every future request:
        Authorization: Bearer <token>
    """

    logger.info(f"[Auth] Login attempt for email: {request.email}")

    # ── Step 1: Find user by email ─────────────────────────────────────────────
    result = await db.execute(
        select(User).where(User.email == request.email)
    )
    user = result.scalar_one_or_none()

    # ── Step 2: Check user exists ──────────────────────────────────────────────
    # IMPORTANT: We return the SAME error whether email is wrong OR password
    # is wrong. This prevents attackers from knowing which one failed
    # (called "user enumeration protection").
    if not user:
        logger.warning(f"[Auth] Login failed — email not found: {request.email}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "Invalid credentials"},
        )

    # ── Step 3: Check account is active ───────────────────────────────────────
    if not user.is_active:
        logger.warning(f"[Auth] Login failed — account disabled: {request.email}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "Account is disabled"},
        )

    # ── Step 4: Verify password ────────────────────────────────────────────────
    if not verify_password(request.password, user.hashed_password):
        logger.warning(f"[Auth] Login failed — wrong password: {request.email}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "Invalid credentials"},
        )

    # ── Step 5: Create JWT token ───────────────────────────────────────────────
    # We embed user ID and role inside the token.
    # The role is used by other routes to allow/deny control commands.
    token = create_access_token(data={
        "sub":  user.id,     # "sub" = subject = who this token belongs to
        "role": user.role,
    })

    logger.info(
        f"[Auth] ✅ Login successful: {request.email} "
        f"(id={user.id}, role={user.role})"
    )

    # ── Step 6: Return token + user info ──────────────────────────────────────
    return LoginResponse(
        token=token,
        user=UserInfo(
            id=user.id,
            role=user.role,
            email=user.email,
            full_name=user.full_name,
        ),
    )
