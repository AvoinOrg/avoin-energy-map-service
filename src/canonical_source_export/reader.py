"""One explicit read-only repeatable-read connection, including counts/catalog."""
import threading
import time
import os
from dataclasses import asdict
from datetime import datetime, timezone
from .projection import TEXT_COLUMNS, TABLES, GEOMETRY
from .validation import ExportError
from .config import FIXTURE_DATABASES, PUBLIC_TEMP_VERSIONS, DatabaseTempPolicy, temp_policy


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class Reader:
    def __init__(self, config, budget):
        self.config, self.budget = config, budget
        self.conn = None
        self.timer = None
        self.shape = {}
        self.snapshot = None

    def __enter__(self):
        import psycopg2
        try:
            self.budget.check()
            if any(k.startswith('PG') for k in os.environ):
                raise ExportError('ambient_pg_environment')
            p = dict(self.config.connection)
            policy = self.config.database_temp_policy
            if not isinstance(policy, DatabaseTempPolicy):
                raise ExportError('temp_policy_invalid')
            parsed = temp_policy({'mode': 'deny'} if policy == DatabaseTempPolicy() else asdict(policy), p)
            if parsed != policy or (policy.mode != 'deny' and p.get('sslmode') != 'verify-full'):
                raise ExportError('temp_policy_invalid')
            p.update(connect_timeout=self.config.limits['connect_seconds'], application_name='energy-f005-canonical-export',
                     client_encoding='UTF8', passfile='/dev/null', options='-c search_path=pg_catalog -c row_security=on',
                     keepalives=1, keepalives_idle=5, keepalives_interval=2, keepalives_count=2, tcp_user_timeout=5000)
            self.conn = psycopg2.connect(**p)  # Dedicated fresh connection, never a pool.
            if self.conn.get_transaction_status() != psycopg2.extensions.TRANSACTION_STATUS_IDLE:
                raise ExportError('source_session')
            if p['sslmode'] == 'verify-full' and not self.conn.info.ssl_in_use:
                raise ExportError('tls_required')
            self.conn.set_session(isolation_level='REPEATABLE READ', readonly=True, autocommit=False)
            # Cancellation is independent of Python signal delivery while libpq
            # is blocked. Daemon cancel has a source statement_timeout backstop.
            def cancel():
                try:
                    self.conn.cancel()
                except Exception:
                    pass
            self.timer = threading.Timer(max(.001, self.budget.end-time.monotonic()), cancel)
            self.timer.daemon = True; self.timer.start()
            self.timeouts = {'statement_timeout': self.config.limits['statement_ms'],
                             'lock_timeout': self.config.limits['lock_ms'],
                             'idle_in_transaction_session_timeout': min(60000, self.config.limits['statement_ms'])}
            with self.conn.cursor() as q:
                for setting, value in self.timeouts.items():
                    q.execute('SELECT pg_catalog.set_config(%s,%s,true)', (setting, str(value)))
                self._session(q, initial=True)
                q.execute("""SELECT d.oid,d.datdba,r.oid,pg_catalog.current_setting('server_version_num')::int
                  FROM pg_catalog.pg_database d CROSS JOIN pg_catalog.pg_roles r
                  WHERE d.datname=pg_catalog.current_database() AND r.rolname=current_user""")
                database_oid, owner_oid, reader_oid, version = q.fetchone()
                self.version = version
                fixture = self.config.fixture
                if fixture and (not isinstance(fixture.get('marker'), str) or fixture['marker'] not in FIXTURE_DATABASES
                    or fixture['marker'] != p['dbname']
                    or database_oid != fixture['database_oid'] or version != fixture['server_version_num']):
                    raise ExportError('fixture_identity')
                if policy.mode != 'deny' and (version not in PUBLIC_TEMP_VERSIONS or
                    (database_oid, reader_oid, version) != (policy.database_oid, policy.reader_oid, policy.server_version_num)):
                    raise ExportError('temp_policy_identity')
                self._privileges(q, database_oid, owner_oid, reader_oid)
            self.catalog()
            return self
        except BaseException:
            self.close()
            raise

    def _session(self, q, initial=False):
        """Fixed, bounded checks before each exporter-controlled source operation."""
        import psycopg2.extensions
        self.budget.check()
        if not initial and self.conn.get_transaction_status() != psycopg2.extensions.TRANSACTION_STATUS_INTRANS:
            raise ExportError('source_session')
        q.execute("""SELECT pg_catalog.current_database(),current_user,session_user,
          pg_catalog.current_setting('transaction_isolation'),pg_catalog.current_setting('transaction_read_only'),
          pg_catalog.current_setting('server_encoding'),pg_catalog.current_setting('client_encoding'),
          pg_catalog.current_setting('search_path'),pg_catalog.current_setting('row_security'),
          pg_catalog.current_setting('session_replication_role'),pg_catalog.current_setting('standard_conforming_strings'),
          pg_catalog.current_setting('role'),pg_catalog.pg_my_temp_schema(),pg_catalog.pg_current_snapshot()::text""")
        values = q.fetchone()
        expected = (self.config.connection['dbname'], self.config.connection['user'], self.config.connection['user'],
                    'repeatable read', 'on', 'UTF8', 'UTF8', 'pg_catalog', 'on', 'origin', 'on', 'none', 0)
        if values[:-1] != expected or (not initial and values[-1] != self.snapshot):
            raise ExportError('source_session')
        self.snapshot = values[-1]
        q.execute("""SELECT name,setting::bigint FROM pg_catalog.pg_settings
          WHERE name IN ('statement_timeout','lock_timeout','idle_in_transaction_session_timeout')""")
        if dict(q.fetchall()) != self.timeouts:
            raise ExportError('source_session')
        if initial:
            q.execute("""SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_prepared_statements),
              EXISTS(SELECT 1 FROM pg_catalog.pg_cursors),
              EXISTS(SELECT 1 FROM pg_catalog.pg_listening_channels())""")
            if any(q.fetchone()):
                raise ExportError('source_session')

    def _privileges(self, q, database_oid, owner_oid, reader_oid):
        self.budget.check()
        q.execute("""SELECT rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls,
          EXISTS(SELECT 1 FROM pg_catalog.pg_auth_members WHERE member=%s)
          FROM pg_catalog.pg_roles WHERE oid=%s""", (reader_oid, reader_oid))
        # Refuse every membership, including NOINHERIT/SET/ADMIN and thus all
        # nested/predefined-role paths. Database owners implicitly reach
        # pg_database_owner even without a pg_auth_members row.
        if reader_oid == owner_oid or any(q.fetchone()):
            raise ExportError('reader_not_narrow')
        q.execute("""SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_db_role_setting s,
          LATERAL pg_catalog.unnest(s.setconfig) setting
          WHERE s.setdatabase IN (0,%s) AND s.setrole IN (0,%s)
          AND setting NOT IN ('default_transaction_read_only=on','search_path=pg_catalog')),
          EXISTS(SELECT 1 FROM pg_catalog.pg_namespace n
            WHERE pg_catalog.has_schema_privilege(n.oid,'CREATE') OR n.nspowner=%s)""",
                  (database_oid, reader_oid, reader_oid))
        if any(q.fetchone()):
            raise ExportError('reader_not_narrow')
        q.execute("""SELECT a.grantor,a.grantee,a.privilege_type,a.is_grantable,
          a.grantor IN (SELECT oid FROM pg_catalog.pg_roles),
          a.grantee=0 OR a.grantee IN (SELECT oid FROM pg_catalog.pg_roles)
          FROM pg_catalog.pg_database d,
          LATERAL pg_catalog.aclexplode(COALESCE(d.datacl,pg_catalog.acldefault('d',d.datdba))) a
          WHERE d.oid=%s""", (database_oid,))
        entries = q.fetchall()
        if len(entries) != len(set(entries)) or any(not r[4] or not r[5] or r[2] not in ('CREATE','CONNECT','TEMPORARY') for r in entries):
            raise ExportError('database_acl')
        effective = set()
        public_temp = False
        for grantor, grantee, privilege, grantable, _, _ in entries:
            if grantee not in (0, reader_oid):
                continue  # Other normal accounts' grants confer no capability.
            if grantor != owner_oid or grantable or privilege == 'CREATE':
                raise ExportError('reader_not_narrow')
            effective.add(privilege)
            if privilege == 'TEMPORARY':
                if grantee != 0:
                    raise ExportError('reader_not_narrow')
                public_temp = True
        q.execute("""SELECT pg_catalog.has_database_privilege(%s,'CREATE'),
          pg_catalog.has_database_privilege(%s,'TEMP'),pg_catalog.has_database_privilege(%s,'CONNECT')""",
                  (database_oid, database_oid, database_oid))
        actual = q.fetchone()
        if actual != tuple(p in effective for p in ('CREATE','TEMPORARY','CONNECT')):
            raise ExportError('database_acl')
        if actual[0] or not actual[2] or (actual[1] and (self.config.database_temp_policy.mode == 'deny' or not public_temp)):
            raise ExportError('reader_not_narrow')
        if self.config.database_temp_policy.mode != 'deny' and not (actual[1] and public_temp):
            raise ExportError('temp_policy_provenance')
        self._relations(q, reader_oid)

    def _relations(self, q, reader_oid):
        # Audit capabilities, not values, across every non-system relation.
        # A column-only grant is readable even without table-level SELECT.
        writes = 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
        if self.version >= 170000:
            writes += ',MAINTAIN'
        q.execute("""SELECT c.relowner=%s,c.relkind,
          n.nspname='public' AND c.relname IN ('main','buildings','addresses'),
          c.relname='spatial_ref_sys' AND EXISTS(SELECT 1 FROM pg_catalog.pg_depend d
            JOIN pg_catalog.pg_extension e ON e.oid=d.refobjid WHERE d.objid=c.oid
            AND d.classid='pg_catalog.pg_class'::pg_catalog.regclass AND d.deptype='e' AND e.extname='postgis'),
          CASE WHEN c.relkind='S' THEN pg_catalog.has_sequence_privilege(c.oid,'USAGE,SELECT,UPDATE')
            ELSE pg_catalog.has_table_privilege(c.oid,'SELECT') OR pg_catalog.has_any_column_privilege(c.oid,'SELECT') END,
          CASE WHEN c.relkind='S' THEN false ELSE pg_catalog.has_table_privilege(c.oid,%s)
            OR pg_catalog.has_table_privilege(c.oid,'SELECT WITH GRANT OPTION')
            OR pg_catalog.has_any_column_privilege(c.oid,'SELECT WITH GRANT OPTION')
            OR pg_catalog.has_any_column_privilege(c.oid,'INSERT,UPDATE,REFERENCES') END
          FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname NOT IN ('pg_catalog','information_schema')
          AND c.relkind IN ('r','p','v','m','f','S')""", (reader_oid, writes))
        for owner, kind, contracted, spatial, readable, writable in q.fetchall():
            if owner or writable:
                raise ExportError('reader_not_narrow')
            if readable and (kind == 'S' or not (contracted or spatial)):
                raise ExportError('reader_excess_select')

    def catalog(self):
        with self.conn.cursor() as q:
            self._session(q)
            for c, table in TABLES.items():
                self.budget.check()
                q.execute("""SELECT c.oid,c.relkind,c.relowner= (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_user),
                  pg_catalog.has_table_privilege(c.oid,'SELECT'),pg_catalog.has_table_privilege(c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'),
                  pg_catalog.pg_has_role(c.relowner,'MEMBER'),c.reltuples::bigint,c.relrowsecurity,
                  EXISTS(SELECT 1 FROM pg_catalog.pg_inherits i WHERE i.inhparent=c.oid OR i.inhrelid=c.oid)
                  FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname=%s""", (table,))
                r = q.fetchone()
                if not r or r[1] != 'r' or not r[3] or r[7] or r[8]:
                    raise ExportError('required_relation', c)
                if r[2] or r[4] or r[5]:
                    raise ExportError('reader_not_narrow', c)
                q.execute("""SELECT a.attname,CASE WHEN t.typnamespace='pg_catalog'::pg_catalog.regnamespace AND t.typname IN ('text','varchar') THEN t.typname
                  WHEN t.typname='geometry' AND EXISTS(SELECT 1 FROM pg_catalog.pg_depend d JOIN pg_catalog.pg_extension e ON e.oid=d.refobjid
                    WHERE d.objid=t.oid AND d.classid='pg_catalog.pg_type'::pg_catalog.regclass AND d.deptype='e' AND e.extname='postgis') THEN 'geometry' ELSE 'unsupported' END,pg_catalog.has_column_privilege(%s,a.attnum,'INSERT,UPDATE,REFERENCES')
                  FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_type t ON t.oid=a.atttypid
                  WHERE a.attrelid=%s AND a.attnum>0 AND NOT a.attisdropped""", (r[0],r[0]))
                attributes = q.fetchall()
                if any(w for _,_,w in attributes):
                    raise ExportError('reader_not_narrow',c)
                columns = {name: typ for name, typ, writable in attributes}
                if any(columns.get(f) not in ('text','varchar') for f in TEXT_COLUMNS[c]):
                    raise ExportError('required_text_column', c)
                geometry = [f for f in GEOMETRY[c] if f in columns]
                if any(columns[f] != 'geometry' for f in geometry):
                    raise ExportError('geometry_type', c)
                pk = 'building_key' if c == 'building' else 'address_key'
                q.execute("""SELECT a.attname FROM pg_catalog.pg_index i JOIN pg_catalog.pg_attribute a
                  ON a.attrelid=i.indrelid AND a.attnum=i.indkey[0]
                  WHERE i.indrelid=%s AND i.indisprimary AND i.indisvalid AND i.indnkeyatts=1""", (r[0],))
                stable = c != 'main' and q.fetchone() == (pk,)
                self.shape[c] = {'stable':stable,'geometry':geometry,'estimated_rows':max(0,r[6])}

    def count(self, c):
        self.budget.check()
        with self.conn.cursor() as q:
            self._session(q)
            q.execute('SELECT pg_catalog.count(*) FROM (SELECT 1 FROM public.'+TABLES[c]+' LIMIT %s) bounded', (self.config.limits['rows']+1,))
            count = q.fetchone()[0]
        if count > self.config.limits['rows']:
            raise ExportError('row_limit', c)
        return count

    def rows(self, c, probe=False):
        with self.conn.cursor() as q:
            self._session(q)
        fields, terms = TEXT_COLUMNS[c], []
        for f in fields:
            maximum = 512 if f in ('address_fin','postal_office_fin') else 256
            terms.extend([f'CASE WHEN pg_catalog.octet_length({f})<={maximum} THEN {f} ELSE NULL END',
                          f'coalesce(pg_catalog.octet_length({f})>{maximum},false)'])
        geom = self.shape[c]['geometry']
        terms.append('('+' OR '.join(f'{g} IS NOT NULL' for g in geom)+')' if geom else 'false')
        sql = 'SELECT '+','.join(terms)+' FROM public.'+TABLES[c]+(' LIMIT 1' if probe else '')
        with self.conn.cursor(name='f005_'+c) as q:
            q.itersize = 1
            q.execute(sql)
            while True:
                self.budget.check()
                batch = q.fetchmany(1)  # Worst-case raw+mapped allocation stays finite.
                if not batch:
                    break
                values = batch[0]
                for i, f in enumerate(fields):
                    if values[i*2+1]:
                        raise ExportError('unrepresentable', f)
                row = {f:values[i*2] for i,f in enumerate(fields)}
                row['geometry_present'] = values[-1]
                yield row

    def close(self):
        if self.timer:
            self.timer.cancel()
        if self.conn:
            try:
                self.conn.cancel()
            except Exception:
                pass
            try:
                self.conn.rollback()
            finally:
                self.conn.close()
            self.conn = None

    def __exit__(self, *args):
        self.close()
