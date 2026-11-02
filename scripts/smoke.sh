#!/usr/bin/env bash
# End-to-end smoke test against a running stack (docker compose up).
#
#   scripts/smoke.sh [base-url]        default http://localhost:8000
#
# Needs PCLAPP_ADMIN_KEY in the environment (the one the stack was started with), curl,
# and python3 for JSON. Creates a user and a key, uploads the sample cloud, waits for the
# automatic summarize job, then runs a cluster job and checks the three known objects.
# Exits non-zero on the first mismatch. CI runs this after `docker compose up`.

set -euo pipefail

BASE="${1:-http://localhost:8000}"
ADMIN="${PCLAPP_ADMIN_KEY:?set PCLAPP_ADMIN_KEY}"
# Overridable so the script can be run against the fake processor with a text sample.
SAMPLE="${SMOKE_SAMPLE:-$(dirname "$0")/../tests/data/sample_objects.pcd}"
EXPECT_POINTS="${SMOKE_EXPECT_POINTS:-3630}"
EXPECT_SIZES="${SMOKE_EXPECT_SIZES:-[2000, 1200, 400]}"

json() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {}, {'d': d}))" "$1"; }
step() { printf '\n== %s\n' "$*"; }

step "wait for /healthz"
for _ in $(seq 1 60); do
  if curl -fsS "$BASE/healthz" >/dev/null 2>&1; then break; fi
  sleep 2
done
curl -fsS "$BASE/healthz"; echo

step "create user and key"
USER_ID=$(curl -fsS -X POST "$BASE/users" -H "Authorization: Bearer $ADMIN" \
  -H "content-type: application/json" -d "{\"name\":\"smoke-$RANDOM\"}" | json "d['id']")
KEY=$(curl -fsS -X POST "$BASE/users/$USER_ID/api-keys" -H "Authorization: Bearer $ADMIN" \
  -H "content-type: application/json" -d '{"label":"smoke"}' | json "d['key']")
AUTH=(-H "Authorization: Bearer $KEY")
curl -fsS "$BASE/me" "${AUTH[@]}"; echo

step "upload sample cloud"
CLOUD=$(curl -fsS -X POST "$BASE/clouds" "${AUTH[@]}" -F "file=@$SAMPLE" -F "name=sample scene")
CLOUD_ID=$(echo "$CLOUD" | json "d['id']")
echo "cloud $CLOUD_ID"

wait_job() {  # wait_job <job-id> -> prints final job JSON, fails unless status == done
  local id=$1 body status
  for _ in $(seq 1 60); do
    body=$(curl -fsS "$BASE/jobs/$id" "${AUTH[@]}")
    status=$(echo "$body" | json "d['status']")
    case "$status" in
      done) echo "$body"; return 0 ;;
      failed) echo "job $id failed: $(echo "$body" | json "d['error']")" >&2; return 1 ;;
    esac
    sleep 1
  done
  echo "job $id still $status after 60s" >&2; return 1
}

step "summarize job (enqueued by the upload)"
SUM_JOB=$(curl -fsS "$BASE/clouds/$CLOUD_ID/jobs" "${AUTH[@]}" | json "d['items'][0]['id']")
wait_job "$SUM_JOB" | json "'attempts', d['attempts'], 'worker', d['worker_id']"
POINTS=$(curl -fsS "$BASE/clouds/$CLOUD_ID" "${AUTH[@]}" | json "d['point_count']")
echo "point_count=$POINTS"
[ "$POINTS" = "$EXPECT_POINTS" ] || { echo "expected $EXPECT_POINTS points" >&2; exit 1; }

step "cluster job"
CL_JOB=$(curl -fsS -X POST "$BASE/clouds/$CLOUD_ID/jobs" "${AUTH[@]}" \
  -H "content-type: application/json" \
  -d '{"type":"cluster","params":{"tolerance":0.15,"min_size":50,"max_size":5000}}' | json "d['id']")
SIZES=$(wait_job "$CL_JOB" | json "[c['size'] for c in d['clusters']]")
echo "cluster sizes: $SIZES"
[ "$SIZES" = "$EXPECT_SIZES" ] || { echo "expected $EXPECT_SIZES" >&2; exit 1; }

step "downsample job"
DS_JOB=$(curl -fsS -X POST "$BASE/clouds/$CLOUD_ID/jobs" "${AUTH[@]}" \
  -H "content-type: application/json" -d '{"type":"downsample","params":{"leaf_size":0.1}}' | json "d['id']")
DERIVED=$(wait_job "$DS_JOB" | json "d['result_cloud_id']")
DERIVED_POINTS=$(curl -fsS "$BASE/clouds/$DERIVED" "${AUTH[@]}" | json "d['point_count']")
echo "derived cloud $DERIVED has $DERIVED_POINTS points"
python3 -c "import sys; n=int(sys.argv[1]); sys.exit(0 if 0 < n < int(sys.argv[2]) else 1)" "$DERIVED_POINTS" "$EXPECT_POINTS"
curl -fsS "$BASE/clouds/$DERIVED/download" "${AUTH[@]}" -o /tmp/derived.pcd
[ -s /tmp/derived.pcd ] || { echo "download is empty" >&2; exit 1; }

step "list shows both clouds"
COUNT=$(curl -fsS "$BASE/clouds" "${AUTH[@]}" | json "len(d['items'])")
[ "$COUNT" = "2" ] || { echo "expected 2 clouds, got $COUNT" >&2; exit 1; }

printf '\nsmoke test passed\n'
