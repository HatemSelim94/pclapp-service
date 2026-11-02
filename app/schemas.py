"""Pydantic request/response models. Validation lives here, not in routers or the worker."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


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
