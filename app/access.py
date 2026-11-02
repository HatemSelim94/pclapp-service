"""Who may do what with a cloud. One table, one function.

    action     owner  editor  viewer  stranger
    read         ✓      ✓       ✓       404
    download     ✓      ✓       ✓       404
    job          ✓      ✓       403     404
    share        ✓      403     403     404
    delete       ✓      403     403     404

Strangers get 404, not 403: a 403 would confirm that the id exists. Users who can see the
cloud but lack the right get 403, since they already know it exists.

Soft-deleted clouds are strangers' clouds to everyone, including the owner.
"""

from __future__ import annotations

import enum
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Cloud, Share, ShareRole, User


class Role(enum.StrEnum):
    owner = "owner"
    editor = "editor"
    viewer = "viewer"


class Action(enum.StrEnum):
    read = "read"
    download = "download"
    job = "job"
    share = "share"
    delete = "delete"


ALLOWED: dict[Action, frozenset[Role]] = {
    Action.read: frozenset({Role.owner, Role.editor, Role.viewer}),
    Action.download: frozenset({Role.owner, Role.editor, Role.viewer}),
    Action.job: frozenset({Role.owner, Role.editor}),
    Action.share: frozenset({Role.owner}),
    Action.delete: frozenset({Role.owner}),
}


def role_of(session: Session, user: User, cloud: Cloud) -> Role | None:
    if cloud.owner_id == user.id:
        return Role.owner
    share_role = session.scalar(
        select(Share.role).where(Share.cloud_id == cloud.id, Share.user_id == user.id)
    )
    if share_role is None:
        return None
    return Role.editor if share_role == ShareRole.editor else Role.viewer


def get_cloud(session: Session, user: User, cloud_id: uuid.UUID, action: Action) -> Cloud:
    """Load the cloud if ``user`` may perform ``action`` on it; raise 404/403 otherwise."""
    cloud = session.get(Cloud, cloud_id)
    if cloud is None or cloud.deleted_at is not None:
        raise _not_found()
    role = role_of(session, user, cloud)
    if role is None:
        raise _not_found()
    if role not in ALLOWED[action]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=f"Not allowed to {action.value}.")
    return cloud


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, detail="Cloud not found.")
