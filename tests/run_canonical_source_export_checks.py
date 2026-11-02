"""Serial required acceptance; no missing prerequisite or skipped test is success."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from .canonical_source_export_fixture import descriptor, guarded
from src.canonical_source_export.validation import ExportError

CONTRACT='305dfc61867e4602d2156dc1e547a80481f5028a'
VALIDATOR='6625af3577f0797221456aaa65750b2f144c871c'
PINNED=('canonical-search-input-v1.schema.json','canonical-search-v1.schema.json',
        'canonical-search-input-v1.examples.json','canonical-search-v1.examples.json','test/canonical-search-contract.test.mjs')
VALIDATOR_PINNED=('src/canonical-input.mjs','src/canonical-validation.mjs','package.json','package-lock.json')
DEPENDENCIES=(*VALIDATOR_PINNED,*PINNED)
NEGATIVE_VARIANTS=('public-plus-direct-temp','public-plus-direct-temp-grant-option',
                   'direct-database-create','public-schema-create','other-schema-create-no-usage',
                   'direct-temp-membership','nested-temp-membership','noinherit-set-temp-membership',
                   'predefined-role-membership','admin-noinherit-noset-membership',
                   'elevated-reader-flags','elevated-reader-superuser','elevated-reader-createdb',
                   'elevated-reader-replication','elevated-reader-bypassrls',
                   'database-ownership','source-table-ownership','unexpected-reader-setting')


def fixture_helper():
    from src.canonical_source_export.config import safe_path
    path=safe_path(Path(__file__).resolve().parents[1]/'.codex-orch/runtime/f005-canonical-source-export-doctor.py')
    s=path.stat()
    if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid() or stat.S_IMODE(s.st_mode)!=0o700 or s.st_nlink!=1:
        raise ExportError('fixture_helper_invalid')
    return path


def python_checks(command,env):
    result=subprocess.run(command,env=env,capture_output=True,text=True,timeout=600)
    output=result.stdout+result.stderr
    total=re.search(r'Ran (\d+) tests?',output)
    if result.returncode or not total or 'skipped' in output.lower():
        names=re.findall(r'^(?:ERROR|FAIL): ([A-Za-z0-9_]+)',output,re.M)
        print(json.dumps({'required_checks':False,'code':'python_checks_failed','tests':names[:20]}))
        raise ExportError('python_checks_failed')
    return int(total[1])


def hashes(root):
    return {p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in DEPENDENCIES}


def installed_digest(root):
    # Include transitive validator dependencies, not just package-lock intent.
    digest=hashlib.sha256();count=0
    for directory,dirs,files in os.walk(root/'node_modules'):
        dirs.sort()
        for name in sorted(files):
            path=Path(directory)/name
            if path.suffix not in ('.js','.cjs','.mjs','.json'):continue
            digest.update(str(path.relative_to(root)).encode());digest.update(b'\0')
            digest.update(path.read_bytes());count+=1
    if not count:raise ExportError('validator_dependencies_missing')
    return {'files':count,'sha256':digest.hexdigest()}


def main():
    p=argparse.ArgumentParser();p.add_argument('--fixture-config',required=True);p.add_argument('--infra-root',required=True)
    p.add_argument('--infra-git-root',help='Object database for the doctor-owned read-only composed receiver snapshot')
    a=p.parse_args()
    try:
        fixture=Path(a.fixture_config).absolute();fixture_data=descriptor(fixture)
        conn,version,_=guarded(fixture_data);conn.close()
        root=Path(a.infra_root).absolute();before=hashes(root);installed=installed_digest(root)
        git_root=Path(a.infra_git_root).absolute() if a.infra_git_root else root
        for ref,files in ((CONTRACT,PINNED),(VALIDATOR,VALIDATOR_PINNED)):
            for f in files:
                b=subprocess.check_output(['git','-C',str(git_root),'show',ref+':secondary/avoin-map-geocoding/'+f],stderr=subprocess.DEVNULL,timeout=5)
                if hashlib.sha256(b).hexdigest()!=before[f]:raise ExportError('pinned_contract_changed')
        # No application/database/auth ambient configuration enters child suites.
        env={k:os.environ[k] for k in ('PATH','HOME','LANG','LC_ALL','VIRTUAL_ENV') if k in os.environ}
        env.update(F005_FIXTURE_CONFIG=str(fixture),F005_INFRA_ROOT=str(root))
        helper=fixture_helper()
        base=[sys.executable,'-m','unittest']
        suites={'strict':python_checks([*base,'tests.test_canonical_source_export',
                  'tests.test_canonical_source_export_db.PostgreSQLTests','-v'],env)}
        for variant in ('public-only-temp','public-default-acl-temp',*NEGATIVE_VARIANTS):
            case='PublicTempPostgreSQLTests' if variant.startswith(('public-only','public-default')) else 'PolicyPostgreSQLTests'
            suites[variant]=python_checks([sys.executable,str(helper),'--fixture-version',str(version),
                        '--with-f006-variant',variant,'--',
                        *base,'tests.test_canonical_source_export_db.'+case,'-v'],env)
        suites['strict-restored']=python_checks([*base,
            'tests.test_canonical_source_export_db.PostgreSQLTests.test_real_snapshot_roundtrip_source_unchanged_and_private_excluded','-v'],env)
        node=subprocess.run(['node','--test','test/canonical-search-contract.test.mjs'],cwd=root,env=env,capture_output=True,text=True,timeout=60)
        n=node.stdout+node.stderr
        if node.returncode or '# skipped 0' not in n or '# fail 0' not in n:raise ExportError('infra_baseline_failed')
        if hashes(root)!=before or installed_digest(root)!=installed:raise ExportError('validator_changed')
        print(json.dumps({'required_checks':True,'python_tests':sum(suites.values()),'python_suites':suites,'python_skips':0,
                          'infra_contract_tests':int(re.search(r'# tests (\d+)',n)[1]),'infra_skips':0,
                          'receiver_contract_ref':CONTRACT,'receiver_validator_ref':VALIDATOR,
                          'postgres_server_version_num':version,
                          'validator_sha256':before,'installed_dependencies':installed},indent=2));return 0
    except BaseException as error:
        if isinstance(error,ExportError) and error.code=='python_checks_failed':return 1
        print('{"required_checks":false,"code":"prerequisite_or_execution_failed"}');return 1


if __name__=='__main__':sys.exit(main())
