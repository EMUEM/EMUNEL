"""EMUNEL API — Users router."""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_db
from ...models.user import User, UserRole
from ...services.auth import get_current_user, require_admin, hash_password

router = APIRouter()


class UserOut(BaseModel):
    id: str
    username: str
    email: Optional[str] = None
    role: str
    is_active: bool
    created_at: Optional[str] = None
    last_login: Optional[str] = None


class UserUpdate(BaseModel):
    email: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    password: Optional[str] = Field(None, min_length=8)


class UsersListResponse(BaseModel):
    users: List[UserOut]
    total: int
    page: int
    per_page: int


@router.get("", response_model=UsersListResponse)
async def list_users(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    role: Optional[str] = None,
    is_active: Optional[bool] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """List all users with pagination and filtering."""
    query = select(User)

    if search:
        query = query.where(
            User.username.ilike(f"%{search}%") | User.email.ilike(f"%{search}%")
        )
    if role:
        query = query.where(User.role == UserRole(role))
    if is_active is not None:
        query = query.where(User.is_active == is_active)

    # Count total
    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    # Paginate
    query = query.offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(query)
    users = result.scalars().all()

    return UsersListResponse(
        users=[
            UserOut(
                id=u.id,
                username=u.username,
                email=u.email,
                role=u.role.value,
                is_active=u.is_active,
                created_at=u.created_at.isoformat() if u.created_at else None,
                last_login=u.last_login.isoformat() if u.last_login else None,
            )
            for u in users
        ],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get("/{user_id}", response_model=UserOut)
async def get_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Get a specific user by ID."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return UserOut(
        id=user.id,
        username=user.username,
        email=user.email,
        role=user.role.value,
        is_active=user.is_active,
        created_at=user.created_at.isoformat() if user.created_at else None,
        last_login=user.last_login.isoformat() if user.last_login else None,
    )


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: str,
    update: UserUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Update a user."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if update.email is not None:
        user.email = update.email
    if update.role is not None:
        user.role = UserRole(update.role)
    if update.is_active is not None:
        user.is_active = update.is_active
    if update.password is not None:
        user.hashed_password = hash_password(update.password)

    await db.commit()
    await db.refresh(user)

    return UserOut(
        id=user.id,
        username=user.username,
        email=user.email,
        role=user.role.value,
        is_active=user.is_active,
        created_at=user.created_at.isoformat() if user.created_at else None,
        last_login=user.last_login.isoformat() if user.last_login else None,
    )


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Delete a user."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")

    await db.delete(user)
    await db.commit()
