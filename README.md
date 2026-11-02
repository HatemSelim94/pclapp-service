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
| M2 | cloud upload / list / get / download / delete, keyset pagination, access rules | done |
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

# upload a cloud (enqueues a 'summarize' job), list, download
export KEY=pk_...
curl -s -X POST localhost:8000/clouds -H "Authorization: Bearer $KEY" \
     -F file=@tests/data/sample_objects.pcd -F name="sample scene"
curl -s "localhost:8000/clouds?limit=20&q=sample" -H "Authorization: Bearer $KEY"
curl -s localhost:8000/clouds/<cloud-id>/download -H "Authorization: Bearer $KEY" -o scene.pcd
```

Interactive docs: `http://localhost:8000/docs`.

## API

Every endpoint except `/healthz` needs `Authorization: Bearer <key>`. Errors are `{"detail": ...}`.

| | | |
|---|---|---|
| `POST` | `/users` | admin key · create a user |
| `POST` | `/users/{id}/api-keys` | admin key · issue a key; the key is in the response once, the DB keeps its hash |
| `GET` `DELETE` | `/users/{id}/api-keys[/{key_id}]` | admin key · list / revoke |
| `GET` | `/me` | who owns this key |
| `POST` | `/clouds` | multipart `file` (`.pcd`/`.txt`), `name?`, `intensity?` → 201; enqueues `summarize` |
| `GET` | `/clouds?limit&cursor&q` | clouds you own or that are shared with you; keyset pagination (below) |
| `GET` | `/clouds/{id}` | metadata incl. summary (once computed) and lineage |
| `GET` | `/clouds/{id}/download` | the file |
| `DELETE` | `/clouds/{id}` | owner only · soft delete |
| `GET` | `/healthz` | `{"db": "ok", "data_dir": "ok"}` or 503 |

Access, as one table (`app/access.py`):

| action | owner | editor | viewer | stranger |
|---|---|---|---|---|
| read, download | ✓ | ✓ | ✓ | 404 |
| create job | ✓ | ✓ | 403 | 404 |
| share, delete | ✓ | 403 | 403 | 404 |

### Keyset pagination

`GET /clouds` returns `{"items": [...], "next_cursor": "..."}`; pass `next_cursor` back as
`cursor` until it is `null`. The cursor encodes the `(created_at, id)` of the last row, and the
next page is

```sql
SELECT ... FROM clouds
WHERE deleted_at IS NULL AND (owner_id = :me OR EXISTS (shared with :me))
  AND (created_at, id) < (:cursor_created_at, :cursor_id)
ORDER BY created_at DESC, id DESC
LIMIT :limit + 1;          -- the extra row says whether there is a next page
```

Why not `OFFSET`: a row inserted between two requests shifts every later page, so the client
sees a row twice or misses one, and Postgres still reads and discards the skipped rows.
The tuple comparison is served straight from `ix_clouds_owner_created`. `id` is part of the
key because `created_at` can tie; the tuple cannot, so a walk never skips or repeats
(`tests/test_clouds.py::test_keyset_pagination_is_stable_across_inserts` inserts rows mid-walk).

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
- **404, not 403, for clouds you cannot see.** A 403 confirms the id exists. Users who *can* see a cloud but lack a right get 403.
- **`created_at` defaults to `clock_timestamp()`, not `now()`.** `now()` is the transaction start, so rows inserted in one transaction would tie; the keyset tie-breaker would cope, but "newest first" should mean what it says.

## Layout

```
app/        FastAPI app: config, db, models, schemas, auth, routers/
worker/     claim–run–finish loop and the job runners (the only code importing pclapp)
alembic/    migrations, hand-reviewed
tests/      pytest, against Postgres
docker/     Dockerfiles for api and worker
```
