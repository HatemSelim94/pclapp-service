"""Clouds: upload, list, get, download, soft delete."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import exists, or_, select, tuple_
from sqlalchemy.orm import Session

from app import schemas
from app.access import Action, get_cloud
from app.auth import CurrentUser
from app.config import Settings, get_settings
from app.db import get_session
from app.models import Cloud, Job, JobType, Share
from app.pagination import Cursor, Page, page_params
from app.storage import Storage, UploadTooLargeError, get_storage

router = APIRouter(prefix="/clouds", tags=["clouds"])

DB = Annotated[Session, Depends(get_session)]
Store = Annotated[Storage, Depends(get_storage)]
Conf = Annotated[Settings, Depends(get_settings)]
PageParams = Annotated[Page, Depends(page_params)]


def _format_of(filename: str | None, allowed: frozenset[str]) -> str:
    suffix = PurePosixPath(filename or "").suffix.lstrip(".").lower()
    if suffix not in allowed:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unsupported file type {suffix!r}; allowed: {sorted(allowed)}.",
        )
    return suffix


@router.post("", status_code=status.HTTP_201_CREATED, response_model=schemas.CloudOut)
def upload_cloud(
    user: CurrentUser,
    session: DB,
    storage: Store,
    settings: Conf,
    file: Annotated[UploadFile, File()],
    name: Annotated[str | None, Form(max_length=200)] = None,
    intensity: Annotated[bool, Form()] = False,
) -> Cloud:
    """Store the file, create the cloud row and enqueue a `summarize` job for it.

    Order matters (the filesystem is not transactional): temp file → rename into place →
    row + job committed → on commit failure the file is deleted again.
    """
    fmt = _format_of(file.filename, settings.allowed_formats)
    try:
        tmp, size = storage.write_temp(file.file)
    except UploadTooLargeError as exc:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE, detail=f"File larger than {exc.limit} bytes."
        ) from exc

    cloud = Cloud(
        id=uuid.uuid4(),
        owner_id=user.id,
        name=(name or PurePosixPath(file.filename or "cloud").stem or "cloud")[:200],
        format=fmt,
        size_bytes=size,
        has_intensity=intensity,
    )
    cloud.path = storage.rel_path(cloud.id, fmt)
    storage.finalize(tmp, cloud.path)

    job = Job(
        cloud_id=cloud.id,
        requested_by=user.id,
        type=JobType.summarize,
        max_attempts=settings.worker_max_attempts,
    )
    try:
        session.add_all([cloud, job])
        session.commit()
    except Exception:
        session.rollback()
        storage.delete(cloud.path)
        raise
    session.refresh(cloud)
    return cloud


@router.get("", response_model=schemas.CloudPage)
def list_clouds(
    user: CurrentUser,
    session: DB,
    page: PageParams,
    q: Annotated[str | None, Query(max_length=200, description="Name contains (ILIKE).")] = None,
) -> schemas.CloudPage:
    """Clouds you own or that are shared with you, newest first, keyset-paginated."""
    shared_with_me = exists().where(Share.cloud_id == Cloud.id, Share.user_id == user.id)
    stmt = (
        select(Cloud)
        .where(Cloud.deleted_at.is_(None), or_(Cloud.owner_id == user.id, shared_with_me))
        .order_by(Cloud.created_at.desc(), Cloud.id.desc())
        .limit(page.limit + 1)  # one extra row tells us whether there is a next page
    )
    if q:
        pattern = "%" + q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
        stmt = stmt.where(Cloud.name.ilike(pattern, escape="\\"))
    if page.cursor:
        stmt = stmt.where(
            tuple_(Cloud.created_at, Cloud.id) < tuple_(page.cursor.created_at, page.cursor.id)
        )

    rows = list(session.scalars(stmt))
    next_cursor = None
    if len(rows) > page.limit:
        rows = rows[: page.limit]
        last = rows[-1]
        next_cursor = Cursor(last.created_at, last.id).encode()
    return schemas.CloudPage(
        items=[schemas.CloudOut.model_validate(c) for c in rows], next_cursor=next_cursor
    )


@router.get("/{cloud_id}", response_model=schemas.CloudOut)
def get_cloud_meta(cloud_id: uuid.UUID, user: CurrentUser, session: DB) -> Cloud:
    return get_cloud(session, user, cloud_id, Action.read)


@router.get("/{cloud_id}/download", response_class=FileResponse)
def download_cloud(
    cloud_id: uuid.UUID, user: CurrentUser, session: DB, storage: Store
) -> FileResponse:
    cloud = get_cloud(session, user, cloud_id, Action.download)
    path = storage.abs_path(cloud.path)
    if not path.is_file():
        # Should not happen (rows are only committed after the file is in place).
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="File missing.")
    return FileResponse(
        path, media_type="application/octet-stream", filename=f"{cloud.name}.{cloud.format}"
    )


@router.delete("/{cloud_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_cloud(cloud_id: uuid.UUID, user: CurrentUser, session: DB) -> None:
    """Soft delete: the row stays (jobs and lineage still point at it); the file is removed
    later by the `cleanup` command, never on the request path."""
    cloud = get_cloud(session, user, cloud_id, Action.delete)
    cloud.deleted_at = datetime.now(UTC)
