"""Exact streaming statistics. Indexed BLOB equality; no hash-only identity."""
import sqlite3
import json
from .validation import encode, ExportError


def semantic_bytes(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')


class Audit:
    def __init__(self, directory, budget):
        self.directory, self.budget = directory, budget
        self.db = sqlite3.connect(directory / 'audit.sqlite')
        self.db.execute('PRAGMA journal_mode=OFF')
        self.db.execute('PRAGMA synchronous=OFF')
        self.db.execute('PRAGMA mmap_size=0')
        self.db.execute('PRAGMA cache_size=-' + str(max(16,budget.limits['buffer_bytes']//4//1024)))
        self.db.execute('PRAGMA temp_store=MEMORY')
        self.db.execute('PRAGMA max_page_count=' + str(max(1, budget.limits['scratch_bytes']//4096)))
        # All aggregations below are indexed point queries or single scans. None
        # creates a temporary sort/group B-tree; no data-sized temp files/memory.
        self.db.executescript('''
        CREATE TABLE keys(k BLOB PRIMARY KEY, m INTEGER DEFAULT 0, b INTEGER DEFAULT 0,
          g INTEGER DEFAULT 0, a INTEGER DEFAULT 0, p INTEGER DEFAULT 0, t INTEGER DEFAULT 0,
          conflict INTEGER DEFAULT 0) WITHOUT ROWID;
        CREATE TABLE semantics(v BLOB PRIMARY KEY) WITHOUT ROWID;
        CREATE TABLE facts(k BLOB, f INTEGER, v BLOB, PRIMARY KEY(k,f,v)) WITHOUT ROWID;
        CREATE TABLE relationships(id BLOB, v BLOB, PRIMARY KEY(id,v)) WITHOUT ROWID;
        ''')
        self.db.set_progress_handler(self.progress, 1000)
        self.duplicates = self.edges = self.missing_sv = self.reused = 0
        self.fi = {c: [0, 0] for c in ('main', 'building', 'address')}
        self.count = 0

    def progress(self):
        try:
            self.budget.check(self.directory)
            return 0
        except ExportError:
            return 1

    def add(self, c, row):
        self.budget.check(self.directory)
        k = row['building_key'].encode()
        db = self.db
        db.execute('INSERT OR IGNORE INTO keys(k) VALUES(?)', (k,))
        db.execute('UPDATE keys SET g=max(g,?) WHERE k=?', (row['geometry'] == 'present_unvalidated', k))
        a = row['address'] if c == 'address' else row['main_display_address']
        if a:
            self.fi[c][0] += a['fi']['status'] == 'available'
            self.fi[c][1] += 1
        if c == 'address':
            self.edges += 1
            self.missing_sv += a['sv']['status'] != 'available'
            db.execute('UPDATE keys SET a=a+1 WHERE k=?', (k,))
            identity = a['address_key'] if a['address_key'] is not None else row['source_row_id']
            if identity is not None:
                ident = encode([a['namespace'], identity])
                value = semantic_bytes([row['building_key'], a])
                if db.execute('SELECT 1 FROM relationships WHERE id=? AND v<>? LIMIT 1', (ident, value)).fetchone():
                    self.reused += 1
                db.execute('INSERT OR IGNORE INTO relationships VALUES(?,?)', (ident, value))
        else:
            db.execute('UPDATE keys SET ' + ('m' if c == 'main' else 'b') + '=1 WHERE k=?', (k,))
            semantic = semantic_bytes({k: v for k, v in row.items() if k not in ('source_row_id', 'source_modified')})
            self.duplicates += db.execute('SELECT 1 FROM semantics WHERE v=?', (semantic,)).fetchone() is not None
            db.execute('INSERT OR IGNORE INTO semantics VALUES(?)', (semantic,))
            for i, field in enumerate(('permanent', 'property')):
                f = row[field]
                if f['status'] == 'available':
                    db.execute('UPDATE keys SET ' + ('p' if i == 0 else 't') + '=1 WHERE k=?', (k,))
                if f['value'] is not None and f['value'].strip():
                    v = f['value'].encode()
                    if db.execute('SELECT 1 FROM facts WHERE k=? AND f=? AND v<>? LIMIT 1', (k, i, v)).fetchone():
                        db.execute('UPDATE keys SET conflict=1 WHERE k=?', (k,))
                    db.execute('INSERT OR IGNORE INTO facts VALUES(?,?,?)', (k, i, v))
        self.count += 1
        if self.count % 128 == 0:
            db.commit(); self.budget.check(self.directory)

    def statistics(self):
        self.budget.check(self.directory)
        v = self.db.execute('''SELECT coalesce(sum(m),0),coalesce(sum(b),0),
          coalesce(sum(m OR b),0),coalesce(sum(m AND NOT b),0),coalesce(sum(b AND NOT m),0),
          coalesce(sum(conflict),0),coalesce(sum(CASE WHEN NOT(m OR b) THEN a ELSE 0 END),0),
          coalesce(sum((m OR b) AND NOT g),0),coalesce(sum((m OR b) AND NOT a),0),
          coalesce(sum((m OR b) AND NOT p),0),coalesce(sum((m OR b) AND NOT t),0) FROM keys''').fetchone()
        names = ('main_keys','building_keys','union_keys','main_only','building_only','conflicted_keys',
                 'orphan_edges','missing_geometry_keys','missing_address_keys','missing_permanent_keys','missing_property_keys')
        return dict(zip(names, v), duplicate_identity_rows=self.duplicates, address_edges=self.edges,
                    missing_sv_edges=self.missing_sv, rejected_rows=0)

    def close(self):
        self.db.close()
