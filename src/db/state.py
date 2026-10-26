"""Lazy, explicitly configured writer. Never falls back to analytical access."""
from contextlib import contextmanager
from functools import lru_cache
import os

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session


class StateUnavailable(Exception):
    """Sanitized application-state boundary."""


@lru_cache(maxsize=4)
def _engine(dsn):
    return create_engine(dsn, hide_parameters=True, pool_pre_ping=True,
        pool_size=5, max_overflow=0, pool_timeout=3,
        connect_args={'connect_timeout': 3, 'options':
            '-c default_transaction_read_only=off -c statement_timeout=10000 -c lock_timeout=2000 -c search_path=pg_catalog,energymap_state'})


def state_engine():
    try:
        if os.getenv('ENERGY_MAP_STATE_ENABLED', 'false').lower() != 'true':
            raise ValueError
        dsn = os.environ['ENERGY_MAP_STATE_DATABASE_URL']
        url = make_url(dsn)
        if (url.drivername not in ('postgresql', 'postgresql+psycopg2') or
                not url.host or not url.username or not url.database or url.query):
            raise ValueError
        return _engine(dsn)
    except (ValueError, KeyError, SQLAlchemyError):
        raise StateUnavailable from None


MUTABLE_COLUMNS = ('display_name', 'schema_version', 'overrides', 'scenario_inputs',
                   'last_known_summary', 'revision', 'updated_at')
EXPECTED_COLUMNS = {
    'saved_id': 'uuid', 'owner_issuer': 'text', 'owner_subject': 'text',
    'building_key': 'text', 'display_name': 'text', 'schema_version': 'bigint',
    'overrides': 'jsonb', 'scenario_inputs': 'jsonb', 'last_known_summary': 'jsonb',
    'revision': 'bigint', 'created_at': 'timestamp with time zone', 'updated_at': 'timestamp with time zone',
}


SHARE_TABLE = 'energymap_state.energymap_user_building_shares'
SHARE_CHECK = """
 AND has_table_privilege(:s,'SELECT') AND has_table_privilege(:s,'INSERT') AND has_table_privilege(:s,'DELETE')
 AND NOT has_table_privilege(:s,'UPDATE,TRUNCATE,REFERENCES,TRIGGER')
 AND NOT has_any_column_privilege(:s,'UPDATE')
 AND NOT EXISTS(SELECT 1 FROM pg_class WHERE oid=to_regclass(:s) AND relowner=current_user::regrole)
 AND (SELECT count(*) FROM pg_constraint c WHERE c.conrelid=to_regclass(:s) AND c.convalidated AND (
   (c.contype='p' AND pg_get_constraintdef(c.oid)='PRIMARY KEY (saved_id)') OR
   (c.contype='u' AND pg_get_constraintdef(c.oid)='UNIQUE (token_digest)') OR
   (c.contype='c' AND pg_get_constraintdef(c.oid)='CHECK ((octet_length(token_digest) = 32))') OR
   (c.contype='f' AND c.confrelid=to_regclass(:t) AND c.confdeltype='c'
    AND c.conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid=to_regclass(:s) AND attname='saved_id')]::smallint[]
    AND c.confkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid=to_regclass(:t) AND attname='saved_id')]::smallint[])
 ))=4
 AND (SELECT count(*) FROM pg_index i WHERE i.indrelid=to_regclass(:s)
      AND i.indisvalid AND i.indisready AND i.indisunique)=2
 AND (SELECT count(*) FROM pg_attribute WHERE attrelid=to_regclass(:s) AND attnum>0 AND NOT attisdropped)=2
 AND EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid=to_regclass(:s) AND attname='saved_id' AND atttypid='uuid'::regtype AND attnotnull)
 AND EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid=to_regclass(:s) AND attname='token_digest' AND atttypid='bytea'::regtype AND attnotnull)
"""


def check_state(session):
    table = 'energymap_state.energymap_user_buildings'
    privileges = session.execute(text("SELECT has_schema_privilege('energymap_state','USAGE') AND " +
        ' AND '.join(f"has_table_privilege(:t,'{p}')" for p in ('SELECT', 'INSERT', 'DELETE')) + ' AND ' +
        ' AND '.join(f"has_column_privilege(:t,'{c}','UPDATE')" for c in MUTABLE_COLUMNS) +
        " AND (SELECT count(*) FROM pg_index i JOIN pg_class x ON x.oid=i.indexrelid "
        "WHERE i.indrelid=to_regclass(:t) AND i.indisvalid AND "
        "((x.relname='energymap_user_buildings_pkey' AND i.indisprimary) OR "
        "(x.relname='uq_user_buildings_owner_key' AND i.indisunique) OR "
        "x.relname='ix_user_buildings_owner_updated'))=3 "
        "AND EXISTS(SELECT 1 FROM pg_roles WHERE rolname=current_user "
        "AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)) "
        "AND NOT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=current_user::regrole) "
        "AND NOT has_database_privilege(current_database(),'CREATE') "
        "AND NOT has_schema_privilege('public','CREATE') "
        "AND NOT has_schema_privilege('energymap_state','CREATE') "
        "AND NOT EXISTS(SELECT 1 FROM pg_class WHERE oid=to_regclass(:t) AND relowner=current_user::regrole) "
        "AND NOT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','f') AND "
        "(has_table_privilege(c.oid,'INSERT,DELETE,TRUNCATE') OR has_any_column_privilege(c.oid,'UPDATE')))" + SHARE_CHECK), {'t': table, 's': SHARE_TABLE}).scalar_one()
    columns = dict(session.execute(text("SELECT attname,format_type(atttypid,atttypmod) FROM pg_attribute WHERE attrelid=to_regclass(:t) AND attnum>0 AND NOT attisdropped"), {'t': table}).all())
    if not privileges or any(columns.get(name) != kind for name, kind in EXPECTED_COLUMNS.items()):
        raise StateUnavailable


@contextmanager
def state_session(*, read_only=False):
    try:
        with Session(state_engine(), expire_on_commit=False) as session:
            if read_only:
                session.execute(text('SET TRANSACTION READ ONLY'))
            check_state(session)
            yield session
    except SQLAlchemyError:
        raise StateUnavailable from None


def state_ready():
    """Read-only schema/privilege probe. No rows created, auth or source access."""
    try:
        with state_session(read_only=True):
            return True
    except StateUnavailable:
        return False
