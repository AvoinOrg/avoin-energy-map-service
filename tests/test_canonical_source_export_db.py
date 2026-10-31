"""Required real PostgreSQL evidence. Missing fixture is a failure, never a skip."""
import json
import os
import signal
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
import psycopg2
from src.canonical_source_export import bundle
from src.canonical_source_export.config import Config, load_config
from src.canonical_source_export.validation import ExportError
from tests.test_canonical_source_export import Work, sample, raw
from tests.canonical_source_export_fixture import descriptor, reset, config, guarded, connect, state_digest, write_config, PRIVATE


class PostgreSQLTests(Work):
    @classmethod
    def setUpClass(cls):
        cls.fixture=descriptor(os.environ['F005_FIXTURE_CONFIG'])
    def setUp(self):
        super().setUp();reset(self.fixture);self.cfg=config(self.fixture,self.root)
    def emit(self,g=None,hook=None):
        g=g or 'db-'+uuid.uuid4().hex
        return g,bundle.export(self.cfg,g,hook)
    def write(self,sql,params=None):
        c,_,_=guarded(self.fixture)
        try:
            with c.cursor() as q:q.execute(sql,params)
            c.commit()
        finally:c.close()
    def no_backend(self):
        c,_,_=guarded(self.fixture)
        try:
            with c.cursor() as q:
                q.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND application_name='energy-f005-canonical-export'")
                self.assertEqual(q.fetchone()[0],0)
        finally:c.close()
    def test_real_snapshot_roundtrip_source_unchanged_and_private_excluded(self):
        before=state_digest(self.fixture);g,r=self.emit();self.assertEqual(state_digest(self.fixture),before)
        self.assertTrue(bundle.verify(self.root/g,self.cfg.limits)['verified']);self.assertEqual(r['records'],13)
        output=b''.join(p.read_bytes() for p in (self.root/g).iterdir())
        for canary in (b'TABLE_PRIVATE_CANARY',b'FORBIDDEN_PRIVATE_CANARY',b'SV_MUST_NOT_ESCAPE',self.cfg.connection['password'].encode()):self.assertNotIn(canary,output)
        self.no_backend()
    def test_narrow_role_denies_writes_private_and_temp(self):
        for sql in ("INSERT INTO public.main(building_key) VALUES('must-fail')",'CREATE TEMP TABLE must_fail(x int)',
                    'CREATE TABLE public.must_fail(x int)','SELECT * FROM f005_fixture_control.private_sentinel'):
            c=connect(self.fixture['reader'])
            try:
                with c.cursor() as q:
                    with self.assertRaises(psycopg2.Error) as e:q.execute(sql)
                    self.assertEqual(e.exception.pgcode,'42501')
            finally:c.close()
    def test_counts_and_streams_same_snapshot_concurrent_commit_and_next_deletion(self):
        known=[]
        def hook(point,source):
            if point=='snapshot':
                self.assertIsNotNone(source.snapshot)
                self.write("UPDATE public.main SET permanent_building_identifier='AFTER' WHERE building_key='0000'")
                self.write("DELETE FROM public.buildings WHERE building_key='/lookup.'")
                self.write("DELETE FROM public.addresses WHERE address_key='secondary'")
                self.write("INSERT INTO public.buildings(building_key) VALUES('next-only')")
                known.append(state_digest(self.fixture))
        g,_=self.emit(hook=hook)
        m=json.loads((self.root/g/'manifest.json').read_bytes())
        self.assertEqual([s['records_actual'] for s in m['streams']],[5,3,5])
        self.assertNotIn(b'AFTER',(self.root/g/'main.ndjson').read_bytes())
        self.assertNotIn(b'next-only',(self.root/g/'building.ndjson').read_bytes())
        self.assertEqual(state_digest(self.fixture),known[0])
        next_g,_=self.emit();next_raw=(self.root/next_g/'building.ndjson').read_bytes()
        self.assertIn(b'next-only',next_raw);self.assertNotIn(b'/lookup.',next_raw)
        self.assertIn(b'AFTER',(self.root/next_g/'main.ndjson').read_bytes())
        self.assertEqual(state_digest(self.fixture),known[0]);self.no_backend()
    def test_invalid_keys_and_huge_raw_text_abort_no_skip(self):
        for column,value in [('building_key',''),('building_key','bad\x85'),('building_key','å'*129),
                             ('property_identifier',' '),('address_fin','X'*1048576)]:
            reset(self.fixture)
            # Column name is a test-owned fixed tuple, never user input.
            self.write('UPDATE public.main SET '+column+'=%s WHERE building_key=%s',(value,'0000'))
            before=state_digest(self.fixture);g='bad-'+uuid.uuid4().hex
            with self.assertRaises(ExportError):self.emit(g)
            self.assertFalse((self.root/g).exists());self.assertFalse((self.root/('.stage-'+g)).exists())
            self.assertEqual(state_digest(self.fixture),before);self.no_backend()
    def test_required_table_column_type_permission_and_rls_fail_closed(self):
        for sql in ('DROP TABLE public.addresses','ALTER TABLE public.main DROP COLUMN postal_code',
                    'ALTER TABLE public.main ALTER COLUMN building_key TYPE int USING 1',
                    'REVOKE SELECT ON public.addresses FROM energymap_f005_reader','ALTER TABLE public.main ENABLE ROW LEVEL SECURITY'):
            reset(self.fixture);self.write(sql);g='shape-'+uuid.uuid4().hex
            with self.assertRaises(ExportError):self.emit(g)
            self.assertFalse((self.root/g).exists());self.no_backend()
    def test_role_privilege_drift_fails_before_source(self):
        self.write('GRANT SELECT ON f005_fixture_control.private_sentinel TO energymap_f005_reader')
        try:
            with self.assertRaisesRegex(ExportError,'reader_excess_select'):self.emit('excess')
        finally:self.write('REVOKE SELECT ON f005_fixture_control.private_sentinel FROM energymap_f005_reader')
    def test_statement_lock_and_overall_deadline_cleanup(self):
        for overall,statement,lock in ((5,200,100),(1,5000,1000)):
            c,_,_=guarded(self.fixture)
            with c.cursor() as q:q.execute('LOCK TABLE public.main IN ACCESS EXCLUSIVE MODE')
            cfg=Config(self.cfg.connection,self.root,self.cfg.publisher,self.cfg.license_references,self.cfg.fixture,
                       dict(self.cfg.limits,overall_seconds=overall,statement_ms=statement,lock_ms=lock))
            start=time.monotonic()
            try:
                with self.assertRaises(ExportError):bundle.export(cfg,'lock-'+uuid.uuid4().hex)
            finally:c.rollback();c.close()
            self.assertLess(time.monotonic()-start,7);self.no_backend()
    def test_sql_statement_timeout_distinct_from_lock(self):
        # A fixture-owned test cursor blocks within an ordinary pg_sleep query;
        # production SQL never executes provider functions or sleeps.
        from src.canonical_source_export.reader import Reader
        from src.canonical_source_export.resources import Budget
        cfg=Config(self.cfg.connection,self.root,self.cfg.publisher,self.cfg.license_references,self.cfg.fixture,
                   dict(self.cfg.limits,statement_ms=100))
        with Budget(self.root,cfg.limits) as b:
            with self.assertRaises(psycopg2.Error) as e:
                with Reader(cfg,b) as r:
                    with r.conn.cursor() as q:q.execute('SELECT pg_catalog.pg_sleep(2)')
            self.assertEqual(e.exception.pgcode,'57014')
        self.no_backend()
    def test_connection_loss_and_mapping_failure_release(self):
        def hook(p,s):
            if p=='snapshot':s.conn.close()
        with self.assertRaises(ExportError):self.emit('lost',hook)
        self.assertFalse((self.root/'lost').exists());self.no_backend()
    def test_real_limits_and_scaled_cursor_scratch(self):
        self.write("INSERT INTO public.main(building_key,permanent_building_identifier) SELECT 'scaled-'||i::text,'P-'||i::text FROM generate_series(1,2000) i")
        g,r=self.emit();self.assertEqual(r['records'],2013);self.assertGreater(r['peak_scratch_bytes'],32768)
        self.assertLess(r['peak_scratch_bytes'],4*1024**2)
        self.assertLess(r['peak_stream_buffer_bytes'],self.cfg.limits['buffer_bytes'])
        evidence={k:r[k] for k in ('records','peak_scratch_bytes','peak_stream_buffer_bytes')}
        (PRIVATE/'resource-evidence.json').write_bytes(bundle.encode(evidence));(PRIVATE/'resource-evidence.json').chmod(0o600)
        for lower,code in (({'rows':100},'row_limit'),({'total_bytes':1000},'byte_limit'),
                           ({'scratch_bytes':32768},'operation_failed'),({'buffer_bytes':1000},'buffer_limit')):
            cfg=Config(self.cfg.connection,self.root,self.cfg.publisher,self.cfg.license_references,self.cfg.fixture,dict(self.cfg.limits,**lower))
            gid='cap-'+uuid.uuid4().hex
            with self.assertRaisesRegex(ExportError,'^'+code+'$'):bundle.export(cfg,gid)
            self.assertFalse((self.root/gid).exists())
        self.no_backend()
    def test_real_child_sigkill_stale_cleanup_and_claim(self):
        p=self.root/'source.json';write_config(self.cfg,p)
        marker=self.root/'child-ready'
        code="""import os,signal,sys,time
from pathlib import Path
from src.canonical_source_export.config import load_config
from src.canonical_source_export.bundle import export
cfg=load_config(sys.argv[1])
def hook(p,s):
 if p==sys.argv[4]:
  Path(sys.argv[3]).write_text('ready')
  while True:time.sleep(.1)
export(cfg,sys.argv[2],hook)
"""
        for point,signum in ((p,s) for p in ('snapshot','main','verify','publish') for s in (signal.SIGINT,signal.SIGTERM,signal.SIGKILL)):
            g='kill-'+uuid.uuid4().hex
            child=subprocess.Popen([sys.executable,'-c',code,str(p),g,str(marker),point],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                until=time.monotonic()+15
                while not marker.exists() and child.poll() is None and time.monotonic()<until:time.sleep(.02)
                self.assertTrue(marker.exists(),'owned child did not reach synchronized barrier')
                with self.assertRaisesRegex(ExportError,'writer_active'):self.emit(g)
                os.kill(child.pid,signum);child.communicate(timeout=10)
            finally:
                if child.poll() is None:child.kill();child.communicate(timeout=10)
            marker.unlink(missing_ok=True)
            until=time.monotonic()+5
            while True:
                try:self.no_backend();break
                except AssertionError:
                    if time.monotonic()>until:raise
                    time.sleep(.02)
            self.assertFalse((self.root/g).exists());self.assertTrue((self.root/('.claim-'+g)).exists())
            if signum==signal.SIGKILL:
                self.assertTrue(bundle.cleanup(self.root,g)['cleaned_owned_staging'])
            else:
                self.assertFalse((self.root/('.stage-'+g)).exists())
            with self.assertRaises(ExportError):self.emit(g)
    def test_sigkill_after_rename_preserves_complete_unacknowledged_bundle(self):
        p=self.root/'source.json';write_config(self.cfg,p)
        marker=self.root/'after-rename';g='after-rename'
        code="""import sys,time
from pathlib import Path
from src.canonical_source_export import bundle
from src.canonical_source_export.config import load_config
original=bundle.write_exclusive
def stop_before_ack(path,raw):
 if path.name.startswith('.ack-'):
  Path(sys.argv[2]).write_text('ready')
  while True:time.sleep(.1)
 return original(path,raw)
bundle.write_exclusive=stop_before_ack
bundle.export(load_config(sys.argv[1]),'after-rename')
"""
        child=subprocess.Popen([sys.executable,'-c',code,str(p),str(marker)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            until=time.monotonic()+15
            while not marker.exists() and child.poll() is None and time.monotonic()<until:time.sleep(.02)
            self.assertTrue(marker.exists())
            os.kill(child.pid,signal.SIGKILL);child.communicate(timeout=10)
        finally:
            if child.poll() is None:child.kill();child.communicate(timeout=10)
        result=bundle.verify(self.root/g,self.cfg.limits)
        self.assertTrue(result['verified']);self.assertFalse(result['acknowledged_invocation'])
        with self.assertRaises(ExportError):bundle.cleanup(self.root,g)
        with self.assertRaises(ExportError):self.emit(g)
        self.no_backend()

    def test_actual_infra_validator_emitted_bundle_and_adversarial_inputs(self):
        infra=Path(os.environ['F005_INFRA_ROOT']);g,_=self.emit();path=self.root/g
        bridge=Path(__file__).with_name('canonical_source_export_infra.mjs')
        def run(extra=None):return subprocess.run(['node',str(bridge),str(infra),str(path)]+([json.dumps(extra)] if extra else []),capture_output=True,text=True,timeout=30)
        success=run();self.assertEqual(success.returncode,0,success.stderr);self.assertEqual(json.loads(success.stdout)['counts'],{'main':5,'building':3,'address':5})
        metadata=json.loads(success.stdout)
        (PRIVATE/'validator-evidence.json').write_text(json.dumps(metadata,indent=2)+'\n');(PRIVATE/'validator-evidence.json').chmod(0o600)
        for change in ('unknown','hash','length','count','version','coverage','missing','bom','terminal_lf','key','status'):
            g2,_=self.emit();path=self.root/g2;mp=path/'manifest.json';m=json.loads(mp.read_bytes())
            if change=='unknown':m['unknown']='x'
            elif change=='hash':m['streams'][0]['sha256']='0'*64
            elif change=='length':m['streams'][0]['bytes']+=1
            elif change=='count':m['streams'][0]['records_actual']+=1
            elif change=='version':m['input_version']='wrong'
            elif change=='coverage':m['coverage']['sources'][0]['fi']='available'
            elif change=='missing':(path/'address.ndjson').unlink()
            elif change in ('bom','terminal_lf','key','status'):
                p=path/'main.ndjson';raw=p.read_bytes()
                if change=='bom':raw=b'\xef\xbb\xbf'+raw
                elif change=='terminal_lf':raw=raw[:-1]
                elif change=='key':raw=raw.replace(b'"building_key":"0000"',b'"building_key":""')
                elif change=='status':raw=raw.replace(b'"status":"available"',b'"status":"wrong"')
                p.write_bytes(raw)
                from src.canonical_source_export.validation import digest,bundle_digest
                m['streams'][0]['bytes']=len(raw);m['streams'][0]['sha256']=digest(raw);m['generation']['bundle_sha256']=bundle_digest(m['streams'])
            mp.write_bytes(bundle.encode(m));self.assertNotEqual(run().returncode,0,change)
        g3,_=self.emit();path=self.root/g3;self.assertNotEqual(run({'rows':1}).returncode,0)
        self.no_backend()


if __name__=='__main__':unittest.main()
