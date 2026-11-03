# pclapp-service

[![CI](https://github.com/HatemSelim94/pclapp-service/actions/workflows/ci.yml/badge.svg)](https://github.com/HatemSelim94/pclapp-service/actions/workflows/ci.yml)

A small, production-shaped REST backend around the [PCL-App](https://github.com/HatemSelim94/PCL-App)
Python package: upload point clouds, queue processing jobs, store results and metadata in
PostgreSQL, fetch them over HTTP.

**Stack:** Python 3.12 · FastAPI + Pydantic v2 · PostgreSQL 16 · SQLAlchemy 2 (sync, psycopg 3) + Alembic ·
a worker process that claims jobs from a `jobs` table with `FOR UPDATE SKIP LOCKED` · API keys ·
pytest against a real Postgres · ruff + mypy (strict) · Docker Compose · GitHub Actions.

## What it does

A client uploads a point cloud (`.pcd` or `.txt`) and gets a cloud id back. The upload itself
queues a `summarize` job; the client can queue `downsample` and `cluster` jobs on top. A separate
worker process picks jobs out of the `jobs` table, runs the geometry through `pclapp` (PCL
underneath) and writes the results back: point count, bounding box and centroid onto the cloud
row, a new *derived* cloud for a downsample, one `clusters` row per cluster. Clients poll
`GET /jobs/{id}` or follow a job live over server-sent events. Owners can share a cloud with
other users as viewer or editor; everyone else gets a 404.

The HTML page at `/` is the whole API in one screen — here with two clouds, a downsample
running and a cluster job waiting behind it:

![Clouds, one job running, one queued](docs/page-running.png)

…and a few seconds later, both finished: the cluster job found the three objects in the sample
scene (2000, 1200 and 400 points), the downsample produced a new cloud that is listed like any
other and can itself be processed again:

![Both jobs done, derived cloud in the list](docs/page-done.png)

### Architecture

Two processes share one database and one file volume. The API never touches PCL and the worker
never touches HTTP; the `jobs` table is the only thing between them.

```mermaid
flowchart TB
    client["client<br/>curl · browser page · any HTTP client"]
    subgraph api["api  (FastAPI, uvicorn)"]
        routers["routers<br/>users · clouds · jobs · shares"]
        access["access rules<br/>owner / editor / viewer / 404"]
        storage["storage<br/>tmp → rename → commit"]
    end
    subgraph pg["PostgreSQL 16"]
        tables[("users · api_keys · clouds<br/>jobs · clusters · shares")]
    end
    subgraph worker["worker  (plain process, N instances)"]
        loop["claim – run – finish loop<br/>FOR UPDATE SKIP LOCKED"]
        runners["runners<br/>summarize · downsample · cluster"]
        pclapp["pclapp (PCL)"]
    end
    volume[("/data volume<br/>clouds/&lt;uuid&gt;.pcd")]

    client -- "Bearer pk_…" --> routers --> access --> tables
    routers --> storage --> volume
    loop <--> tables
    loop --> runners --> pclapp
    runners <--> volume
    client -. "GET /jobs/{id}/events (SSE)" .-> routers
```

### Life of a job

```mermaid
sequenceDiagram
    autonumber
    participant C as client
    participant A as api
    participant DB as PostgreSQL
    participant FS as /data
    participant W as worker

    C->>A: POST /clouds (multipart file)
    A->>FS: stream to tmp/, rename to clouds/<uuid>.pcd
    A->>DB: INSERT cloud + INSERT job(summarize, queued) — one transaction
    A-->>C: 201 {cloud}
    C->>A: POST /clouds/{id}/jobs {type: cluster, params}
    A->>DB: INSERT job(cluster, queued)
    A-->>C: 202 {job}

    loop every poll interval
        W->>DB: SELECT … WHERE status='queued' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED
        DB-->>W: one row (or none) — other workers skip it
        W->>DB: UPDATE status='running', attempts+1, worker_id, then COMMIT
        W->>FS: read cloud file
        Note over W: pclapp runs here, with no transaction open
        W->>DB: UPDATE status='done' + INSERT clusters / derived cloud, then COMMIT
    end

    C->>A: GET /jobs/{id}  (or /events)
    A->>DB: SELECT job + clusters
    A-->>C: {status: done, clusters: [...]}
```

### Job states

```mermaid
stateDiagram-v2
    [*] --> queued: POST /clouds or POST /clouds/{id}/jobs
    queued --> running: worker claims it (SKIP LOCKED), attempts += 1
    running --> done: result rows written
    running --> queued: error, attempts < max_attempts (error kept on the row)
    running --> failed: error, no attempts left
    running --> failed: permanent — bad params, cloud deleted, file missing
    done --> [*]
    failed --> [*]
```

### Data model

```mermaid
erDiagram
    users ||--o{ api_keys : "has"
    users ||--o{ clouds : "owns"
    users ||--o{ shares : "is granted"
    clouds ||--o{ shares : "shared via"
    clouds ||--o{ jobs : "processed by"
    clouds |o--o{ clouds : "derived_from"
    jobs |o--o| clouds : "result_cloud"
    jobs ||--o{ clusters : "found"

    users {
        uuid id PK
        text name UK
        timestamptz created_at
    }
    api_keys {
        uuid id PK
        uuid user_id FK
        text key_hash UK
        text key_prefix
        timestamptz revoked_at
    }
    clouds {
        uuid id PK
        uuid owner_id FK
        text name
        text format
        text path
        bigint point_count
        float8_3 bbox_min
        float8_3 bbox_max
        float8_3 centroid
        uuid derived_from_cloud_id FK
        timestamptz deleted_at
    }
    jobs {
        uuid id PK
        uuid cloud_id FK
        job_type type
        jsonb params
        job_status status
        int attempts
        int max_attempts
        text error
        text worker_id
        uuid result_cloud_id FK
    }
    clusters {
        bigint id PK
        uuid job_id FK
        int index
        int size
        float8_3 bbox_min
        float8_3 bbox_max
        float8_3 centroid
    }
    shares {
        uuid cloud_id PK
        uuid user_id PK
        share_role role
        uuid granted_by FK
    }
```

## Status

| Milestone | | |
|---|---|---|
| M1 | skeleton, settings, schema + first migration, `/healthz`, users and API keys | done |
| M2 | cloud upload / list / get / download / delete, keyset pagination, access rules | done |
| M3 | jobs API, worker with `FOR UPDATE SKIP LOCKED`, `summarize` / `downsample` / `cluster` runners | done |
| M4 | shares and access rules | done |
| M5 | Docker Compose (db, migrate, api, worker) + CI with the end-to-end smoke test | done |
| M6 | `GET /jobs/{id}/events` (server-sent events), `pclapp-cleanup` command | done |
| M7 | a plain HTML page at `/` listing clouds and jobs, following running jobs live | done |

## Run it

```bash
git clone https://github.com/HatemSelim94/pclapp-service && cd pclapp-service
echo "PCLAPP_ADMIN_KEY=$(openssl rand -hex 16)" > .env
docker compose up --build          # db → migrate → api :8000 + worker
PCLAPP_ADMIN_KEY=$(cut -d= -f2 .env) scripts/smoke.sh
```

The worker image compiles `pclapp` against PCL (a few minutes the first time). `scripts/smoke.sh`
creates a user and key, uploads `tests/data/sample_objects.pcd`, waits for the summarize job
(3630 points), runs a cluster job (2000 / 1200 / 400 points) and a downsample job, and downloads
the derived cloud. CI runs the same script after `docker compose up`.

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

```bash
# start a worker (needs pclapp, i.e. PCL: pip install -e .[pcl]); then queue a job
uv run pclapp-worker                     # or: python -m worker.main [--once]
curl -s -X POST localhost:8000/clouds/<cloud-id>/jobs -H "Authorization: Bearer $KEY" \
     -H "content-type: application/json" \
     -d '{"type":"cluster","params":{"tolerance":0.15,"min_size":50,"max_size":5000}}'
curl -s localhost:8000/jobs/<job-id> -H "Authorization: Bearer $KEY"
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
| `POST` | `/clouds/{id}/jobs` | owner/editor · `{"type": "downsample", "params": {"leaf_size": 0.05}}` or `{"type": "cluster", "params": {"tolerance", "min_size", "max_size"}}` → 202 |
| `GET` | `/jobs/{id}` | status, attempts, timings, error, `result_cloud_id`, `clusters` |
| `GET` | `/clouds/{id}/jobs?limit&cursor` | a cloud's jobs, newest first |
| `GET` | `/jobs/{id}/events` | server-sent events: a `status` event per change, `end` when final, `timeout` after `PCLAPP_SSE_MAX_SECONDS` |
| `GET` `POST` | `/clouds/{id}/shares` | owner only · list / grant `{"user_id", "role": "viewer"\|"editor"}`; granting again changes the role |
| `DELETE` | `/clouds/{id}/shares/{user_id}` | owner only · revoke |
| `GET` | `/healthz` | `{"db": "ok", "data_dir": "ok"}` or 503 |
| `GET` | `/` | the HTML page (no framework, ~80 lines): paste a key, list clouds, upload, run jobs, watch them finish |

Access, as one table (`app/access.py`; `tests/test_access.py` runs every cell over HTTP):

| action | owner | editor | viewer | stranger |
|---|---|---|---|---|
| read, download, list jobs | ✓ | ✓ | ✓ | 404 |
| create job | ✓ | ✓ | 403 | 404 |
| list/grant/revoke shares, delete | ✓ | 403 | 403 | 404 |

A shared cloud shows up in the grantee's `GET /clouds`; its jobs and results are visible to
anyone who can read the cloud. Soft-deleted clouds are 404 for everyone, owner included.

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

## Compose and CI

`docker-compose.yml` has four services: `db` (postgres:16 with `pg_isready` healthcheck),
`migrate` (the api image running `alembic upgrade head` once `db` is healthy), `api` and
`worker` (both wait for `migrate` to complete). Migrations as a one-shot service rather than
inside the api's start command means the api and the worker never race each other to the
schema, and `docker compose up --scale worker=3` just works. `stop_grace_period: 60s` gives a
running job time to finish after SIGTERM.

`.github/workflows/ci.yml`, two jobs: **test** (ruff, mypy, pytest against a `postgres:16`
service container, `alembic check`) and **compose** (build both images with layer caching,
`docker compose up`, `scripts/smoke.sh`, `docker compose down`). The compose job is where the
real PCL code path is exercised; the unit tests use the fake processor.

## Jobs and the worker

Uploading a cloud enqueues a `summarize` job; clients enqueue `downsample` and `cluster`.
Parameters are validated once, in Pydantic (`app/schemas.py`): an unknown type or a
`leaf_size` of 0 is a 422 at the API, and the worker re-validates the stored JSONB with the
same models before running, so a bad row can only ever fail, never crash the loop.

The worker (`worker/main.py`) is a plain process polling the `jobs` table:

```sql
-- claim: one short transaction
SELECT * FROM jobs WHERE status = 'queued'
ORDER BY created_at, id LIMIT 1
FOR UPDATE SKIP LOCKED;
UPDATE jobs SET status = 'running', attempts = attempts + 1, worker_id = $1, started_at = now()
WHERE id = $2;
COMMIT;
-- run the job with no transaction open (PCL may take seconds)
-- finish: a second short transaction writes the result and status = 'done',
-- or the error and status = 'queued' (attempts < max_attempts) or 'failed'
```

`FOR UPDATE` locks the row being taken; `SKIP LOCKED` lets a second worker step over it
instead of waiting. N workers share the queue with no coordinator and nothing is claimed
twice — `tests/test_worker.py` runs two workers in threads over twenty jobs and asserts
`attempts == 1` on every one. SIGTERM finishes the job in hand, then exits.

| job | does | writes |
|---|---|---|
| `summarize` | `pclapp.load(...).summary()` | `point_count`, `bbox_min/max`, `centroid` on the cloud row |
| `downsample` | `pclapp.voxel_downsample(cloud, leaf_size)` → new PCD file | a new `clouds` row with `derived_from_cloud_id`; `jobs.result_cloud_id` |
| `cluster` | `pclapp.euclidean_cluster(cloud, tolerance, min_size, max_size)` | one `clusters` row per cluster, largest first |

Each runner is split into `compute()` (outside any transaction, returns a plain result) and
`apply()` (inside the finishing transaction, writes rows). A derived cloud's file is moved into
place before its row is committed, like an upload, and removed again if the commit fails.

`GET /jobs/{id}/events` streams the job as server-sent events. It polls the row once a second in
its own short session (the request's session is not held open), emits `status` whenever
`(status, attempts)` changes, a `: keepalive` comment in between, and `end` when the job is done
or failed. Polling rather than `LISTEN/NOTIFY` keeps the worker unaware of who is watching; at
this scale a one-second poll is free. The page at `/` reads it with `fetch` because `EventSource`
cannot send an `Authorization` header.

`pclapp-cleanup [--grace-hours 24] [--dry-run]` removes what the request path leaves behind on
purpose: files of soft-deleted clouds, orphan files in `clouds/` with no row (a crash between
rename and commit), and leftovers in `tmp/`. Anything younger than the grace period is left alone
so nothing in flight is touched; rows are never deleted. `docker compose run --rm api pclapp-cleanup`.

`pclapp` sits behind a four-method `Processor` protocol (`worker/processor.py`). The real
implementation is `worker/pclapp_processor.py`; tests use a pure-Python fake
(`tests/fake_processor.py`) so the queue, retry and concurrency tests run without PCL.
`python -m worker.main --processor tests.fake_processor:FakeProcessor` runs the fake for real.

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
- **Retry by putting the row back.** A failed attempt with attempts left sets `status = 'queued'` again (the error stays on the row for inspection); the claim query picks it up like any other job. Permanent failures — bad params, deleted cloud — skip the retries.
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
docker/     api.Dockerfile (python:3.12-slim), worker.Dockerfile (ubuntu:24.04 + libpcl, two stages)
scripts/    smoke.sh — end-to-end check against a running stack
docs/       screenshots used above
```
