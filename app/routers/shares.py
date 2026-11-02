"""Shares: the owner grants another user `viewer` or `editor` on one cloud.

Granting again with a different role updates the existing share (the primary key is
``(cloud_id, user_id)``, so there is never more than one row per pair).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import schemas
from app.access import Action, get_cloud
from app.auth import CurrentUser
from app.db import get_session
from app.models import Share, ShareRole, User

router = APIRouter(prefix="/clouds/{cloud_id}/shares", tags=["shares"])

DB = Annotated[Session, Depends(get_session)]


@router.get("", response_model=list[schemas.ShareOut])
def list_shares(cloud_id: uuid.UUID, user: CurrentUser, session: DB) -> list[Share]:
    get_cloud(session, user, cloud_id, Action.share)
    return list(
        session.scalars(select(Share).where(Share.cloud_id == cloud_id).order_by(Share.created_at))
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=schemas.ShareOut)
def grant_share(
    cloud_id: uuid.UUID, body: schemas.ShareCreate, user: CurrentUser, session: DB
) -> Share:
    cloud = get_cloud(session, user, cloud_id, Action.share)
    if body.user_id == cloud.owner_id:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="The owner needs no share.")
    if session.get(User, body.user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.")

    share = session.get(Share, (cloud.id, body.user_id))
    if share is None:
        share = Share(cloud_id=cloud.id, user_id=body.user_id, granted_by=user.id)
        session.add(share)
    share.role = ShareRole(body.role)
    share.granted_by = user.id
    session.flush()
    session.refresh(share)
    return share


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_share(cloud_id: uuid.UUID, user_id: uuid.UUID, user: CurrentUser, session: DB) -> None:
    get_cloud(session, user, cloud_id, Action.share)
    share = session.get(Share, (cloud_id, user_id))
    if share is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Share not found.")
    session.delete(share)
