# Dockerfile
FROM python:3.12-slim

# System deps for psycopg2 and friends
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# install poetry using pip
RUN pip install --no-cache-dir poetry

ENV POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Copy project metadata first to leverage Docker layer caching
COPY pyproject.toml poetry.lock* /app/

# Install deps (no root package install)
RUN poetry install --no-root

# Now copy the actual source code
COPY src/db /app/db
COPY src/etl /app/etl
COPY db_migrations /app/db_migrations
COPY alembic.ini /app/alembic.ini

# Entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["/entrypoint.sh"]
