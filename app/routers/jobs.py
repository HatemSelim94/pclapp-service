"""Jobs: enqueue processing for a cloud, read status and results."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session, selectinload

from app import schemas
from app.access import Action, get_cloud
from app.auth import CurrentUser
from app.config import Settings, get_settings
from app.db import get_session, get_sessionmaker
from app.models import Job, JobStatus, JobType
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


# --- server-sent events ---------------------------------------------------------------------------


def _sse(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _job_events(job_id: uuid.UUID, settings: Settings) -> Iterator[str]:
    """Poll the row; emit `status` whenever (status, attempts) changes; `end` when it is final.

    Each poll is its own short session — the request's session is not held open across the
    stream. Polling rather than LISTEN/NOTIFY keeps the worker ignorant of who is watching.
    """
    sessions = get_sessionmaker()
    started = time.monotonic()
    last_sent = started
    last_key: tuple[str, int] | None = None
    while True:
        with sessions() as session:
            job = session.scalars(
                select(Job).where(Job.id == job_id).options(selectinload(Job.clusters))
            ).first()
            if job is None:
                yield _sse("gone", {"id": str(job_id)})
                return
            key = (job.status.value, job.attempts)
            if key != last_key:
                last_key = key
                last_sent = time.monotonic()
                yield _sse("status", schemas.JobOut.model_validate(job).model_dump(mode="json"))
                if job.status in (JobStatus.done, JobStatus.failed):
                    yield _sse("end", {"id": str(job.id), "status": job.status.value})
                    return
        now = time.monotonic()
        if now - started > settings.sse_max_seconds:
            yield _sse("timeout", {"id": str(job_id)})
            return
        if now - last_sent > settings.sse_keepalive_seconds:
            last_sent = now
            yield ": keepalive\n\n"
        time.sleep(settings.sse_poll_seconds)


@router.get("/jobs/{job_id}/events", response_class=StreamingResponse)
def job_events(
    job_id: uuid.UUID, user: CurrentUser, session: DB, settings: Conf
) -> StreamingResponse:
    """Server-sent events: one `status` event per change, then `end`. Closes on its own when the
    job is done or failed, or after `sse_max_seconds` with a `timeout` event."""
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Job not found.")
    get_cloud(session, user, job.cloud_id, Action.read)
    return StreamingResponse(
        _job_events(job_id, settings),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
