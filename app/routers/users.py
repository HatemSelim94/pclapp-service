"""Admin endpoints: create users, issue and revoke their API keys."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import auth, schemas
from app.db import get_session
from app.models import ApiKey, User

router = APIRouter(prefix="/users", tags=["users"], dependencies=[auth.AdminOnly])

DB = Annotated[Session, Depends(get_session)]


def _get_user(session: Session, user_id: uuid.UUID) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.")
    return user


@router.post("", status_code=status.HTTP_201_CREATED, response_model=schemas.UserOut)
def create_user(body: schemas.UserCreate, session: DB) -> User:
    user = User(name=body.name)
    session.add(user)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="User name already taken.") from exc
    return user


@router.get("", response_model=list[schemas.UserOut])
def list_users(session: DB) -> list[User]:
    return list(session.scalars(select(User).order_by(User.created_at, User.id)))


@router.post(
    "/{user_id}/api-keys",
    status_code=status.HTTP_201_CREATED,
    response_model=schemas.ApiKeyIssued,
)
def create_api_key(
    user_id: uuid.UUID, body: schemas.ApiKeyCreate, session: DB
) -> schemas.ApiKeyIssued:
    user = _get_user(session, user_id)
    row, key = auth.issue_key(session, user, label=body.label)
    return schemas.ApiKeyIssued(id=row.id, key=key, key_prefix=row.key_prefix, label=row.label)


@router.get("/{user_id}/api-keys", response_model=list[schemas.ApiKeyOut])
def list_api_keys(user_id: uuid.UUID, session: DB) -> list[ApiKey]:
    _get_user(session, user_id)
    return list(
        session.scalars(select(ApiKey).where(ApiKey.user_id == user_id).order_by(ApiKey.created_at))
    )


@router.delete("/{user_id}/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_api_key(user_id: uuid.UUID, key_id: uuid.UUID, session: DB) -> None:
    row = session.get(ApiKey, key_id)
    if row is None or row.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="API key not found.")
    if row.revoked_at is None:
        auth.revoke_key(session, row)
