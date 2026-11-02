"""Pydantic request/response models. Validation lives here, not in routers or the worker."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- users / keys -------------------------------------------------------------------------


class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")


class UserOut(ORMModel):
    id: uuid.UUID
    name: str
    created_at: datetime


class ApiKeyCreate(BaseModel):
    label: str = Field(default="", max_length=100)


class ApiKeyIssued(BaseModel):
    """Returned exactly once; the server keeps only the hash."""

    id: uuid.UUID
    key: str
    key_prefix: str
    label: str


class ApiKeyOut(ORMModel):
    id: uuid.UUID
    key_prefix: str
    label: str
    created_at: datetime
    revoked_at: datetime | None


# --- misc ----------------------------------------------------------------------------------


class Health(BaseModel):
    db: str
    data_dir: str


# --- clouds --------------------------------------------------------------------------------


class CloudOut(ORMModel):
    id: uuid.UUID
    owner_id: uuid.UUID
    name: str
    format: str
    size_bytes: int
    has_intensity: bool
    point_count: int | None
    bbox_min: list[float] | None
    bbox_max: list[float] | None
    centroid: list[float] | None
    derived_from_cloud_id: uuid.UUID | None
    derived_by_job_id: uuid.UUID | None
    created_at: datetime


class CloudPage(BaseModel):
    items: list[CloudOut]
    next_cursor: str | None = Field(
        default=None, description="Pass as `cursor` to get the next page; null on the last page."
    )


# --- jobs ----------------------------------------------------------------------------------
# Validation of job parameters lives here and only here: the API rejects bad requests with a
# 422, and the worker re-validates the stored JSONB with the same models before running.


class SummarizeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DownsampleParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    leaf_size: float = Field(gt=0, description="Voxel edge length in the cloud's units.")


class ClusterParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tolerance: float = Field(gt=0, description="Max gap between neighbours in one cluster.")
    min_size: int = Field(default=20, ge=1)
    max_size: int = Field(default=3000, ge=1)

    @model_validator(mode="after")
    def _sizes(self) -> ClusterParams:
        if self.min_size > self.max_size:
            raise ValueError("min_size must be <= max_size")
        return self


class SummarizeJobCreate(BaseModel):
    type: Literal["summarize"]
    params: SummarizeParams = Field(default_factory=SummarizeParams)


class DownsampleJobCreate(BaseModel):
    type: Literal["downsample"]
    params: DownsampleParams


class ClusterJobCreate(BaseModel):
    type: Literal["cluster"]
    params: ClusterParams


# Unknown job types fail here, at the schema, never in the worker.
JobCreate = Annotated[
    SummarizeJobCreate | DownsampleJobCreate | ClusterJobCreate, Field(discriminator="type")
]

PARAM_MODELS: dict[str, type[BaseModel]] = {
    "summarize": SummarizeParams,
    "downsample": DownsampleParams,
    "cluster": ClusterParams,
}


class ClusterOut(ORMModel):
    index: int
    size: int
    bbox_min: list[float]
    bbox_max: list[float]
    centroid: list[float]


class JobOut(ORMModel):
    id: uuid.UUID
    cloud_id: uuid.UUID
    requested_by: uuid.UUID
    type: str
    params: dict[str, Any]
    status: str
    attempts: int
    max_attempts: int
    error: str | None
    worker_id: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    result_cloud_id: uuid.UUID | None
    clusters: list[ClusterOut] = []


class JobPage(BaseModel):
    items: list[JobOut]
    next_cursor: str | None = None


# --- shares --------------------------------------------------------------------------------


class ShareCreate(BaseModel):
    user_id: uuid.UUID
    role: Literal["viewer", "editor"]


class ShareOut(ORMModel):
    cloud_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    granted_by: uuid.UUID
    created_at: datetime
