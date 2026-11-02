# pclapp-service

A small, production-shaped REST backend around the [PCL-App](https://github.com/HatemSelim94/PCL-App)
Python package: upload point clouds, queue processing jobs, store results and metadata in
PostgreSQL, fetch them over HTTP.

**Stack:** Python 3.12 · FastAPI + Pydantic v2 · PostgreSQL 16 · SQLAlchemy 2 (sync, psycopg 3) + Alembic ·
a worker process that claims jobs from a `jobs` table with `FOR UPDATE SKIP LOCKED` · API keys ·
pytest against a real Postgres · ruff + mypy (strict) · Docker Compose · GitHub Actions.

## Status

| Milestone | | |
|---|---|---|
| M1 | skeleton, settings, schema + first migration, `/healthz`, users and API keys | done |
| M2 | cloud upload / list / get / download / delete | — |
| M3 | jobs table, worker, `summarize` / `downsample` / `cluster` runners | — |
| M4 | shares and access rules | — |
| M5 | Docker Compose + CI | — |

## Run it (development)

```bash
# Postgres 16 somewhere, e.g.
docker run -d --name pclapp-db -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16
createdb -h localhost -U postgres pclapp

uv sync --group dev                      # or: pip install -e .[dev]
cp .env.example .env                     # edit PCLAPP_ADMIN_KEY at least
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

```bash
# create a user and a key with the admin key, then use the key
curl -s -X POST localhost:8000/users -H "Authorization: Bearer $PCLAPP_ADMIN_KEY" \
     -H "content-type: application/json" -d '{"name":"hatem"}'
curl -s -X POST localhost:8000/users/<user-id>/api-keys -H "Authorization: Bearer $PCLAPP_ADMIN_KEY" \
     -H "content-type: application/json" -d '{"label":"laptop"}'
curl -s localhost:8000/me -H "Authorization: Bearer pk_..."
```

## Tests

Tests run against a real PostgreSQL (`PCLAPP_TEST_DATABASE_URL`, default
`postgresql+psycopg://postgres:postgres@127.0.0.1:5432/pclapp_test`). There is no SQLite
stand-in; the point is Postgres.

```bash
createdb -h localhost -U postgres pclapp_test
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Two fixture tiers: most tests run inside one transaction that is rolled back (fast, isolated);
tests that need rows visible from other connections — two workers racing on `SKIP LOCKED`,
the migration round-trip — commit for real and truncate afterwards.

## Schema

```
users      id, name (unique), created_at
api_keys   id, user_id, key_hash (sha256, unique), key_prefix, label, created_at, revoked_at
clouds     id, owner_id, name, format ('pcd'|'txt'), path, size_bytes, has_intensity,
           point_count, bbox_min[3], bbox_max[3], centroid[3]      -- filled by 'summarize'
           derived_from_cloud_id, created_at, deleted_at            -- soft delete
jobs       id, cloud_id, requested_by, type (summarize|downsample|cluster), params jsonb,
           status (queued|running|done|failed), attempts, max_attempts, error, worker_id,
           created_at, started_at, finished_at, result_cloud_id
clusters   id, job_id, index, size, bbox_min[3], bbox_max[3], centroid[3]   unique (job_id, index)
shares     cloud_id, user_id, role (viewer|editor), granted_by, created_at  pk (cloud_id, user_id)
```

Indexes, each with its one-sentence reason:

- `jobs (status, created_at)` — the worker's claim query: `WHERE status='queued' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED`.
- `clouds (owner_id, created_at DESC, id DESC)` — listing newest-first per owner; `id` breaks ties so the keyset cursor `(created_at, id)` never skips or repeats rows.
- `clouds (owner_id) WHERE deleted_at IS NULL` — partial index; almost every query wants live clouds only, and the index stays small as deletes pile up.
- `shares (user_id)` — "which clouds are shared with me" joins through here.

## Decisions

- **Postgres as the job queue, not Celery/Redis.** One fewer service to run and explain; `FOR UPDATE SKIP LOCKED` gives exactly-once claiming across workers, and a concurrency test proves it.
- **Sync SQLAlchemy.** Endpoints are plain `def`, so FastAPI runs them in its threadpool; the worker is a plain loop. Async would add a second set of APIs for no throughput we need.
- **SHA-256 for API keys, not bcrypt.** Keys are 32 random bytes; bcrypt's slowness defends low-entropy passwords against guessing, which does not apply here, and it would cost latency on every request.
- **One FK for lineage.** `jobs.result_cloud_id` points at the derived cloud; `clouds` does not point back at the job, which keeps the two tables from depending on each other.
- **Soft delete.** `DELETE /clouds/{id}` sets `deleted_at`; a separate cleanup command removes files, so the request path never deletes from disk.
- **Files and rows.** The filesystem is not transactional, so: write to a temp path → insert row → commit → rename into place; on failure the temp file is removed. Orphans from a crash between commit and rename are what the cleanup command sweeps.
- **404, not 403, for clouds you cannot see.** A 403 confirms the id exists.

## Layout

```
app/        FastAPI app: config, db, models, schemas, auth, routers/
worker/     claim–run–finish loop and the job runners (the only code importing pclapp)
alembic/    migrations, hand-reviewed
tests/      pytest, against Postgres
docker/     Dockerfiles for api and worker
```
