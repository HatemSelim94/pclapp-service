"""SQLAlchemy models. One file; the schema is small enough to read top to bottom.

Postgres-specific on purpose (native enums, JSONB, float8[]): the point of the project is
Postgres, there is no SQLite fallback.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, DOUBLE_PRECISION, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

Vec3 = ARRAY(DOUBLE_PRECISION, dimensions=1)


class JobType(enum.StrEnum):
    summarize = "summarize"
    downsample = "downsample"
    cluster = "cluster"


class JobStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


class ShareRole(enum.StrEnum):
    viewer = "viewer"
    editor = "editor"


def _pg_enum(py_enum: type[enum.StrEnum], name: str) -> Enum:
    # values_callable: store the enum *values* ("queued"), not the member names.
    return Enum(py_enum, name=name, values_callable=lambda e: [m.value for m in e])


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    created_at: Mapped[datetime] = _created_at()

    api_keys: Mapped[list[ApiKey]] = relationship(back_populates="user")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # sha256 of the full key. Keys are 32 random bytes, so a fast hash is the right one.
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # First characters of the key, kept in clear so a user can tell their keys apart.
    key_prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = _created_at()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="api_keys")


class Cloud(Base):
    __tablename__ = "clouds"
    __table_args__ = (
        CheckConstraint("format IN ('pcd', 'txt')", name="ck_clouds_format"),
        # Most queries only look at live clouds; the partial index stays small as deletes pile up.
        Index("ix_clouds_owner_live", "owner_id", postgresql_where=text("deleted_at IS NULL")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    format: Mapped[str] = mapped_column(String(10), nullable=False)  # 'pcd' | 'txt'
    path: Mapped[str] = mapped_column(Text, nullable=False)  # relative to DATA_DIR
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    has_intensity: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    # Filled by the 'summarize' job; null until it has run.
    point_count: Mapped[int | None] = mapped_column(BigInteger)
    bbox_min: Mapped[list[float] | None] = mapped_column(Vec3)
    bbox_max: Mapped[list[float] | None] = mapped_column(Vec3)
    centroid: Mapped[list[float] | None] = mapped_column(Vec3)

    # Lineage. The job that produced a derived cloud is found via jobs.result_cloud_id;
    # a second FK in this direction would make the two tables depend on each other.
    derived_from_cloud_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("clouds.id"))

    created_at: Mapped[datetime] = _created_at()
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    owner: Mapped[User] = relationship()
    derived_from: Mapped[Cloud | None] = relationship(remote_side="Cloud.id")


# Listing: newest first per owner, with id as the tie-breaker the keyset cursor needs.
Index("ix_clouds_owner_created", Cloud.owner_id, Cloud.created_at.desc(), Cloud.id.desc())


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        # The worker's claim query: WHERE status='queued' ORDER BY created_at LIMIT 1.
        Index("ix_jobs_status_created", "status", "created_at"),
        Index("ix_jobs_cloud_created", "cloud_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    cloud_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clouds.id"), nullable=False)
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    type: Mapped[JobType] = mapped_column(_pg_enum(JobType, "job_type"), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[JobStatus] = mapped_column(
        _pg_enum(JobStatus, "job_status"),
        nullable=False,
        default=JobStatus.queued,
        server_default=JobStatus.queued.value,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    error: Mapped[str | None] = mapped_column(Text)
    worker_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = _created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_cloud_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("clouds.id"))

    cloud: Mapped[Cloud] = relationship(foreign_keys=[cloud_id])
    result_cloud: Mapped[Cloud | None] = relationship(foreign_keys=[result_cloud_id])
    clusters: Mapped[list[Cluster]] = relationship(
        back_populates="job", order_by="Cluster.index", cascade="all, delete-orphan"
    )


class Cluster(Base):
    __tablename__ = "clusters"
    __table_args__ = (UniqueConstraint("job_id", "index", name="uq_clusters_job_index"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    bbox_min: Mapped[list[float]] = mapped_column(Vec3, nullable=False)
    bbox_max: Mapped[list[float]] = mapped_column(Vec3, nullable=False)
    centroid: Mapped[list[float]] = mapped_column(Vec3, nullable=False)

    job: Mapped[Job] = relationship(back_populates="clusters")


class Share(Base):
    __tablename__ = "shares"
    __table_args__ = (Index("ix_shares_user", "user_id"),)

    cloud_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clouds.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[ShareRole] = mapped_column(_pg_enum(ShareRole, "share_role"), nullable=False)
    granted_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created_at()
