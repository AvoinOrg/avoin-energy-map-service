import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

def local_db_url():
    # fallback to local postgres if env vars not set
    user = os.getenv("PGUSER", "postgres")
    pwd  = os.getenv("PGPASSWORD", "postgres")
    host = os.getenv("PGHOST", "localhost")
    port = os.getenv("PGPORT", "5432")
    db   = os.getenv("PGDATABASE", "avoin_energy_map_db")
    return f"postgresql+psycopg2://{user}:{pwd}@{host}:{port}/{db}"

ENGINE_LOCAL = create_engine(local_db_url(), future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=ENGINE_LOCAL, autoflush=False, autocommit=False, future=True)

def sandbox_db_url():
    # fallback to local postgres if env vars not set
    user = os.getenv("SANDBOX_PGUSER", "postgres")
    pwd  = os.getenv("SANDBOX_PGPASSWORD", "postgres")
    host = os.getenv("SANDBOX_PGHOST", "localhost")
    port = os.getenv("SANDBOX_PGPORT", "5432")
    db   = os.getenv("SANDBOX_PGDATABASE", "avoin_energy_map_db")
    return f"postgresql+psycopg2://{user}:{pwd}@{host}:{port}/{db}"

ENGINE_SANDBOX = create_engine(sandbox_db_url(), future=True, pool_pre_ping=True)
SessionSandbox = sessionmaker(bind=ENGINE_SANDBOX, autoflush=False, autocommit=False, future=True)
