#!/usr/bin/env bash
set -euo pipefail

# Run Alembic migrations
echo "Running Alembic migrations..."
alembic upgrade head

# @todo: Run ETL scripts

# Keep container alive
tail -f /dev/null
