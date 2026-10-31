"""One explicit read-only repeatable-read connection, including counts/catalog."""
import threading
import time
from datetime import datetime, timezone
from .projection import TEXT_COLUMNS, TABLES, GEOMETRY
from .validation import ExportError


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
            p = dict(self.config.connection)
            p.update(connect_timeout=self.config.limits['connect_seconds'], application_name='energy-f005-canonical-export',
                     client_encoding='UTF8', passfile='/dev/null', options='-c search_path=pg_catalog',
                     keepalives=1, keepalives_idle=5, keepalives_interval=2, keepalives_count=2, tcp_user_timeout=5000)
            self.conn = psycopg2.connect(**p)
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
            with self.conn.cursor() as q:
                for setting, value in [('statement_timeout', self.config.limits['statement_ms']),
                                       ('lock_timeout', self.config.limits['lock_ms']),
                                       ('idle_in_transaction_session_timeout', min(60000, self.config.limits['statement_ms']))]:
                    q.execute('SELECT pg_catalog.set_config(%s,%s,true)', (setting, str(value)))
                q.execute("SELECT current_database(),current_user,current_setting('transaction_isolation'),current_setting('transaction_read_only'),current_setting('server_encoding'),current_setting('client_encoding'),current_setting('server_version_num')::int,pg_current_snapshot()::text")
                db, user, isolation, ro, server, client, version, self.snapshot = q.fetchone()
                if (db != p['dbname'] or user != p['user'] or isolation != 'repeatable read' or ro != 'on'
                    or server != 'UTF8' or client != 'UTF8'):
                    raise ExportError('source_identity')
                q.execute('SELECT oid FROM pg_database WHERE datname=current_database()')
                oid = q.fetchone()[0]
                fixture = self.config.fixture
                if fixture and (oid != fixture['database_oid'] or version != fixture['server_version_num']):
                    raise ExportError('fixture_identity')
                q.execute('SELECT rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=current_user')
                if any(q.fetchone()):
                    raise ExportError('reader_not_narrow')
                q.execute("SELECT has_database_privilege(current_database(),'CREATE'),has_database_privilege(current_database(),'TEMP'),has_schema_privilege('public','CREATE')")
                if any(q.fetchone()):
                    raise ExportError('reader_not_narrow')
                q.execute("""SELECT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                  WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%'
                  AND c.relkind IN ('r','p','v','m','f')
                  AND NOT(n.nspname='public' AND c.relname IN ('main','buildings','addresses'))
                  AND NOT(c.relname='spatial_ref_sys' AND EXISTS(SELECT 1 FROM pg_depend d
                    JOIN pg_extension e ON e.oid=d.refobjid WHERE d.objid=c.oid AND d.classid='pg_class'::regclass AND d.deptype='e' AND e.extname='postgis'))
                  AND has_table_privilege(c.oid,'SELECT'))""")
                if q.fetchone()[0]:
                    raise ExportError('reader_excess_select')
            self.catalog()
            return self
        except BaseException:
            self.close()
            raise

    def catalog(self):
        with self.conn.cursor() as q:
            for c, table in TABLES.items():
                self.budget.check()
                q.execute("""SELECT c.oid,c.relkind,c.relowner= (SELECT oid FROM pg_roles WHERE rolname=current_user),
                  has_table_privilege(c.oid,'SELECT'),has_table_privilege(c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'),
                  pg_has_role(c.relowner,'MEMBER'),c.reltuples::bigint,c.relrowsecurity
                  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname=%s""", (table,))
                r = q.fetchone()
                if not r or r[1] != 'r' or not r[3] or r[7]:
                    raise ExportError('required_relation', c)
                if r[2] or r[4] or r[5]:
                    raise ExportError('reader_not_narrow', c)
                q.execute("""SELECT a.attname,CASE WHEN t.typnamespace='pg_catalog'::regnamespace AND t.typname IN ('text','varchar') THEN t.typname
                  WHEN t.typname='geometry' AND EXISTS(SELECT 1 FROM pg_depend d JOIN pg_extension e ON e.oid=d.refobjid
                    WHERE d.objid=t.oid AND d.classid='pg_type'::regclass AND d.deptype='e' AND e.extname='postgis') THEN 'geometry' ELSE 'unsupported' END,has_column_privilege(%s,a.attnum,'INSERT,UPDATE,REFERENCES')
                  FROM pg_attribute a JOIN pg_type t ON t.oid=a.atttypid
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
                q.execute("""SELECT a.attname FROM pg_index i JOIN pg_attribute a
                  ON a.attrelid=i.indrelid AND a.attnum=i.indkey[0]
                  WHERE i.indrelid=%s AND i.indisprimary AND i.indisvalid AND i.indnkeyatts=1""", (r[0],))
                stable = c != 'main' and q.fetchone() == (pk,)
                self.shape[c] = {'stable':stable,'geometry':geometry,'estimated_rows':max(0,r[6])}

    def count(self, c):
        self.budget.check()
        with self.conn.cursor() as q:
            q.execute('SELECT count(*) FROM (SELECT 1 FROM public.'+TABLES[c]+' LIMIT %s) bounded', (self.config.limits['rows']+1,))
            count = q.fetchone()[0]
        if count > self.config.limits['rows']:
            raise ExportError('row_limit', c)
        return count

    def rows(self, c, probe=False):
        fields, terms = TEXT_COLUMNS[c], []
        for f in fields:
            maximum = 512 if f in ('address_fin','postal_office_fin') else 256
            terms.extend([f'CASE WHEN octet_length({f})<={maximum} THEN {f} ELSE NULL END',
                          f'coalesce(octet_length({f})>{maximum},false)'])
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
