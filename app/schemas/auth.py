"""
app/schemas/auth.py
────────────────────
Auth Request & Response Schemas (Pydantic)
═══════════════════════════════════════════════════════════════════════════════

Schemas define the SHAPE of data coming IN (requests) and going OUT (responses).

Think of them as:
  - Input validation (like WTForms or marshmallow in Flask)
  - Response serialization (what JSON we return to the frontend)
  - Auto-documentation (FastAPI reads these to build /docs)

IMPORTANT — Schema vs Model:
  Model  (app/models/)   = database table definition (SQLAlchemy)
  Schema (app/schemas/)  = API data shape (Pydantic)

  They look similar but serve different purposes:
  Model  → talks to PostgreSQL
  Schema → talks to the HTTP client (frontend)
═══════════════════════════════════════════════════════════════════════════════
"""

from pydantic import BaseModel, EmailStr, Field


# ── Request Schemas (Frontend → Backend) ──────────────────────────────────────

class LoginRequest(BaseModel):
    """
    Shape of the JSON body sent by the frontend on login.

    Frontend sends:
        POST /api/auth/login
        {
            "email": "admin@generator.local",
            "password": "admin123"
        }

    Pydantic automatically:
      - Validates email is a valid email format
      - Returns 422 error if fields are missing or wrong type
      - No extra code needed from us
    """
    email: EmailStr = Field(
        ...,                              # ... means required
        example="admin@generator.local",
    )
    password: str = Field(
        ...,
        min_length=6,                     # Reject passwords shorter than 6 chars
        example="admin123",
    )


# ── Response Schemas (Backend → Frontend) ─────────────────────────────────────

class UserInfo(BaseModel):
    """
    User information included in the login response.
    We return only safe fields — never the hashed_password.
    """
    id: str = Field(example="user_01")
    role: str = Field(example="admin")
    email: EmailStr = Field(example="admin@generator.local")
    full_name: str | None = Field(default=None, example="Platform Admin")


class LoginResponse(BaseModel):
    """
    Shape of the JSON response returned after successful login.

    Backend returns:
        {
            "token": "eyJhbGciOiJIUzI1NiJ9...",
            "user": {
                "id": "user_01",
                "role": "admin",
                "email": "admin@generator.local",
                "full_name": "Platform Admin"
            }
        }

    The frontend stores `token` and sends it in every future request:
        Authorization: Bearer eyJhbGciOiJIUzI1NiJ9...
    """
    token: str = Field(example="eyJhbGciOiJIUzI1NiJ9...")
    user: UserInfo


class ErrorResponse(BaseModel):
    """
    Standard error shape returned on failed login.

        {
            "error": "Invalid credentials"
        }
    """
    error: str = Field(example="Invalid credentials")
