"""Serial required acceptance; no missing prerequisite or skipped test is success."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from .canonical_source_export_fixture import descriptor
from src.canonical_source_export.validation import ExportError

CONTRACT='305dfc61867e4602d2156dc1e547a80481f5028a'
PINNED=('canonical-search-input-v1.schema.json','canonical-search-v1.schema.json',
        'canonical-search-input-v1.examples.json','canonical-search-v1.examples.json','test/canonical-search-contract.test.mjs')
DEPENDENCIES=('src/canonical-input.mjs','src/canonical-validation.mjs',*PINNED,'package-lock.json')


def hashes(root):
    return {p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in DEPENDENCIES}


def main():
    p=argparse.ArgumentParser();p.add_argument('--fixture-config',required=True);p.add_argument('--infra-root',required=True);a=p.parse_args()
    try:
        fixture=Path(a.fixture_config).absolute();descriptor(fixture)
        root=Path(a.infra_root).absolute();before=hashes(root)
        for f in PINNED:
            b=subprocess.check_output(['git','-C',str(root),'show',CONTRACT+':secondary/avoin-map-geocoding/'+f],stderr=subprocess.DEVNULL,timeout=5)
            if hashlib.sha256(b).hexdigest()!=before[f]:raise ExportError('pinned_contract_changed')
        # No application/database/auth ambient configuration enters child suites.
        env={k:os.environ[k] for k in ('PATH','HOME','LANG','LC_ALL','VIRTUAL_ENV') if k in os.environ}
        env.update(F005_FIXTURE_CONFIG=str(fixture),F005_INFRA_ROOT=str(root))
        pure=subprocess.run([sys.executable,'-m','unittest','tests.test_canonical_source_export','tests.test_canonical_source_export_db','-v'],env=env,capture_output=True,text=True,timeout=600)
        output=pure.stdout+pure.stderr
        total=re.search(r'Ran (\d+) tests?',output)
        if pure.returncode or not total or 'skipped' in output.lower():
            names=re.findall(r'^(?:ERROR|FAIL): ([A-Za-z0-9_]+)',output,re.M)
            print(json.dumps({'required_checks':False,'code':'python_checks_failed','tests':names[:20]}));return 1
        node=subprocess.run(['node','--test','test/canonical-search-contract.test.mjs'],cwd=root,env=env,capture_output=True,text=True,timeout=60)
        n=node.stdout+node.stderr
        if node.returncode or '# skipped 0' not in n or '# fail 0' not in n:raise ExportError('infra_baseline_failed')
        if hashes(root)!=before:raise ExportError('validator_changed')
        print(json.dumps({'required_checks':True,'python_tests':int(total[1]),'python_skips':0,
                          'infra_contract_tests':int(re.search(r'# tests (\d+)',n)[1]),'infra_skips':0,
                          'validator_sha256':before},indent=2));return 0
    except BaseException:
        print('{"required_checks":false,"code":"prerequisite_or_execution_failed"}');return 1


if __name__=='__main__':sys.exit(main())
