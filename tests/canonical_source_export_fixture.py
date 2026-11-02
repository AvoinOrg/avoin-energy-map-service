"""Guarded data-only fixture setup; service/database/role lifecycle is doctor's."""
import json
import os
from dataclasses import asdict
from pathlib import Path
import psycopg2
from psycopg2 import sql
from src.canonical_source_export.config import private_read, Config, DEFAULTS
from src.canonical_source_export.projection import TEXT_COLUMNS, TABLES, GEOMETRY
from src.canonical_source_export.validation import ExportError, encode
from tests.test_canonical_source_export import PRIVATE, sample

RUNTIME=PRIVATE.parent
# Fixed doctor-owned identities only; no caller-selected endpoint or account.
FIXTURES={
    170005: {'version':170005,'root':PRIVATE,
             'marker':'energymap-f005-canonical-export-fixture-v1',
             'database':'energymap_f005_canonical_export_test',
             'container':'energymap-f005-canonical-export-postgis','port':55435,
             'reader':'energymap_f005_reader','maintenance':'energymap_f005_maintenance'},
    170002: {'version':170002,'root':RUNTIME/'f006-canonical-source-export-pg170002-private',
             'marker':'energymap-f006-canonical-export-fixture-pg170002-v1',
             'database':'energymap_f006_canonical_export_pg170002_test',
             'container':'energymap-f006-canonical-export-postgis-170002','port':55436,
             'reader':'energymap_f006_pg170002_reader','maintenance':'energymap_f006_pg170002_maintenance'},
}


def fixture_profile(c):
    if not isinstance(c,dict):
        raise ExportError('fixture_guard')
    version=c.get('postgres_server_version_num',170005)  # Original F005 descriptor compatibility.
    if type(version) is not int or version not in FIXTURES:
        raise ExportError('fixture_guard')
    p=FIXTURES[version]
    if (type(c.get('version')) is not int or c['version']!=1 or
        c.get('fixture_marker')!=p['marker'] or c.get('database')!=p['database'] or
        c.get('container_name')!=p['container'] or c.get('endpoint')!={'host':'localhost','port':p['port']}):
        raise ExportError('fixture_guard')
    for key in ('reader','maintenance'):
        a=c.get(key)
        if (not isinstance(a,dict) or set(a)!={'host','port','dbname','user','password','sslmode','sslrootcert'} or
            a['host']!='localhost' or a['port']!=p['port'] or a['dbname']!=p['database'] or
            a['user']!=p[key] or a['sslmode']!='verify-full' or
            not isinstance(a['sslrootcert'],str) or Path(a['sslrootcert'])!=p['root']/'tls/ca.crt'):
            raise ExportError('fixture_guard')
    return p


def descriptor(path):
    c=json.loads(private_read(path))
    p=fixture_profile(c)
    if Path(path).absolute()!=p['root']/'fixture.json':
        raise ExportError('fixture_guard')
    return c


def connect(account):
    if any(k.startswith('PG') for k in os.environ):
        raise ExportError('ambient_pg_environment')
    return psycopg2.connect(**account,connect_timeout=5,application_name='f005-guarded-test-maintenance',
                            options='-c statement_timeout=15000 -c lock_timeout=1000')


def guarded(c):
    p=fixture_profile(c)
    conn=connect(c['maintenance'])
    try:
        with conn.cursor() as q:
            q.execute('SELECT marker,server_identity FROM f005_fixture_control.identity')
            if q.fetchall()!=[(p['marker'],c['server_identity'])]:raise ExportError('fixture_identity')
            q.execute("SELECT current_database(),current_user,current_setting('server_version_num')::int,oid FROM pg_database WHERE datname=current_database()")
            database,user,version,oid=q.fetchone()
            if database!=p['database'] or user!=p['maintenance'] or version!=p['version']:raise ExportError('fixture_identity')
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
                  {'marker':c['database'],'database_oid':oid,'server_version_num':version},
                  dict(DEFAULTS,reserve_bytes=1,**limits))


def write_config(cfg,path):
    from src.canonical_source_export.bundle import write_exclusive
    policy=cfg.database_temp_policy
    d={'version':1,'connection':cfg.connection,'output_root':str(cfg.root),
       'publisher_role':cfg.publisher,'license_references':cfg.license_references,'fixture':cfg.fixture,'limits':cfg.limits}
    if policy.mode!='deny':
        d.update(version=2,database_temp_policy=asdict(policy))
    write_exclusive(path,encode(d))


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
