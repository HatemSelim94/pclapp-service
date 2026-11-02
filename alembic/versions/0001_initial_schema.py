"""initial schema: users, api_keys, clouds, jobs, clusters, shares

Revision ID: 0001
Revises:
Create Date: 2026-11-02

Generated with ``alembic revision --autogenerate`` and then edited by hand:
readable revision id, enum types dropped on downgrade (autogenerate forgets them),
server defaults for columns the worker may insert with raw SQL.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

VEC3 = postgresql.ARRAY(sa.DOUBLE_PRECISION(), dimensions=1)
# clock_timestamp(): the real insert time, not the transaction start (see models._created_at)
NOW = sa.text("clock_timestamp()")

job_type = sa.Enum("summarize", "downsample", "cluster", name="job_type")
job_status = sa.Enum("queued", "running", "done", "failed", name="job_status")
share_role = sa.Enum("viewer", "editor", name="share_role")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("key_prefix", sa.String(length=12), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])

    op.create_table(
        "clouds",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("format", sa.String(length=10), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("has_intensity", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("point_count", sa.BigInteger(), nullable=True),
        sa.Column("bbox_min", VEC3, nullable=True),
        sa.Column("bbox_max", VEC3, nullable=True),
        sa.Column("centroid", VEC3, nullable=True),
        sa.Column("derived_from_cloud_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["derived_from_cloud_id"], ["clouds.id"]),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("format IN ('pcd', 'txt')", name="ck_clouds_format"),
    )
    # Listing per owner, newest first; id breaks ties for the keyset cursor.
    op.create_index(
        "ix_clouds_owner_created",
        "clouds",
        ["owner_id", sa.literal_column("created_at DESC"), sa.literal_column("id DESC")],
    )
    # Partial index: only live clouds, which is what nearly every query wants.
    op.create_index(
        "ix_clouds_owner_live",
        "clouds",
        ["owner_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("cloud_id", sa.UUID(), nullable=False),
        sa.Column("requested_by", sa.UUID(), nullable=False),
        sa.Column("type", job_type, nullable=False),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", job_status, nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("worker_id", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_cloud_id", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(["cloud_id"], ["clouds.id"]),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["result_cloud_id"], ["clouds.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    # The worker's claim: WHERE status = 'queued' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED
    op.create_index("ix_jobs_status_created", "jobs", ["status", "created_at"])
    # Job list per cloud, keyset-paginated like clouds.
    op.create_index("ix_jobs_cloud_created", "jobs", ["cloud_id", "created_at", "id"])

    op.create_table(
        "clusters",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("index", sa.Integer(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("bbox_min", VEC3, nullable=False),
        sa.Column("bbox_max", VEC3, nullable=False),
        sa.Column("centroid", VEC3, nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "index", name="uq_clusters_job_index"),
    )

    op.create_table(
        "shares",
        sa.Column("cloud_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("role", share_role, nullable=False),
        sa.Column("granted_by", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(["cloud_id"], ["clouds.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["granted_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("cloud_id", "user_id"),
    )
    op.create_index("ix_shares_user", "shares", ["user_id"])


def downgrade() -> None:
    op.drop_table("shares")
    op.drop_table("clusters")
    op.drop_table("jobs")
    op.drop_table("clouds")
    op.drop_table("api_keys")
    op.drop_table("users")
    # create_table made these implicitly; drop_table does not remove them.
    bind = op.get_bind()
    share_role.drop(bind, checkfirst=True)
    job_status.drop(bind, checkfirst=True)
    job_type.drop(bind, checkfirst=True)
