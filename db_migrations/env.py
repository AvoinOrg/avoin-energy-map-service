from logging.config import fileConfig
import os

from sqlalchemy import engine_from_config, pool
from alembic import context
from alembic.operations.ops import ModifyTableOps, UpgradeOps, DowngradeOps

# Import model metadata
from db.models import Base

# Alembic Config object (from alembic.ini)
config = context.config

EXCLUDED_TABLES = {
    "spatial_ref_sys"
}
# Exclude specific tables from autogeneration
def include_object(object, name, type_, reflected, compare_to):
    if type_ == "table" and name in EXCLUDED_TABLES:
        return False
    return True


def _set_if_not_exists_recursive(ops_container):
    """
    Walk Alembic op tree and set if_not_exists=True on create-table and create-index ops.
    Handles nested containers like ModifyTableOps.
    """
    if not hasattr(ops_container, "ops"):
        return
    for op in ops_container.ops:
        # Recurse into nested op containers (e.g., ModifyTableOps)
        if hasattr(op, "ops"):
            _set_if_not_exists_recursive(op)
            
        # Set the flag when the attribute exists (Alembic >= 1.11)
        if hasattr(op, "if_not_exists"):
            op.if_not_exists = True

def process_revision_directives(context, revision, directives):
    # Called during "alembic revision --autogenerate"
    script = directives[0]
    if hasattr(script, "upgrade_ops") and isinstance(script.upgrade_ops, (UpgradeOps, ModifyTableOps)):
        _set_if_not_exists_recursive(script.upgrade_ops)
    if hasattr(script, "downgrade_ops") and isinstance(script.downgrade_ops, (DowngradeOps, ModifyTableOps)):
        _set_if_not_exists_recursive(script.downgrade_ops)

# Override sqlalchemy.url with env vars, if provided
for k in ("PGUSER", "PGPASSWORD", "PGHOST", "PGPORT", "PGDATABASE"):
    if os.getenv(k):
        config.set_main_option(k, os.getenv(k))

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set target metadata for 'autogenerate'
target_metadata = Base.metadata

def run_migrations_offline():
    """Run migrations without DB connection (outputs SQL)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
        process_revision_directives=process_revision_directives,
    )

    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online():
    """Run migrations with DB connection (applies to DB)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=include_object,
            process_revision_directives=process_revision_directives,
        )

        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
