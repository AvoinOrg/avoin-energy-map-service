"""Full isolated-state structure and effective ACL audit (no personal row output)."""
import json
from pathlib import Path

from psycopg2 import sql

from .private import require

HISTORICAL_VERSION = 'es0001_saved_share'
VERSION = 'es0002_personal_v2'
PROFILES = {HISTORICAL_VERSION: 'expected_catalog.json', VERSION: 'expected_catalog_v2.json'}
DATA = 'energymap_state'
META = 'energymap_state_meta'
SAVED = 'energymap_user_buildings'
SHARES = 'energymap_user_building_shares'
MUTABLE = ('display_name', 'schema_version', 'overrides', 'scenario_inputs',
           'last_known_summary', 'revision', 'updated_at')


def rows(connection, query, args=()):
    with connection.cursor() as cursor:
        cursor.execute(query, args or None)
        return cursor.fetchall() if cursor.description else []


def scalar(connection, query, args=()):
    return rows(connection, query, args)[0][0]


def snapshot(connection):
    """Stable catalog definitions; includes every data relation/column/constraint/index."""
    return {
        'relations': rows(connection, """SELECT c.relname,c.relkind,c.relpersistence,c.relrowsecurity,c.relforcerowsecurity
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='energymap_state' AND c.relkind NOT IN ('i','I') ORDER BY c.relname"""),
        'columns': rows(connection, """SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,
            pg_get_expr(d.adbin,d.adrelid),a.attidentity,a.attgenerated
            FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
            WHERE n.nspname='energymap_state' AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped
            ORDER BY c.relname,a.attnum"""),
        'constraints': rows(connection, """SELECT c.relname,x.conname,x.contype,pg_get_constraintdef(x.oid),
            x.convalidated,x.condeferrable,x.condeferred FROM pg_constraint x JOIN pg_class c ON c.oid=x.conrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='energymap_state'
            ORDER BY c.relname,x.conname"""),
        'indexes': rows(connection, """SELECT c.relname,x.relname,pg_get_indexdef(i.indexrelid),
            i.indisvalid,i.indisready,i.indislive,i.indisunique,i.indisprimary
            FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_class x ON x.oid=i.indexrelid WHERE n.nspname='energymap_state' ORDER BY c.relname,x.relname"""),
    }


def normalized(value):
    return json.loads(json.dumps(value))


def schema_version(connection):
    versions = scalar(connection, "SELECT array_agg(version_num::text ORDER BY version_num) FROM energymap_state_meta.alembic_version")
    require(versions in ([HISTORICAL_VERSION], [VERSION]), 'version_drift')
    return versions[0]


def audit_schema(connection, expected_version=None):
    version = schema_version(connection)
    require(expected_version is None or expected_version == version, 'version_drift')
    expected = json.loads((Path(__file__).parent / PROFILES[version]).read_text())
    require(normalized(snapshot(connection)) == expected, 'schema_drift')
    require(scalar(connection, "SELECT array_agg(version_num::text ORDER BY version_num) FROM energymap_state_meta.alembic_version") == [version], 'version_drift')
    require(rows(connection, """SELECT n.nspname,c.relname,c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' AND c.relkind NOT IN ('i','I')
        ORDER BY n.nspname,c.relname""") == [(DATA, SHARES, 'r'), (DATA, SAVED, 'r'), (META, 'alembic_version', 'r')], 'schema_drift')
    require(rows(connection, """SELECT a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull
        FROM pg_attribute a WHERE a.attrelid='energymap_state_meta.alembic_version'::regclass AND a.attnum>0 AND NOT a.attisdropped""") == [('version_num', 'character varying(32)', True)], 'version_drift')
    require(rows(connection, """SELECT contype,pg_get_constraintdef(oid),convalidated FROM pg_constraint
        WHERE conrelid='energymap_state_meta.alembic_version'::regclass""") == [('p', 'PRIMARY KEY (version_num)', True)], 'version_drift')
    require(scalar(connection, """SELECT count(*) FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('energymap_state','energymap_state_meta')
        AND a.attnum>0 AND a.attisdropped""") == 0, 'schema_drift')
    require(scalar(connection, """SELECT count(*) FROM pg_index WHERE indrelid='energymap_state_meta.alembic_version'::regclass
        AND indisvalid AND indisready AND indisprimary AND indisunique""") == 1, 'version_drift')
    require(scalar(connection, """SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
        WHERE n.nspname IN ('public','energymap_state','energymap_state_meta')
        AND NOT (t.typtype='c' AND t.typrelid IN ('energymap_state.energymap_user_buildings'::regclass,
        'energymap_state.energymap_user_building_shares'::regclass,'energymap_state_meta.alembic_version'::regclass))
        AND NOT (t.typelem<>0 AND t.typcategory='A')""") == 0, 'schema_drift')
    require(scalar(connection, 'SELECT count(*) FROM pg_event_trigger') == 0, 'schema_drift')
    require(scalar(connection, "SELECT count(*) FROM pg_extension WHERE extname<>'plpgsql'") == 0, 'schema_drift')
    require(scalar(connection, """SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'""") == 0, 'schema_drift')
    require(scalar(connection, """SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('energymap_state','energymap_state_meta') AND NOT t.tgisinternal""") == 0, 'schema_drift')
    require(scalar(connection, """SELECT count(*) FROM pg_rewrite r JOIN pg_class c ON c.oid=r.ev_class
        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('energymap_state','energymap_state_meta')""") == 0, 'schema_drift')
    # No personal dependency on anything outside its own schemas and built-ins.
    require(scalar(connection, """SELECT count(*) FROM pg_constraint x JOIN pg_class c ON c.oid=x.conrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_class ref ON ref.oid=x.confrelid
        JOIN pg_namespace rn ON rn.oid=ref.relnamespace WHERE n.nspname='energymap_state' AND x.contype='f'
        AND rn.nspname<>'energymap_state'""") == 0, 'schema_drift')


def role_audit(connection, name, *, login):
    require(rows(connection, """SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,rolinherit,
        rolconnlimit,rolvaliduntil,rolconfig FROM pg_roles WHERE rolname=%s""", (name,)) ==
        [(login, False, False, False, False, False, False, -1, None, None)], 'role_drift')
    require(scalar(connection, "SELECT count(*) FROM pg_auth_members WHERE member=%s::regrole OR roleid=%s::regrole", (name, name)) == 0, 'role_drift')


def harden(connection, target):
    """Only on a positively identified newly managed target, before LOGIN."""
    owner, writer = target['migrator'], target['writer']
    rows(connection, sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(target['database'])))
    rows(connection, sql.SQL('GRANT CONNECT ON DATABASE {} TO {}, {}').format(*map(sql.Identifier, (target['database'], owner, writer))))
    rows(connection, 'REVOKE ALL ON SCHEMA public FROM PUBLIC')
    rows(connection, sql.SQL('ALTER DEFAULT PRIVILEGES FOR ROLE {} REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC').format(sql.Identifier(owner)))
    rows(connection, sql.SQL('ALTER DEFAULT PRIVILEGES FOR ROLE {} REVOKE USAGE ON TYPES FROM PUBLIC').format(sql.Identifier(owner)))


def grant_state(connection, target):
    writer = sql.Identifier(target['writer'])
    for schema in (DATA, META):
        rows(connection, sql.SQL('REVOKE ALL ON SCHEMA {} FROM PUBLIC').format(sql.Identifier(schema)))
        rows(connection, sql.SQL('REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM PUBLIC').format(sql.Identifier(schema)))
    rows(connection, sql.SQL('GRANT USAGE ON SCHEMA energymap_state TO {}').format(writer))
    for table in (SAVED, SHARES):
        rows(connection, sql.SQL('GRANT SELECT,INSERT,DELETE ON {}.{} TO {}').format(sql.Identifier(DATA), sql.Identifier(table), writer))
    rows(connection, sql.SQL('GRANT UPDATE ({}) ON {}.{} TO {}').format(
        sql.SQL(',').join(map(sql.Identifier, MUTABLE)), sql.Identifier(DATA), sql.Identifier(SAVED), writer))


def audit_acl(connection, target, *, writer_login=True, allow_missing_state_grants=False):
    # Only interrupted restore may lack the explicit data/schema grants. All
    # other ACLs, ownership, defaults and role attributes remain exact, and even
    # that pre-grant audit rejects any privilege outside the contract.
    owner, writer = target['migrator'], target['writer']
    role_audit(connection, owner, login=True)
    role_audit(connection, writer, login=writer_login)
    require(scalar(connection, "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=current_database()") == owner, 'privilege_drift')
    require(scalar(connection, """SELECT count(*) FROM pg_namespace WHERE nspname !~ '^pg_'
        AND nspname NOT IN ('information_schema','public','energymap_state','energymap_state_meta')""") == 0, 'privilege_drift')
    require(rows(connection, "SELECT nspname,pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname IN ('energymap_state','energymap_state_meta') ORDER BY nspname") == [(DATA, owner), (META, owner)], 'privilege_drift')
    require(scalar(connection, """SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname IN ('energymap_state','energymap_state_meta') AND c.relowner<>%s::regrole""", (owner,)) == 0, 'privilege_drift')
    # Exact ACLs for non-owner roles, including PUBLIC and grant options.
    actual = rows(connection, """SELECT a.grantee::regrole::text,a.privilege_type,a.is_grantable
        FROM pg_database d,LATERAL aclexplode(coalesce(d.datacl,acldefault('d',d.datdba))) a
        WHERE d.datname=current_database() AND a.grantee<>d.datdba ORDER BY 1,2""")
    require(actual == [(writer, 'CONNECT', False)], 'privilege_drift')
    actual = rows(connection, """SELECT n.nspname,a.grantee::regrole::text,a.privilege_type,a.is_grantable
        FROM pg_namespace n,LATERAL aclexplode(coalesce(n.nspacl,acldefault('n',n.nspowner))) a
        WHERE n.nspname IN ('public','energymap_state','energymap_state_meta') AND a.grantee<>n.nspowner ORDER BY 1,2,3""")
    expected = [(DATA, writer, 'USAGE', False)]
    require(set(actual) <= set(expected) if allow_missing_state_grants else actual == expected, 'privilege_drift')
    actual = rows(connection, """SELECT n.nspname,c.relname,a.grantee::regrole::text,a.privilege_type,a.is_grantable
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace,
        LATERAL aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) a
        WHERE n.nspname IN ('energymap_state','energymap_state_meta') AND a.grantee<>c.relowner ORDER BY 1,2,3,4""")
    expected = sorted((DATA, table, writer, privilege, False) for table in (SAVED, SHARES) for privilege in ('SELECT','INSERT','DELETE'))
    require(set(actual) <= set(expected) if allow_missing_state_grants else actual == expected, 'privilege_drift')
    actual = rows(connection, """SELECT n.nspname,c.relname,x.attname,a.grantee::regrole::text,a.privilege_type,a.is_grantable
        FROM pg_attribute x JOIN pg_class c ON c.oid=x.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace,
        LATERAL aclexplode(x.attacl) a WHERE n.nspname IN ('energymap_state','energymap_state_meta') ORDER BY 1,2,3,4,5""")
    expected = sorted((DATA, SAVED, col, writer, 'UPDATE', False) for col in MUTABLE)
    require(set(actual) <= set(expected) if allow_missing_state_grants else actual == expected, 'privilege_drift')
    # No default ACLs except a global removal of PUBLIC function execution.
    actual = rows(connection, """SELECT d.defaclrole::regrole::text,d.defaclnamespace,d.defaclobjtype,
        a.grantee::regrole::text,a.privilege_type,a.is_grantable
        FROM pg_default_acl d,LATERAL aclexplode(d.defaclacl) a ORDER BY 1,2,3,4,5""")
    require(actual == [(owner, 0, 'T', owner, 'USAGE', False), (owner, 0, 'f', owner, 'EXECUTE', False)], 'privilege_drift')
    require(not scalar(connection, "SELECT has_database_privilege(%s,current_database(),'CREATE,TEMP')", (writer,)), 'privilege_drift')
    require(not scalar(connection, "SELECT has_schema_privilege(%s,'public','USAGE,CREATE') OR has_schema_privilege(%s,'energymap_state_meta','USAGE,CREATE')", (writer,writer)), 'privilege_drift')
    # Reader must have neither CONNECT nor state access in this database.
    require(not scalar(connection, "SELECT has_database_privilege(%s,current_database(),'CONNECT') OR has_schema_privilege(%s,'energymap_state','USAGE,CREATE')", (target['source_reader'],target['source_reader'])), 'source_separation')
    # Global ownership inventory includes shared objects; runtime may own none.
    require(scalar(connection, "SELECT count(*) FROM pg_shdepend WHERE refclassid='pg_authid'::regclass AND refobjid=%s::regrole AND deptype='o'", (writer,)) == 0, 'privilege_drift')


def audit_source(connection, target):
    # CONNECT denial closes PUBLIC built-in large-object writes. HBA mode must
    # additionally pass exact policy and fresh connection probes in full_audit.
    entry = target.values.get('source_entry', {'mode': 'acl'})
    for key in ('writer', 'migrator'):
        name = target[key]
        if entry['mode'] == 'acl':
            require(not scalar(connection,
                "SELECT has_database_privilege(%s,current_database(),'CONNECT')", (name,)), 'source_separation')
        require(not scalar(connection,
            "SELECT has_database_privilege(%s,current_database(),'CREATE')", (name,)), 'source_separation')
        require(not scalar(connection, """SELECT EXISTS(SELECT 1 FROM pg_namespace n
            WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
            AND has_schema_privilege(%s,n.oid,'CREATE'))""", (name,)), 'source_separation')
        require(not scalar(connection, """SELECT EXISTS(SELECT 1 FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname !~ '^pg_'
            AND n.nspname<>'information_schema' AND c.relkind IN ('r','p','v','m','f','S')
            AND (CASE WHEN c.relkind='S' THEN has_sequence_privilege(%s,c.oid,'USAGE,UPDATE')
            ELSE has_table_privilege(%s,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
            OR has_any_column_privilege(%s,c.oid,'INSERT,UPDATE,REFERENCES') END))""", (name,name,name)), 'source_separation')
        require(not scalar(connection, """SELECT EXISTS(SELECT 1 FROM pg_proc p
            JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname !~ '^pg_'
            AND n.nspname<>'information_schema' AND p.prosecdef
            AND has_function_privilege(%s,p.oid,'EXECUTE'))""", (name,)), 'source_separation')
