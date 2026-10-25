"""Doctor-run persistence/recovery probe; capability transfer stays private."""
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from src.api.auth import get_introspector, ZitadelIntrospector
from src.api.main import app
from src.api.buildings import building_session
from tests.test_auth import claims, settings
from tests.test_user_buildings_db import OwnerDatabaseTests, PATH, guarded_urls

PRIVATE = Path('.codex-orch/runtime/f0014-lifecycle.private.json')


def without_read_time(value):
    if value.get('base'):
        value['base'].pop('resolved_at',None)
    return value


def main():
    guarded_urls()
    if sys.argv[1:] == ['save']:
        OwnerDatabaseTests.setUpClass()
        test=OwnerDatabaseTests('test_summary_adapter_empty_reads_and_readiness_do_not_write')
        try:
            test.setUp()
            saved=test.save({'display_name':'Synthetic shared lifecycle','overrides':{'floor_area':0,'total_area':None},'scenario_inputs':{'resident_count':None}})
            created=test.client.post(PATH+'/share',headers=test.auth(**{'If-Match':saved.headers['etag']}))
            assert created.status_code==201, created.status_code
            public=test.client.get(created.json()['resolver_path'])
            assert public.status_code==200, public.status_code
            owner=test.read()
            payload={'save_pid':os.getpid(),'path':created.json()['resolver_path'],
                     'etag':owner.headers['etag'],'owner':without_read_time(owner.json()),
                     'shared':without_read_time(public.json())}
            descriptor=os.open(PRIVATE,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(descriptor,'w') as handle:json.dump(payload,handle)
            os.chmod(PRIVATE,0o600)
            print('Synthetic owner and share committed; private mode-600 transfer ready.')
        finally:
            test.doCleanups();OwnerDatabaseTests.doClassCleanups()
    elif sys.argv[1:] == ['read']:
        OwnerDatabaseTests.setUpClass()
        Reader=OwnerDatabaseTests.Reader
        expected=json.loads(PRIVATE.read_text())
        def source():
            with Reader() as session:yield session
        provider=ZitadelIntrospector(settings(),transport=httpx.MockTransport(lambda request:httpx.Response(200,json=claims()|{'sub':'A'})),clock=lambda:1000)
        app.dependency_overrides[get_introspector]=lambda:provider
        app.dependency_overrides[building_session]=source
        try:
            with patch('src.api.user_buildings.SessionLocal',Reader), TestClient(app) as client:
                owner=client.get(PATH,headers={'Authorization':'Bearer A.new-session'})
                # Public capability must work while provider is unavailable.
                app.dependency_overrides[get_introspector]=lambda:(_ for _ in ()).throw(AssertionError('provider touched'))
                public=client.get(expected['path'],headers={'Authorization':'Bearer invalid'})
            assert owner.status_code==public.status_code==200
            assert without_read_time(owner.json())==expected['owner']
            assert without_read_time(public.json())==expected['shared']
            assert owner.headers['etag']==expected['etag']
            assert 'etag' not in public.headers
            assert os.getpid()!=expected['save_pid']
            print('Separate API process recovered identical owner/ETag and anonymous shared view, including sparse null/zero and historical summary; provider untouched.')
        finally:
            app.dependency_overrides.clear();OwnerDatabaseTests.doClassCleanups()
    else:raise SystemExit('Use save or read')


if __name__=='__main__':main()
