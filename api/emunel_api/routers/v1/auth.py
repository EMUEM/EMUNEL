"""EMUNEL API — Authentication router."""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ...database import get_db
from ...models.user import User, UserRole
from ...services.auth import (
    create_access_token,
    verify_password,
    hash_password,
    get_current_user,
    create_api_key,
)

router = APIRouter()


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: dict


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=8, max_length=128)
    email: Optional[str] = None


class ApiKeyResponse(BaseModel):
    key: str
    name: str
    created_at: datetime


@router.post("/login", response_model=TokenResponse)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """Authenticate and return a JWT token."""
    result = await db.execute(
        select(User).where(User.username == form_data.username)
    )
    user = result.scalar_one_or_none()

    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled",
        )

    # Update last login
    user.last_login = datetime.utcnow()
    await db.commit()

    token, expires_in = create_access_token(user_id=user.id, role=user.role.value)

    return TokenResponse(
        access_token=token,
        expires_in=expires_in,
        user={
            "id": user.id,
            "username": user.username,
            "role": user.role.value,
            "email": user.email,
        },
    )


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    request: RegisterRequest,
    db: AsyncSession = Depends(get_db),
):
    """Register a new user account."""
    # Check if username exists
    existing = await db.execute(
        select(User).where(User.username == request.username)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already exists",
        )

    user = User(
        username=request.username,
        hashed_password=hash_password(request.password),
        email=request.email,
        role=UserRole.USER,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    return {
        "id": user.id,
        "username": user.username,
        "role": user.role.value,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@router.get("/me")
async def me(current_user: User = Depends(get_current_user)):
    """Get current authenticated user info."""
    return {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "role": current_user.role.value,
        "is_active": current_user.is_active,
        "created_at": current_user.created_at.isoformat() if current_user.created_at else None,
        "last_login": current_user.last_login.isoformat() if current_user.last_login else None,
    }


@router.post("/api-key", response_model=ApiKeyResponse)
async def generate_api_key(
    name: str = "default",
    current_user: User = Depends(get_current_user),
):
    """Generate a new API key for the current user."""
    key = create_api_key(user_id=current_user.id, name=name)
    return ApiKeyResponse(
        key=key,
        name=name,
        created_at=datetime.utcnow(),
    )


@router.post("/refresh")
async def refresh_token(current_user: User = Depends(get_current_user)):
    """Refresh the JWT token."""
    token, expires_in = create_access_token(
        user_id=current_user.id, role=current_user.role.value
    )
    return {"access_token": token, "token_type": "bearer", "expires_in": expires_in}
