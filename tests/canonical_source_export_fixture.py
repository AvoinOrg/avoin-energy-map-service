"""Guarded data-only fixture setup; service/database/role lifecycle is doctor's."""
import json
import os
from pathlib import Path
import psycopg2
from psycopg2 import sql
from src.canonical_source_export.config import private_read, Config, DEFAULTS
from src.canonical_source_export.projection import TEXT_COLUMNS, TABLES, GEOMETRY
from src.canonical_source_export.validation import ExportError, encode
from tests.test_canonical_source_export import PRIVATE, sample

MARKER='energymap-f005-canonical-export-fixture-v1'
DB='energymap_f005_canonical_export_test'


def descriptor(path):
    c=json.loads(private_read(path))
    if (c.get('version')!=1 or c.get('fixture_marker')!=MARKER or c.get('database')!=DB or
        c.get('container_name')!='energymap-f005-canonical-export-postgis' or
        c.get('endpoint')!={'host':'localhost','port':55435}):
        raise ExportError('fixture_guard')
    for key,user in (('reader','energymap_f005_reader'),('maintenance','energymap_f005_maintenance')):
        a=c[key]
        if (set(a)!={'host','port','dbname','user','password','sslmode','sslrootcert'} or
            a['host']!='localhost' or a['port']!=55435 or a['dbname']!=DB or a['user']!=user or a['sslmode']!='verify-full' or
            Path(a['sslrootcert'])!=PRIVATE/'tls/ca.crt'):
            raise ExportError('fixture_guard')
    return c


def connect(account):
    if any(k.startswith('PG') for k in os.environ):
        raise ExportError('ambient_pg_environment')
    return psycopg2.connect(**account,connect_timeout=5,application_name='f005-guarded-test-maintenance',
                            options='-c statement_timeout=15000 -c lock_timeout=1000')


def guarded(c):
    conn=connect(c['maintenance'])
    try:
        with conn.cursor() as q:
            q.execute('SELECT marker,server_identity FROM f005_fixture_control.identity')
            if q.fetchall()!=[(MARKER,c['server_identity'])]:raise ExportError('fixture_identity')
            q.execute("SELECT current_database(),current_user,current_setting('server_version_num')::int,oid FROM pg_database WHERE datname=current_database()")
            database,user,version,oid=q.fetchone()
            if database!=DB or user!='energymap_f005_maintenance' or version//10000!=17:raise ExportError('fixture_identity')
        return conn,version,oid
    except BaseException:
        conn.close();raise


def reset(c,data=None):
    conn,_,_=guarded(c)
    try:
        with conn.cursor() as q:
            for component,table in TABLES.items():
                q.execute(sql.SQL('DROP TABLE IF EXISTS public.{}').format(sql.Identifier(table)))
                defs=[sql.SQL('{} text{}').format(sql.Identifier(f),sql.SQL(' PRIMARY KEY' if component=='building' and f=='building_key' else '')) for f in TEXT_COLUMNS[component]]
                defs += [sql.SQL('{} geometry').format(sql.Identifier(g)) for g in GEOMETRY[component]]
                defs += [sql.SQL('private_canary text'),sql.SQL('address_swe text')]
                q.execute(sql.SQL('CREATE TABLE public.{} ({})').format(sql.Identifier(table),sql.SQL(',').join(defs)))
                for row in (data or sample())[component]:
                    fields=list(TEXT_COLUMNS[component]);vals=[row[f] for f in fields]
                    fields+=['private_canary','address_swe'];vals+=['TABLE_PRIVATE_CANARY','SV_MUST_NOT_ESCAPE']
                    q.execute(sql.SQL('INSERT INTO public.{} ({}) VALUES ({})').format(sql.Identifier(table),
                        sql.SQL(',').join(map(sql.Identifier,fields)),sql.SQL(',').join(sql.Placeholder()*len(vals))),vals)
                    if row['geometry_present']:
                        q.execute(sql.SQL("UPDATE public.{} SET point=ST_GeomFromText('POINT(24 60)',4326) WHERE building_key=%s AND address_key IS NOT DISTINCT FROM %s").format(sql.Identifier(table)),(row['building_key'],row.get('address_key')))
            q.execute("UPDATE f005_fixture_control.private_sentinel SET value='FORBIDDEN_PRIVATE_CANARY'")
        conn.commit()
    finally:conn.close()


def config(c,root,**limits):
    conn,version,oid=guarded(c);conn.close()
    return Config(c['reader'],root,'energy-disposable-postgresql-fixture-not-provider',['synthetic-fixture-not-source-license'],
                  {'marker':DB,'database_oid':oid,'server_version_num':version},
                  dict(DEFAULTS,reserve_bytes=1,**limits))


def write_config(cfg,path):
    from src.canonical_source_export.bundle import write_exclusive
    write_exclusive(path,encode({'version':1,'connection':cfg.connection,'output_root':str(cfg.root),
        'publisher_role':cfg.publisher,'license_references':cfg.license_references,'fixture':cfg.fixture,'limits':cfg.limits}))


def state_digest(c):
    import hashlib
    conn,_,_=guarded(c)
    try:
        with conn.cursor() as q:
            rows=[]
            for table in (*TABLES.values(),'f005_fixture_control.private_sentinel'):
                name='public.'+table if '.' not in table else table
                q.execute('SELECT row_to_json(t)::text FROM '+name+' t ORDER BY row_to_json(t)::text COLLATE "C"')
                rows.append(q.fetchall())
            q.execute("SELECT c.relname,a.attname,a.atttypid,a.attnotnull FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname IN ('main','buildings','addresses') AND a.attnum>0 ORDER BY c.relname,a.attnum")
            rows.append(q.fetchall())
            return hashlib.sha256(encode(rows)).hexdigest()
    finally:conn.close()
