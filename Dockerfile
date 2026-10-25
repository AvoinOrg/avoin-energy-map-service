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

# Now copy the actual source code and versioned ETL reference assets
COPY src /app/src
COPY assets /app/assets
COPY db_migrations /app/db_migrations
COPY alembic.ini /app/alembic.ini

# Entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app:/app/src
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl --fail --silent http://127.0.0.1:8000/healthz || exit 1

ENTRYPOINT ["/entrypoint.sh"]
