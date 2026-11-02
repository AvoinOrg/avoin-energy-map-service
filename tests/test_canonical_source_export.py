"""Synthetic unit evidence only. Real snapshot proof is in the guarded DB suite."""
import copy
import io
import json
import os
import shutil
import signal
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from src.canonical_source_export import bundle
from src.canonical_source_export.config import Config, DEFAULTS, load_config
from src.canonical_source_export.projection import TEXT_COLUMNS, project, key, fact
from src.canonical_source_export.resources import Budget
from src.canonical_source_export.validation import ExportError, encode, strict_json, validate, digest

PRIVATE = Path(__file__).resolve().parents[1]/'.codex-orch/runtime/f005-canonical-source-export-private'


def raw(c,k,**values):
    d=dict.fromkeys(TEXT_COLUMNS[c]);d.update(building_key=k,geometry_present=False);d.update(values)
    return d


def sample():
    a=raw('main','0000',permanent_building_identifier='P',property_identifier='00000000000001',
          address_key='display',address_fin='Display FI',postal_code='00100')
    return {'main':[a,dict(a),raw('main','   '),raw('main','é'),raw('main','e\u0301')],
            'building':[raw('building','0000',permanent_building_identifier='Q',property_identifier='BAD'),
                        raw('building','/lookup.',permanent_building_identifier='P'),raw('building','0001')],
            'address':[raw('address','0000',address_key='primary',address_fin='FI',postal_code='00100',municipality_number='091'),
                       raw('address','0000',address_key='secondary',address_fin='Secondary',geometry_present=True),
                       raw('address','/lookup.',address_key='shared',address_fin='FI'),
                       raw('address','orphan',address_key='shared',address_fin='FI'),
                       raw('address','orphan',address_key='shared',address_fin='FI')]}


class FakeReader:
    data=sample()
    def __init__(self,cfg,budget):
        self.snapshot='synthetic-disposable-shape';self.shape={c:{'stable':False,'geometry':['point']} for c in TEXT_COLUMNS}
    def __enter__(self): return self
    def __exit__(self,*a): pass
    def count(self,c): return len(self.data[c])
    def rows(self,c,probe=False): return iter(self.data[c][:1] if probe else self.data[c])


class Work(unittest.TestCase):
    def setUp(self):
        PRIVATE.mkdir(parents=True,exist_ok=True,mode=0o700)
        if PRIVATE.stat().st_mode & 0o777 != 0o700:
            raise RuntimeError('private runtime root must be0700')
        self.root=Path(tempfile.mkdtemp(prefix='unit-',dir=PRIVATE));self.root.chmod(0o700)
        self.cfg=Config({},self.root,'synthetic-unit-fixture',['synthetic-license-not-real-source'],{'fixture':True},
                        dict(DEFAULTS,reserve_bytes=1,disk_bytes=64*1024**2,scratch_bytes=32*1024**2))
    def tearDown(self): shutil.rmtree(self.root)
    def emit(self,g=None,hook=None):
        g=g or 'fixture-'+uuid.uuid4().hex
        with patch.object(bundle,'Reader',FakeReader):
            return g,bundle.export(self.cfg,g,hook)


class ProjectionTests(unittest.TestCase):
    def test_pinned_schema_bytes(self):
        schema=Path(__file__).resolve().parents[1]/'src/canonical_source_export/schemas'
        for name,sha in [('canonical-search-input-v1.schema.json','d96a40da6109c47ae99e76a2831bf424c055362e214c6cfdf1a6e425c9d47468'),
                         ('canonical-search-v1.schema.json','c55afa529f335428b4f4f11d43b738be657450bf19075cd7586d6774238e1250')]:
            self.assertEqual(digest((schema/name).read_bytes()),sha)

    def test_exact_key_domain(self):
        for s in ('0000','/lookup.','   ',' x ','é','e\u0301','å'*128,'\ufeff'):
            with self.subTest(kind='valid'): self.assertEqual(key(s),s)
        for s in (None,1,True,'','x\n','\x85','\ud800','å'*129):
            with self.subTest(kind='invalid'),self.assertRaises(ExportError): key(s)
    def test_fact_states_raw_invalid(self):
        self.assertEqual(fact(None,'postal_code')['status'],'missing')
        self.assertEqual(fact(None,'sv',projected=False)['status'],'not_projected')
        for field,v in [('property_identifier','00000000000000'),('postal_code',' 00100'),('municipality_number','９１'),('modified_timestamp_utc','yesterday')]:
            f=fact(v,field);self.assertEqual(f['status'],'invalid');self.assertEqual(f['value'],v)
        for v in ('','  ','\x00','å'*257):
            with self.assertRaises(ExportError):fact(v,'permanent_building_identifier')
    def test_projection_closed_no_unknown_or_private_columns(self):
        r=raw('address','000',private_canary='SECRET',address_swe='NOT_ALLOWED',address_fin='FI')
        out=project('address',r);self.assertNotIn(b'SECRET',encode(out));self.assertNotIn(b'NOT_ALLOWED',encode(out))
        self.assertIsNone(out['source_row_id']);self.assertEqual(out['address']['sv']['status'],'not_projected')
        out['address']['private']='x'
        with self.assertRaises(ExportError):validate('AddressRecord',out)
    def test_display_separate(self):
        m=project('main',raw('main','x',address_fin='FI'))
        self.assertEqual(m['main_display_address']['kind'],'main_display')
        self.assertEqual(m['main_display_address']['municipality_number']['status'],'not_projected')
        self.assertEqual(m['source_modified']['status'],'not_projected')
        self.assertIsNone(project('building',raw('building','x'))['main_display_address'])
    def test_strict_json(self):
        for raw_bytes in (b'{"x":1,"\\u0078":2}',b'\xef\xbb\xbf{}',b'NaN',b'1e999',b'"\xff"',b'"\\ud800"'):
            with self.assertRaises(ExportError):strict_json(raw_bytes)
        with self.assertRaises(ExportError):strict_json(b'{ "x":1}',True)
        self.assertEqual(strict_json(b'"   "',True),'   ')


class BundleTests(Work):
    def test_verification_uses_bounded_raw_reads_and_charges_buffer(self):
        # Exercise the real verifier on >1MiB of valid synthetic records. Count
        # raw calls rather than timing: unbuffered FileIO.readline reads a byte
        # per call and must fail this check regardless of filesystem speed.
        rows = 2048
        data = {'main': [sample()['main'][0]] * rows, 'building': [], 'address': []}
        with patch.object(FakeReader, 'data', data):
            generation, _ = self.emit()
        directory = self.root / generation
        m = json.loads((directory / 'manifest.json').read_bytes())
        total_bytes = sum(s['bytes'] for s in m['streams'])
        self.assertGreater(total_bytes, 1024**2)
        scratch = self.root / 'buffer-check'
        scratch.mkdir(mode=0o700)
        reads = {'calls': 0, 'bytes': 0, 'max_request': 0}
        buffer_sizes = []

        class CountingFile(io.FileIO):
            def read(self, size=-1):
                reads['calls'] += 1
                reads['max_request'] = max(reads['max_request'], size)
                value = super().read(size)
                reads['bytes'] += len(value)
                return value

            def readinto(self, target):
                reads['calls'] += 1
                reads['max_request'] = max(reads['max_request'], len(target))
                count = super().readinto(target)
                reads['bytes'] += count
                return count

        def counting_fdopen(fd, mode, buffering):
            self.assertEqual(mode, 'rb')
            buffer_sizes.append(buffering)
            raw_file = CountingFile(fd, mode)
            return raw_file if buffering == 0 else io.BufferedReader(raw_file, buffer_size=buffering)

        with Budget(self.root, self.cfg.limits) as budget:
            ordinary_observe = budget.observe
            charged_rows = 0

            def observe(*objects, resident_bytes=0):
                nonlocal charged_rows
                self.assertEqual(resident_bytes, bundle.VERIFY_BUFFER_BYTES)
                ordinary_observe(*objects, resident_bytes=resident_bytes)
                charged_rows += 1

            budget.observe = observe
            with patch.object(bundle.os, 'fdopen', counting_fdopen):
                result = bundle.verify_files(directory, self.cfg.limits, budget, scratch)
            self.assertTrue(result['verified'])
            self.assertEqual(result['records'], rows)
            self.assertEqual(charged_rows, rows)
            self.assertEqual(budget.total, total_bytes)

        self.assertLessEqual(reads['calls'], total_bytes // (8 * 1024) + 6)
        self.assertEqual(buffer_sizes, [8 * 1024] * 3)
        self.assertEqual(reads['bytes'], total_bytes)
        self.assertLessEqual(reads['max_request'], bundle.VERIFY_BUFFER_BYTES)
        evidence = dict(reads, stream_bytes=total_bytes, records=rows,
                        read_buffer_bytes=bundle.VERIFY_BUFFER_BYTES,
                        peak_stream_buffer_bytes=result['peak_stream_buffer_bytes'])
        path = PRIVATE / 'buffer-read-evidence.json'
        path.write_bytes(encode(evidence))
        path.chmod(0o600)

    def test_resident_read_buffer_enforces_stream_budget(self):
        budget = Budget(self.root, self.cfg.limits)
        budget.observe(b'line')
        baseline = budget.peak_stream_buffer_bytes
        budget.observe(b'line', resident_bytes=bundle.VERIFY_BUFFER_BYTES)
        self.assertEqual(budget.peak_stream_buffer_bytes, baseline + bundle.VERIFY_BUFFER_BYTES)
        budget = Budget(self.root, dict(self.cfg.limits, buffer_bytes=256 * 1024))
        payload = b'x' * (58 * 1024)
        budget.observe(payload)
        with self.assertRaisesRegex(ExportError, 'buffer_limit'):
            budget.observe(payload, resident_bytes=bundle.VERIFY_BUFFER_BYTES)

    def test_roundtrip_exact_stats_and_receipt(self):
        g,result=self.emit();self.assertTrue(bundle.verify(self.root/g,self.cfg.limits)['verified'])
        m=json.loads((self.root/g/'manifest.json').read_bytes())
        self.assertEqual(m['statistics'],dict(main_keys=4,building_keys=3,union_keys=6,main_only=3,building_only=2,
            duplicate_identity_rows=1,conflicted_keys=1,address_edges=5,orphan_edges=2,missing_geometry_keys=5,
            missing_address_keys=4,missing_permanent_keys=4,missing_property_keys=5,missing_sv_edges=5,rejected_rows=0))
        self.assertEqual(sorted(p.name for p in (self.root/g).iterdir()),['address.ndjson','building.ndjson','main.ndjson','manifest.json'])
        for p in (self.root/g).iterdir():self.assertEqual(p.stat().st_mode&0o777,0o600)
        self.assertLess(result['peak_scratch_bytes'],self.cfg.limits['scratch_bytes'])
    def test_empty_readable_streams(self):
        with patch.object(FakeReader,'data',{c:[] for c in TEXT_COLUMNS}):g,_=self.emit()
        m=json.loads((self.root/g/'manifest.json').read_bytes())
        self.assertIn('explicit_empty_export',m['coverage']['omissions'])
        self.assertEqual((self.root/g/'main.ndjson').read_bytes(),b'')
        self.assertTrue(bundle.verify(self.root/g,self.cfg.limits)['verified'])
    def test_manifest_hash_pins_metadata(self):
        g,_=self.emit();p=self.root/g/'manifest.json';m=json.loads(p.read_bytes());m['acquisition']['publisher_role']='changed';p.write_bytes(encode(m))
        with self.assertRaisesRegex(ExportError,'manifest_hash'):bundle.verify(self.root/g,self.cfg.limits)
    def test_stream_corruption(self):
        for name in ('main.ndjson','building.ndjson','address.ndjson'):
            g,_=self.emit();p=self.root/g/name;p.write_bytes(p.read_bytes()+b'\n')
            with self.assertRaises(ExportError):bundle.verify(self.root/g,self.cfg.limits)
    def test_count_hash_length_mismatch(self):
        for f,val in [('records_actual',111),('bytes',12),('sha256','0'*64)]:
            g,_=self.emit();p=self.root/g/'manifest.json';m=json.loads(p.read_bytes());m['streams'][0][f]=val;p.write_bytes(encode(m))
            from src.canonical_source_export.resources import Budget
            scratch=self.root/('scratch-'+uuid.uuid4().hex);scratch.mkdir(mode=0o700)
            with Budget(self.root,self.cfg.limits) as b,self.assertRaises(ExportError):bundle.verify_files(self.root/g,self.cfg.limits,b,scratch)
    def test_limits_abort_and_tombstone(self):
        for changes,code in (({'record':100},'record_length'),({'total_bytes':100},'byte_limit'),
                             ({'rows':1},'row_limit'),({'manifest':100},'manifest_limit'),
                             ({'scratch_bytes':4096},'operation_failed'),({'disk_bytes':4096},'disk_limit'),
                             ({'reserve_bytes':10**18},'disk_reserve')):
            g='limit-'+uuid.uuid4().hex;lim=self.cfg.limits.copy();lim.update(changes)
            cfg=Config({},self.root,self.cfg.publisher,self.cfg.license_references,self.cfg.fixture,lim)
            with patch.object(bundle,'Reader',FakeReader),self.assertRaisesRegex(ExportError,'^'+code+'$'):bundle.export(cfg,g)
            self.assertFalse((self.root/g).exists());self.assertFalse((self.root/('.stage-'+g)).exists())
    def test_mapping_failure_no_values_in_diagnostics(self):
        data=sample();data['main'][0]['property_identifier']='SECRET\x00'
        with patch.object(FakeReader,'data',data),self.assertRaises(ExportError) as e:self.emit('invalid')
        self.assertEqual(str(e.exception),'unrepresentable');self.assertFalse((self.root/'invalid').exists())
    def test_reuse_concurrent_writer_and_existing_path(self):
        g,_=self.emit('once')
        with self.assertRaises(ExportError):self.emit('once')
        self.assertTrue(bundle.verify(self.root/g,self.cfg.limits)['verified'])
        with bundle.Publication(self.root,'first'):
            with self.assertRaisesRegex(ExportError,'writer_active'):self.emit('second')
        (self.root/'exists').mkdir(mode=0o700)
        with self.assertRaises(ExportError):self.emit('exists')
        (self.root/'symlink').symlink_to(self.root/'exists',target_is_directory=True)
        with self.assertRaises(ExportError):self.emit('symlink')
    def test_failed_rename_cleanup_no_overwrite(self):
        with patch.object(bundle,'no_replace',side_effect=ExportError('publication_refused')),self.assertRaises(ExportError):self.emit('rename')
        self.assertFalse((self.root/'rename').exists());self.assertFalse((self.root/'.stage-rename').exists())
        with self.assertRaises(ExportError):self.emit('rename')
    def test_after_rename_is_verifiable_not_acknowledged(self):
        actual=bundle.write_exclusive
        def fail_ack(p,b):
            if p.name.startswith('.ack-'):raise ExportError('ack_failure')
            actual(p,b)
        with patch.object(bundle,'write_exclusive',fail_ack),self.assertRaises(ExportError):self.emit('ack')
        r=bundle.verify(self.root/'ack',self.cfg.limits)
        self.assertTrue(r['verified']);self.assertFalse(r['acknowledged_invocation'])
    def test_signals_and_faults_cleanup(self):
        for point in ('snapshot','verify','publish'):
            for sig in (signal.SIGINT,signal.SIGTERM):
                g='signal-'+uuid.uuid4().hex
                def hook(p,s):
                    if p==point:os.kill(os.getpid(),sig)
                with self.assertRaises(ExportError):self.emit(g,hook)
                self.assertFalse((self.root/g).exists());self.assertFalse((self.root/('.stage-'+g)).exists())
    def test_stale_cleanup_preserves_claim_and_foreign(self):
        with bundle.Publication(self.root,'stale') as p:
            p.allocate();p.owned=None
        self.assertTrue(bundle.cleanup(self.root,'stale')['cleaned_owned_staging'])
        self.assertTrue((self.root/'.claim-stale').is_file())
        foreign=self.root/'.stage-foreign';foreign.mkdir(mode=0o700);(foreign/'keep').write_text('owned elsewhere')
        with self.assertRaises(ExportError):bundle.cleanup(self.root,'foreign')
        self.assertTrue((foreign/'keep').is_file())
    def test_stage_substitution_refused(self):
        def hook(p,s):
            if p=='publish':
                (self.root/'.stage-sub').rename(self.root/'original');(self.root/'.stage-sub').mkdir(mode=0o700)
        with self.assertRaises(ExportError):self.emit('sub',hook)
        self.assertTrue((self.root/'original').is_dir());self.assertFalse((self.root/'sub').exists())


if __name__=='__main__':unittest.main()


class ConfigAndCorruptionTests(Work):
    def config_dict(self):
        return {'version':1,'connection':{'host':'127.0.0.1','port':55435,'dbname':'energymap_f005_canonical_export_test',
                'user':'fixture_reader','password':'SYNTHETIC_PASSWORD_CANARY','sslmode':'disable'},
                'fixture':{'marker':'energymap_f005_canonical_export_test','database_oid':123,'server_version_num':170002},
                'output_root':str(self.root),'publisher_role':'fixture','license_references':['synthetic'], 'limits':{}}
    def policy_dict(self):
        return {'mode':'allow-inherited-public-temp','database_name':'energymap_f005_canonical_export_test',
                'database_oid':123,'reader_name':'fixture_reader','reader_oid':456,'server_version_num':170005,
                'evidence_reference':'synthetic-reviewed-fixture-only'}

    def test_versioned_temp_policy_closed_and_strict_default(self):
        from src.canonical_source_export.config import DatabaseTempPolicy
        p=self.root/'source.json'
        bundle.write_exclusive(p,encode(self.config_dict()))
        self.assertEqual(load_config(p).database_temp_policy,DatabaseTempPolicy())
        d=self.config_dict();d.update(version=2,database_temp_policy={'mode':'deny'})
        p.write_bytes(encode(d));self.assertEqual(load_config(p).database_temp_policy,DatabaseTempPolicy())
        # Production transport in this parser-only test; never connect anywhere.
        d['connection'].update(sslmode='verify-full',sslrootcert=str(p))
        d['database_temp_policy']=self.policy_dict()
        for version in (170002,170005):
            d['database_temp_policy']['server_version_num']=version
            d['fixture']['server_version_num']=version
            p.write_bytes(encode(d));cfg=load_config(p)
            self.assertEqual(cfg.database_temp_policy.reader_oid,456)
            self.assertEqual(cfg.database_temp_policy.server_version_num,version)
        bad=[]
        for field in self.policy_dict():
            v=copy.deepcopy(d);del v['database_temp_policy'][field];bad.append(v)
            v=copy.deepcopy(d);v['database_temp_policy'][field]=None;bad.append(v)
        for field,value in [('mode','allow'),('unknown',1),('database_oid',True),('reader_oid',False),
                            ('reader_oid',0),('database_oid',4294967296),
                            ('server_version_num',True),('database_name','recreated'),('reader_name','other'),
                            ('evidence_reference',''),('evidence_reference',' '),('evidence_reference','x'*193),
                            ('evidence_reference','https://example.invalid'),('evidence_reference','secret\n')]:
            v=copy.deepcopy(d);v['database_temp_policy'][field]=value;bad.append(v)
        for version in (160005,170001,170003,170004,170006,180000):
            v=copy.deepcopy(d);v['database_temp_policy']['server_version_num']=version;bad.append(v)
        for version in (1,3,True,None,'2'):
            v=copy.deepcopy(d);v['version']=version;bad.append(v)
        for policy in ({'mode':'deny','reader_oid':456},None,[],{},True):
            v=copy.deepcopy(d);v['database_temp_policy']=policy;bad.append(v)
        v=copy.deepcopy(d);del v['database_temp_policy'];bad.append(v)
        v=copy.deepcopy(d);v['connection']['sslmode']='disable';bad.append(v)
        for i,v in enumerate(bad):
            with self.subTest(case=i):
                p.write_bytes(encode(v))
                with self.assertRaises(ExportError):load_config(p)
        p.write_bytes(encode(d).replace(b'"reader_oid":456',b'"reader_oid":456,"reader_oid":456'))
        with self.assertRaises(ExportError):load_config(p)

    def test_fixed_fixture_profiles_reject_mixed_identity_and_version_drift(self):
        from tests.canonical_source_export_fixture import FIXTURES, descriptor, guarded
        for version,profile in FIXTURES.items():
            d={'version':1,'fixture_marker':profile['marker'],'database':profile['database'],
               'container_name':profile['container'],'endpoint':{'host':'localhost','port':profile['port']},
               'postgres_server_version_num':version,'server_identity':'synthetic-test-identity'}
            for account in ('reader','maintenance'):
                d[account]={'host':'localhost','port':profile['port'],'dbname':profile['database'],
                    'user':profile[account],'password':'SYNTHETIC_PASSWORD_CANARY','sslmode':'verify-full',
                    'sslrootcert':str(profile['root']/'tls/ca.crt')}
            path=profile['root']/'fixture.json'
            with patch('tests.canonical_source_export_fixture.private_read',return_value=encode(d)):
                self.assertEqual(descriptor(path),d)
                with self.assertRaisesRegex(ExportError,'fixture_guard'):descriptor(self.root/'copied-fixture.json')
            other=FIXTURES[170005 if version==170002 else 170002]
            for field,value in (('postgres_server_version_num',170003),('postgres_server_version_num',True),
                                ('fixture_marker',other['marker']),('database',other['database']),
                                ('container_name',other['container']),('endpoint',{'host':'localhost','port':other['port']}),
                                ('reader',dict(d['reader'],user=other['reader'])),
                                ('maintenance',dict(d['maintenance'],sslrootcert=str(other['root']/'tls/ca.crt')))):
                mixed=dict(d,**{field:value})
                with patch('tests.canonical_source_export_fixture.private_read',return_value=encode(mixed)),self.assertRaisesRegex(ExportError,'fixture_guard'):
                    descriptor(path)
            with patch('tests.canonical_source_export_fixture.connect') as connect:
                conn=connect.return_value;q=conn.cursor.return_value.__enter__.return_value
                q.fetchall.return_value=[(profile['marker'],d['server_identity'])]
                q.fetchone.return_value=(profile['database'],profile['maintenance'],version+1,123)
                with self.assertRaisesRegex(ExportError,'fixture_identity'):guarded(d)
                conn.close.assert_called_once()
    def test_protected_closed_config_and_environment(self):
        p=self.root/'source.json';bundle.write_exclusive(p,encode(self.config_dict()))
        self.assertEqual(load_config(p).connection['dbname'],'energymap_f005_canonical_export_test')
        for k,v in [('unknown',1),('limits',{'buffer_bytes':1}),('fixture',None)]:
            d=self.config_dict();d[k]=v;p.write_bytes(encode(d))
            with self.assertRaises(ExportError):load_config(p)
        p.write_bytes(encode(self.config_dict()))
        with patch.dict(os.environ,{'PGSERVICE':'sensitive_canary'}),self.assertRaisesRegex(ExportError,'ambient_pg_environment'):load_config(p)
        p.chmod(0o644)
        with self.assertRaises(ExportError):load_config(p)
        p.chmod(0o600);link=self.root/'link';link.symlink_to(p)
        with self.assertRaises(ExportError):load_config(link)
    def test_adversarial_records_rejected_with_correct_stream_hashes(self):
        from src.canonical_source_export.resources import Budget
        for mutation in ('unknown','bad_key','status','bom','terminal_lf','nonfinite','duplicate','utf8','component'):
            g,_=self.emit();p=self.root/g/'main.ndjson';lines=p.read_bytes().splitlines(keepends=True)
            row=json.loads(lines[0]);line=lines[0]
            if mutation=='unknown':row['private']='CANARY'
            elif mutation=='bad_key':row['building_key']=''
            elif mutation=='status':row['permanent']['reason']='not_null'
            elif mutation=='component':row['component']='building'
            if mutation in ('unknown','bad_key','status','component'):line=encode(row)
            elif mutation=='bom':line=b'\xef\xbb\xbf'+line
            elif mutation=='terminal_lf':lines[-1]=lines[-1][:-1]
            elif mutation=='nonfinite':line=line.replace(b'"source_row_id":null',b'"source_row_id":NaN')
            elif mutation=='duplicate':line=line.replace(b'"source_row_id":null',b'"source_row_id":null,"source_row_id":null')
            elif mutation=='utf8':line=b'\xff'+line
            lines[0]=line;raw_bytes=b''.join(lines);p.write_bytes(raw_bytes)
            mp=self.root/g/'manifest.json';m=json.loads(mp.read_bytes());m['streams'][0]['bytes']=len(raw_bytes);m['streams'][0]['sha256']=digest(raw_bytes)
            m['generation']['bundle_sha256']=bundle.bundle_digest(m['streams']);mp.write_bytes(encode(m))
            scratch=self.root/('check-'+uuid.uuid4().hex);scratch.mkdir(mode=0o700)
            with Budget(self.root,self.cfg.limits) as b,self.assertRaises(ExportError):bundle.verify_files(self.root/g,self.cfg.limits,b,scratch)
    def test_deadline_and_disk_fault(self):
        import time
        cfg=Config({},self.root,self.cfg.publisher,self.cfg.license_references,self.cfg.fixture,dict(self.cfg.limits,overall_seconds=1))
        def hook(p,s):
            if p=='snapshot':time.sleep(10)
        with patch.object(bundle,'Reader',FakeReader),self.assertRaisesRegex(ExportError,'deadline_exceeded'):bundle.export(cfg,'deadline',hook)
        import errno
        with patch.object(bundle.os,'fsync',side_effect=OSError(errno.ENOSPC,'SENSITIVE_CANARY')),self.assertRaises(ExportError):self.emit('enospc')
        self.assertFalse((self.root/'enospc').exists())
