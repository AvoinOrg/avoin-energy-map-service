"""Private staging, independently audited receipts and atomic Linux publication."""
import ctypes
import fcntl
import hashlib
import os
import shutil
import stat
import uuid
from functools import wraps
from .audit import Audit
from .config import private_dir, private_read, protected_root, generation_id, safe_path
from .projection import project
from .reader import Reader, now
from .resources import Budget
from .validation import ExportError, encode, strict_json, validate, digest, bundle_digest

COMPONENTS = ('main','building','address')
VERIFY_BUFFER_BYTES = 8 * 1024


def sanitized(operation):
    @wraps(operation)
    def call(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except ExportError:
            raise
        except BaseException:
            raise ExportError('operation_failed') from None
    return call


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_exclusive(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())


def no_replace(source, target):
    # renameat2 is atomic only within a supported local filesystem. No fallback
    # to overwrite-capable rename/copy, including when the target is empty.
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, 'renameat2', None)
    if rename is None:
        raise ExportError('atomic_publication_unsupported')
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1):
        raise ExportError('publication_refused')


def local_filesystem(root):
    libc = ctypes.CDLL(None, use_errno=True)
    result = ctypes.create_string_buffer(256)
    if libc.statfs(os.fsencode(root), result):
        raise ExportError('filesystem_unsupported')
    magic = ctypes.c_long.from_buffer(result).value & 0xffffffff
    if magic not in (0xef53, 0x58465342, 0x9123683e, 0x794c7630):
        raise ExportError('filesystem_unsupported')


class Publication:
    def __init__(self, root, generation):
        self.root, self.generation = protected_root(root), generation_id(generation)
        self.stage = self.root / ('.stage-'+generation)
        self.final = self.root / generation
        self.claim = self.root / ('.claim-'+generation)
        self.lock = None
        self.owned = None

    def __enter__(self):
        local_filesystem(self.root)
        self.lock = os.open(self.root / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        s = os.fstat(self.lock)
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o600 or s.st_nlink != 1:
            os.close(self.lock)
            self.lock = None
            raise ExportError('unsafe_file')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock)
            self.lock = None
            raise ExportError('writer_active') from None
        return self

    def allocate(self):
        # Durable never-reuse claim precedes raw source reads.
        write_exclusive(self.claim, encode({'version':1,'producer':'energy-canonical-source-export-v1','generation':self.generation,'token':uuid.uuid4().hex}))
        fsync_dir(self.root)
        if self.final.exists() or self.final.is_symlink():
            raise ExportError('generation_exists')
        self.stage.mkdir(mode=0o700)
        self.owned = self.stage.stat().st_ino
        write_exclusive(self.root / ('.owner-'+self.generation), encode({'inode':self.owned,'claim_sha256':digest(private_read(self.claim))}))
        fsync_dir(self.stage)
        fsync_dir(self.root)

    def assert_owned(self):
        private_dir(self.stage)
        claim = strict_json(private_read(self.claim))
        if (set(claim) != {'version','producer','generation','token'} or claim['version'] != 1 or
            claim['producer'] != 'energy-canonical-source-export-v1' or claim['generation'] != self.generation):
            raise ExportError('staging_identity')
        owner = strict_json(private_read(self.root / ('.owner-'+self.generation)))
        if self.stage.stat().st_ino != self.owned or owner != {'inode':self.owned,'claim_sha256':digest(private_read(self.claim))}:
            raise ExportError('staging_identity')

    def publish(self):
        self.assert_owned()
        fsync_dir(self.stage)
        no_replace(self.stage, self.final)
        self.owned = None
        fsync_dir(self.root)
        write_exclusive(self.root / ('.ack-'+self.generation), encode({'generation':self.generation,'acknowledged':True}))
        fsync_dir(self.root)

    def __exit__(self, *args):
        try:
            if self.owned is not None:
                # Never recursively remove a substituted stage/link/foreign dir.
                try:
                    self.assert_owned()
                    shutil.rmtree(self.stage)
                    fsync_dir(self.root)
                except (OSError, ExportError):
                    pass
        finally:
            if self.lock is not None:
                os.close(self.lock)


def coverage(audit, fixture, empty):
    omissions = ['provider_generation_unknown','upstream_deletions_unknown','sv_not_projected','raw_address_components_not_projected']
    if fixture:
        omissions.append('disposable_fixture_not_provider_evidence')
    if empty:
        omissions.append('explicit_empty_export')
    sources=[]
    for c in COMPONENTS:
        available, count = audit.fi[c]
        sources.append({'source':c,'projection_version':'energy-identity-address-v1',
            'source_version':None,'source_version_status':'unknown',
            'modes': {'address':'unsupported' if c=='building' else 'partial',
                      'property_identifier':'unsupported' if c=='address' else 'partial',
                      'permanent_building_identifier':'unsupported' if c=='address' else 'partial'},
            'membership':'complete_export','address_edges':'complete_export' if c=='address' else 'not_applicable',
            'fi': 'not_applicable' if c=='building' else 'available' if count and available==count else 'partial',
            'sv':'not_projected','omissions':['sv_not_projected','nullable_or_invalid_source_facts']})
    return {'coverage_version':'canonical-coverage-v1','scope':'coherent_export','national_completeness':'unknown',
            'national_evidence':None,'sources':sources,'omissions':omissions}


def manifest(generation, cfg, snapshot, start, completed, streams, audit):
    statistics = audit.statistics()
    return validate('Manifest', {'input_version':'canonical-input-v1','projection_profile':'energy-identity-address-v1',
        'matching_policy':'canonical-match-v1',
        'generation':{'id':generation,'bundle_sha256':bundle_digest(streams),'export_id':str(uuid.uuid4()),
                      'provider_generation':None,'provider_generation_status':'unknown','exported_at':now()},
        'acquisition':{'interface':'energy-analytical-readonly-export','publisher_role':cfg.publisher,
                       'snapshot_id':snapshot,'consistency':'repeatable_read','started_at':start,'completed_at':completed,
                       'upstream_snapshot_evidence':None,'license_references':cfg.license_references,
                       'upstream_deletions_reconciled':False},
        'canonical_parity':'identity_only_confirmation_required','streams':streams,'statistics':statistics,
        'coverage':coverage(audit, cfg.fixture, not statistics['union_keys'])})


def manifest_semantics(m):
    validate('Manifest',m)
    if bundle_digest(m['streams']) != m['generation']['bundle_sha256']:
        raise ExportError('bundle_hash')
    for c,s in zip(COMPONENTS,m['streams']):
        if s['file']!=c+'.ndjson' or s['status']!='complete' or s['records_expected']!=s['records_actual'] or s['sha256'] is None:
            raise ExportError('stream_receipt',c)
    a, c = m['acquisition'], m['coverage']
    if ((a['interface']=='synthetic') != (c['scope']=='synthetic_fixture') or
        (a['interface']=='synthetic') != (a['consistency']=='synthetic')):
        raise ExportError('scope_invalid')
    from datetime import datetime
    try:
        for t in (a['started_at'],a['completed_at'],m['generation']['exported_at']):
            if datetime.strptime(t,'%Y-%m-%dT%H:%M:%SZ').strftime('%Y-%m-%dT%H:%M:%SZ')!=t:
                raise ValueError()
    except ValueError:
        raise ExportError('timestamp_invalid') from None
    if not a['started_at']<=a['completed_at']<=m['generation']['exported_at'] or m['statistics']['rejected_rows']:
        raise ExportError('manifest_invalid')
    if not m['statistics']['union_keys'] and 'explicit_empty_export' not in c['omissions']:
        raise ExportError('empty_evidence')
    if c['national_completeness']!='unknown' or c['national_evidence'] is not None or a['upstream_snapshot_evidence'] is not None or a['upstream_deletions_reconciled']:
        # This qualified projection never claims provider completeness.
        raise ExportError('unsupported_lineage_claim')
    if [s['source'] for s in c['sources']]!=list(COMPONENTS):
        raise ExportError('source_coverage')


def verify_files(directory, limits, budget, scratch, manifest_hash=None):
    private_dir(directory)
    raw = private_read(directory/'manifest.json',limits['manifest'])
    if manifest_hash is not None and digest(raw)!=manifest_hash:
        raise ExportError('manifest_hash')
    m = strict_json(raw)
    manifest_semantics(m)
    audit = Audit(scratch,budget)
    try:
        for c,s in zip(COMPONENTS,m['streams']):
            path = safe_path(directory/s['file'])
            # Fixed read-ahead avoids raw per-byte readline I/O. Charge its
            # resident allocation separately from the bounded line/row below.
            fd = os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            with os.fdopen(fd,'rb',buffering=VERIFY_BUFFER_BYTES) as f:
                st=os.fstat(f.fileno())
                if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o600 or st.st_nlink!=1:
                    raise ExportError('unsafe_file')
                h=hashlib.sha256()
                size=count=0
                while True:
                    budget.check(scratch)
                    line=f.readline(limits['record']+1)
                    if not line:
                        break
                    if len(line)>limits['record'] or not line.endswith(b'\n') or line==b'\n':
                        raise ExportError('record_length',c)
                    budget.add(len(line))
                    size+=len(line)
                    count+=1
                    if count>limits['rows']:
                        raise ExportError('row_limit',c)
                    h.update(line)
                    row=validate('AddressRecord' if c=='address' else 'IdentityRecord',strict_json(line[:-1],True))
                    if (c=='address' and (row['address']['kind']!='address_edge' or row['address']['namespace']!='energy.address-key-v1') or
                        c!='address' and (row['component']!=c or c=='building' and row['main_display_address'] is not None or
                         c=='main' and row['main_display_address'] is not None and
                         (row['main_display_address']['kind']!='main_display' or row['main_display_address']['namespace']!='energy.main-display-v1'))):
                        raise ExportError('component_invalid',c)
                    budget.observe(line,row,resident_bytes=VERIFY_BUFFER_BYTES)
                    audit.add(c,row)
                if (size,count,h.hexdigest())!=(s['bytes'],s['records_actual'],s['sha256']):
                    raise ExportError('stream_receipt',c)
        if audit.statistics()!=m['statistics']:
            raise ExportError('statistics_mismatch')
        expected=coverage(audit,'disposable_fixture_not_provider_evidence' in m['coverage']['omissions'],not m['statistics']['union_keys'])
        if m['coverage']!=expected:
            raise ExportError('coverage_mismatch')
        return {'verified':True,'manifest_sha256':digest(raw),'records':sum(s['records_actual'] for s in m['streams']),
                'peak_scratch_bytes':budget.peak_scratch,'peak_stream_buffer_bytes':budget.peak_stream_buffer_bytes,'relationship_reuse_observations':audit.reused}
    finally:
        audit.close()


@sanitized
def export(cfg, generation, hook=None):
    generation_id(generation)
    with Budget(cfg.root,cfg.limits) as budget, Publication(cfg.root,generation) as pub:
        budget.check()
        pub.allocate()
        scratch=pub.stage/'.scratch'
        scratch.mkdir(mode=0o700)
        audit=Audit(scratch,budget)
        try:
            start=now()
            with Reader(cfg,budget) as source:
                counts={c:source.count(c) for c in COMPONENTS}
                if hook:
                    hook('snapshot',source)
                streams=[]
                for c in COMPONENTS:
                    h=hashlib.sha256()
                    size=count=0
                    fd=os.open(pub.stage/(c+'.ndjson'),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                    with os.fdopen(fd,'wb',buffering=0) as f:
                        for raw_row in source.rows(c):
                            row=project(c,raw_row,source.shape[c]['stable'])
                            if not source.shape[c]['geometry']:
                                row['geometry']='not_projected'
                            line=encode(row)
                            if len(line)>cfg.limits['record']:
                                raise ExportError('record_length',c)
                            budget.observe(raw_row,row,line)
                            budget.add(len(line))
                            count+=1
                            if count>cfg.limits['rows']:
                                raise ExportError('row_limit',c)
                            f.write(line)
                            h.update(line)
                            size+=len(line)
                            audit.add(c,row)
                            if hook:
                                hook(c,source)
                        os.fsync(f.fileno())
                    if count!=counts[c]:
                        raise ExportError('count_mismatch',c)
                    streams.append({'file':c+'.ndjson','status':'complete','records_expected':counts[c],
                                    'records_actual':count,'bytes':size,'sha256':h.hexdigest()})
                snapshot=source.snapshot
            # Source transaction is released before expensive local reread.
            m=manifest(generation,cfg,snapshot,start,now(),streams,audit)
            raw=encode(m)
            if len(raw)>cfg.limits['manifest']:
                raise ExportError('manifest_limit')
            write_exclusive(pub.stage/'manifest.json',raw)
        finally:
            audit.close()
        shutil.rmtree(scratch)
        scratch.mkdir(mode=0o700)
        budget.bundle_bytes=budget.total
        budget.total=0  # Reread cap is the same delivered byte cap, not double.
        if hook:
            hook('verify',None)
        receipt=verify_files(pub.stage,cfg.limits,budget,scratch,digest(raw))
        shutil.rmtree(scratch)
        pub.assert_owned()
        fsync_dir(pub.stage)
        receipt.update(generation=generation,verified_before_publish=True)
        write_exclusive(cfg.root/('.receipt-'+generation),encode(receipt))
        fsync_dir(cfg.root)
        if hook:
            hook('publish',None)
        pub.publish()
        return receipt


@sanitized
def verify(bundle, limits):
    bundle=private_dir(bundle)
    root=protected_root(bundle.parent)
    generation_id(bundle.name)
    receipt=strict_json(private_read(root/('.receipt-'+bundle.name)))
    with Budget(root,limits) as budget, Publication(root,'verify-'+uuid.uuid4().hex) as work:
        budget.bundle_bytes=sum((bundle/(c+'.ndjson')).lstat().st_size for c in COMPONENTS)
        work.allocate()
        scratch=work.stage/'.scratch'
        scratch.mkdir(mode=0o700)
        result=verify_files(bundle,limits,budget,scratch,receipt['manifest_sha256'])
        result['acknowledged_invocation']=(root/('.ack-'+bundle.name)).is_file()
        return result


@sanitized
def cleanup(root,generation):
    """Guarded stale staging removal only; claim and complete outputs survive."""
    with Publication(root,generation) as pub:
        if pub.final.exists() or pub.final.is_symlink():
            raise ExportError('complete_generation_preserved')
        private_dir(pub.stage)
        pub.owned=pub.stage.stat().st_ino
        pub.assert_owned()
        shutil.rmtree(pub.stage)
        pub.owned=None
        fsync_dir(pub.root)
    return {'cleaned_owned_staging':True,'generation_unavailable_for_reuse':True}


@sanitized
def preflight(cfg):
    with Budget(cfg.root,cfg.limits) as budget:
        local_filesystem(cfg.root)
        budget.check()
        with Reader(cfg,budget) as source:
            for c in COMPONENTS:
                for raw in source.rows(c,True):
                    project(c,raw,source.shape[c]['stable'])
            estimates={c:s['estimated_rows'] for c,s in source.shape.items()}
        st=os.statvfs(cfg.root)
        return {'read_only':True,'repeatable_read':True,'utf8':True,'narrow_reader':True,
                'required_projections_readable':True,'catalog_estimated_rows':estimates,
                'free_bytes':st.f_bavail*st.f_frsize,'free_inodes':st.f_favail,
                'full_population_verified':False,'operational_export_ready':False}
