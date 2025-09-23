#!/usr/bin/env bash
set -euo pipefail

# Run Alembic migrations
echo "Running Alembic migrations..."
# echo "gonna run alembic upgrade head"
alembic upgrade head

# Choose ETL mode
# MODE="${ETL_MODE:-init}"
# UPDATED_SINCE="${UPDATED_SINCE:-}"

# if [[ -z "${RYHTI_BUILDINGS_URL:-}" ]]; then
#   echo "WARNING: RYHTI_BUILDINGS_URL not set; ETL will skip building ingest."
# fi

# if [[ "$MODE" == "init" ]]; then
#   echo "Starting ETL (init)..."
#   python etl/etl_sqlalchemy.py --mode init --limit "${LIMIT:-0}"
# else
#   echo "Starting ETL (delta) since=${UPDATED_SINCE:-<default>}"
#   if [[ -n "$UPDATED_SINCE" ]]; then
#     python etl/etl_sqlalchemy.py --mode delta --updated-since "$UPDATED_SINCE"
#   else
#     python etl/etl_sqlalchemy.py --mode delta
#   fi
# fi

# Keep container alive
tail -f /dev/null
