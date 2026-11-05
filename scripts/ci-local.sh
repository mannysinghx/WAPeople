#!/bin/sh
# Run this repo's CI on this machine, in Linux containers, against a clean checkout of one commit.
# GitHub CI isn't used (see the owner's standing rule); record the result in docs/OPS_LOG.md.
#
#   scripts/ci-local.sh            # the commit at HEAD
#   scripts/ci-local.sh <commit>   # any other commit
#
# Steps: build the production image, byte-compile, run the full test suite against a throwaway
# Postgres (same major version as production), and audit dependencies for known vulnerabilities.
# Nothing here touches the wapeople_pgdata volume, the local compose stack, or production.
set -eu

COMMIT=$(git rev-parse --verify "${1:-HEAD}^{commit}")
SHORT=$(git rev-parse --short "$COMMIT")
TAG="wapeople-ci-$SHORT-$$"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/wapeople-ci.XXXXXX")

cleanup() {
  docker rm -f "$TAG-db" >/dev/null 2>&1 || true
  docker network rm "$TAG" >/dev/null 2>&1 || true
  docker rmi -f "$TAG" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

echo "== CI for $SHORT: $(git log -1 --format=%s "$COMMIT")"
git archive "$COMMIT" | tar -x -C "$WORK"

echo "== build image"
docker build -q -t "$TAG" "$WORK/app" >/dev/null

echo "== start throwaway database"
docker network create "$TAG" >/dev/null
docker run -d --name "$TAG-db" --network "$TAG" -e POSTGRES_USER=ci -e POSTGRES_PASSWORD=ci -e POSTGRES_DB=ci \
  --tmpfs /var/lib/postgresql:rw postgres:18-alpine >/dev/null
tries=0
until docker exec "$TAG-db" pg_isready -U ci -d ci >/dev/null 2>&1; do
  tries=$((tries + 1))
  [ "$tries" -lt 60 ] || { echo "database did not start"; exit 1; }
  sleep 1
done
sleep 2

URL="postgresql://ci:ci@$TAG-db:5432/ci"
run() { docker run --rm --network "$TAG" -e DATABASE_URL="$URL" -e TEST_DATABASE_URL="$URL" "$TAG" "$@"; }

echo "== byte-compile"
run sh -c "PYTHONPYCACHEPREFIX=/tmp/pyc python -m compileall -q wapeople tests"

echo "== migrations apply to an empty database"
run python -m wapeople.migrate

echo "== tests (nothing may be skipped: integration tests need the database above)"
RESULT=$(run python -m pytest -q -p no:cacheprovider tests 2>&1) || { echo "$RESULT"; exit 1; }
echo "$RESULT" | tail -3
echo "$RESULT" | grep -q "skipped" && { echo "FAILED: tests were skipped"; exit 1; }

echo "== dependency audit"
docker run --rm -u root "$TAG" sh -c "pip install -q pip-audit 2>/dev/null && pip-audit -r requirements.txt"

echo "== PASS $SHORT"
