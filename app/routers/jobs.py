"""Jobs: enqueue processing for a cloud, read status and results."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session, selectinload

from app import schemas
from app.access import Action, get_cloud
from app.auth import CurrentUser
from app.config import Settings, get_settings
from app.db import get_session
from app.models import Job, JobType
from app.pagination import Cursor, Page, page_params

router = APIRouter(tags=["jobs"])

DB = Annotated[Session, Depends(get_session)]
Conf = Annotated[Settings, Depends(get_settings)]
PageParams = Annotated[Page, Depends(page_params)]


@router.post(
    "/clouds/{cloud_id}/jobs", status_code=status.HTTP_202_ACCEPTED, response_model=schemas.JobOut
)
def create_job(
    cloud_id: uuid.UUID,
    body: schemas.JobCreate,
    user: CurrentUser,
    session: DB,
    settings: Conf,
) -> Job:
    """Enqueue a job. Owner or editor; params are validated here, the worker just runs them."""
    cloud = get_cloud(session, user, cloud_id, Action.job)
    job = Job(
        cloud_id=cloud.id,
        requested_by=user.id,
        type=JobType(body.type),
        params=body.params.model_dump(),
        max_attempts=settings.worker_max_attempts,
    )
    session.add(job)
    session.flush()
    session.refresh(job)
    return job


@router.get("/jobs/{job_id}", response_model=schemas.JobOut)
def get_job(job_id: uuid.UUID, user: CurrentUser, session: DB) -> Job:
    job = session.scalars(
        select(Job).where(Job.id == job_id).options(selectinload(Job.clusters))
    ).first()
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Job not found.")
    # Visibility follows the cloud: if you may read the cloud, you may read its jobs.
    get_cloud(session, user, job.cloud_id, Action.read)
    return job


@router.get("/clouds/{cloud_id}/jobs", response_model=schemas.JobPage)
def list_jobs(
    cloud_id: uuid.UUID, user: CurrentUser, session: DB, page: PageParams
) -> schemas.JobPage:
    """Jobs of one cloud, newest first, same keyset cursor as `/clouds`."""
    get_cloud(session, user, cloud_id, Action.read)
    stmt = (
        select(Job)
        .where(Job.cloud_id == cloud_id)
        .options(selectinload(Job.clusters))
        .order_by(Job.created_at.desc(), Job.id.desc())
        .limit(page.limit + 1)
    )
    if page.cursor:
        stmt = stmt.where(
            tuple_(Job.created_at, Job.id) < tuple_(page.cursor.created_at, page.cursor.id)
        )
    rows = list(session.scalars(stmt))
    next_cursor = None
    if len(rows) > page.limit:
        rows = rows[: page.limit]
        next_cursor = Cursor(rows[-1].created_at, rows[-1].id).encode()
    return schemas.JobPage(
        items=[schemas.JobOut.model_validate(j) for j in rows], next_cursor=next_cursor
    )
