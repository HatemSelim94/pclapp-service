"""One runner per job type, each split in two halves.

``compute`` runs *outside* any database transaction — it may take seconds — and returns a
plain result object. ``apply`` runs inside the finishing transaction and writes rows. The
split is what keeps the worker's transactions short: the database never waits on PCL.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models import Cloud, Cluster, Job, JobType
from app.schemas import PARAM_MODELS, ClusterParams, DownsampleParams
from app.storage import Storage
from worker.processor import ClusterInfo, Processor, Summary


class PermanentJobError(Exception):
    """The job can never succeed (bad params, deleted cloud); do not retry."""


@dataclass(frozen=True)
class JobSnapshot:
    """What a runner needs, copied out of the claim transaction so no row stays locked."""

    id: uuid.UUID
    type: JobType
    params: dict[str, Any]
    attempts: int
    max_attempts: int
    cloud_id: uuid.UUID
    cloud_path: str
    cloud_name: str
    has_intensity: bool
    cloud_deleted: bool
    requested_by: uuid.UUID

    @classmethod
    def of(cls, job: Job, cloud: Cloud) -> JobSnapshot:
        return cls(
            id=job.id,
            type=job.type,
            params=dict(job.params),
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            cloud_id=cloud.id,
            cloud_path=cloud.path,
            cloud_name=cloud.name,
            has_intensity=cloud.has_intensity,
            cloud_deleted=cloud.deleted_at is not None,
            requested_by=job.requested_by,
        )


def validate_params(snap: JobSnapshot) -> Any:
    try:
        return PARAM_MODELS[snap.type.value].model_validate(snap.params)
    except ValidationError as exc:
        raise PermanentJobError(f"invalid params: {exc.errors()[0]['msg']}") from exc


# --- results -------------------------------------------------------------------------------


@dataclass(frozen=True)
class SummarizeResult:
    summary: Summary


@dataclass(frozen=True)
class DownsampleResult:
    tmp_file: Path  # the new cloud, waiting in tmp/ until apply() moves it into place
    summary: Summary
    leaf_size: float


@dataclass(frozen=True)
class ClusterResult:
    clusters: list[ClusterInfo]


Result = SummarizeResult | DownsampleResult | ClusterResult


# --- compute (no transaction) ------------------------------------------------------------------


def compute(snap: JobSnapshot, processor: Processor, storage: Storage) -> Result:
    if snap.cloud_deleted:
        raise PermanentJobError("cloud was deleted")
    params = validate_params(snap)
    src = storage.abs_path(snap.cloud_path)
    if not src.is_file():
        raise PermanentJobError(f"cloud file missing: {snap.cloud_path}")

    match snap.type:
        case JobType.summarize:
            return SummarizeResult(processor.summarize(src, has_intensity=snap.has_intensity))
        case JobType.downsample:
            assert isinstance(params, DownsampleParams)
            storage.ensure_dirs()
            tmp = storage.root / "tmp" / f"{uuid.uuid4().hex}.part"
            try:
                summary = processor.downsample(
                    src, tmp, has_intensity=snap.has_intensity, leaf_size=params.leaf_size
                )
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            return DownsampleResult(tmp_file=tmp, summary=summary, leaf_size=params.leaf_size)
        case JobType.cluster:
            assert isinstance(params, ClusterParams)
            clusters = processor.cluster(
                src,
                has_intensity=snap.has_intensity,
                tolerance=params.tolerance,
                min_size=params.min_size,
                max_size=params.max_size,
            )
            return ClusterResult(clusters)


# --- apply (inside the finishing transaction) -------------------------------------------------


def _fill_summary(cloud: Cloud, s: Summary) -> None:
    cloud.point_count = s.point_count
    cloud.bbox_min = list(s.bbox_min) if s.bbox_min else None
    cloud.bbox_max = list(s.bbox_max) if s.bbox_max else None
    cloud.centroid = list(s.centroid) if s.centroid else None


def apply(session: Session, job: Job, result: Result, storage: Storage) -> str | None:
    """Write ``result`` for ``job``; the caller sets status/finished_at and commits.

    Returns the relative path of a file this put in place, if any, so that the caller can
    remove it again should the commit fail.
    """
    match result:
        case SummarizeResult(summary):
            cloud = session.get_one(Cloud, job.cloud_id)
            _fill_summary(cloud, summary)
            return None

        case DownsampleResult(tmp_file, summary, leaf_size):
            source = session.get_one(Cloud, job.cloud_id)
            derived = Cloud(
                id=uuid.uuid4(),
                owner_id=source.owner_id,
                name=f"{source.name} (downsampled {leaf_size:g})"[:200],
                format="pcd",
                size_bytes=tmp_file.stat().st_size,
                has_intensity=source.has_intensity,
                derived_from_cloud_id=source.id,
            )
            derived.path = storage.rel_path(derived.id, "pcd")
            _fill_summary(derived, summary)
            # Same rule as uploads: file in place first, row committed after; the caller
            # deletes the file if the commit fails (see Worker._finish).
            storage.finalize(tmp_file, derived.path)
            session.add(derived)
            job.result_cloud_id = derived.id
            return derived.path

        case ClusterResult(clusters):
            for i, c in enumerate(clusters):
                session.add(
                    Cluster(
                        job_id=job.id,
                        index=i,
                        size=c.size,
                        bbox_min=list(c.bbox_min),
                        bbox_max=list(c.bbox_max),
                        centroid=list(c.centroid),
                    )
                )
            return None
