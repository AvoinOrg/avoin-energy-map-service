#!/usr/bin/env bash
set -euo pipefail

# echo "Waiting for Postgres at $PGHOST:$PGPORT ..."
# until pg_isready -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" >/dev/null 2>&1; do
#   sleep 1
# done
# echo "Postgres is ready."

# Run Alembic migrations
echo "Running Alembic migrations..."
echo "later"
# alembic upgrade head

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
