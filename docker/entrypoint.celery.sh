#!/usr/bin/env bash
set -euo pipefail

POSTGRES_HOST="${POSTGRES_HOST:-postgres}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
REDIS_HOST="${REDIS_HOST:-redis}"
REDIS_PORT="${REDIS_PORT:-6379}"

echo "[celery] waiting for postgres ${POSTGRES_HOST}:${POSTGRES_PORT} and redis ${REDIS_HOST}:${REDIS_PORT} ..."
until nc -z "${POSTGRES_HOST}" "${POSTGRES_PORT}"; do sleep 1; done
until nc -z "${REDIS_HOST}" "${REDIS_PORT}"; do sleep 1; done
echo "[celery] dependencies are up."

echo "[celery] starting: $*"
exec "$@"
