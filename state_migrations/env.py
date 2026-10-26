"""Online-only entry point; connection supplied by guarded storage tooling."""
from alembic import context

connection = context.config.attributes.get('connection')
if connection is None or context.is_offline_mode():
    raise RuntimeError('Use the guarded personal-state storage entry point')
context.configure(connection=connection, target_metadata=None,
                  version_table_schema='energymap_state_meta',
                  version_table='alembic_version', transactional_ddl=True)
with context.begin_transaction():
    context.run_migrations()
