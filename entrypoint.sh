#!/usr/bin/env bash
set -euo pipefail

# Migration is an explicit operator step; serving containers may be read-only.
if [[ "${MIGRATE_ON_START:-false}" == "true" ]]; then
  alembic upgrade head
fi

if [[ $# -gt 0 ]]; then
  exec "$@"
fi
exec uvicorn src.api.main:app --log-config /app/src/api/logging.json --host 0.0.0.0 --port "${PORT:-8000}"
