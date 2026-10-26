"""Doctor-invoked two-process persistence probe; never targets a live database."""
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

EVIDENCE=Path('.codex-orch/features/F001.3-owner-saved-buildings/evidence/lifecycle.json')


def main():
    guarded_urls()
    if sys.argv[1:] == ['save']:
        OwnerDatabaseTests.setUpClass()
        test=OwnerDatabaseTests('test_summary_adapter_empty_reads_and_readiness_do_not_write')
        try:
            test.setUp()
            response=test.save({'display_name':'Lifecycle fixture','overrides':{'floor_area':0,'total_area':None},'scenario_inputs':{'resident_count':None}})
            EVIDENCE.write_text(json.dumps({'save_pid':os.getpid(),'etag':response.headers['etag'],'saved':response.json()},indent=2))
            print('Fixture saved and committed in first API process; null/zero retained.')
        finally:
            test.doCleanups();OwnerDatabaseTests.doClassCleanups()
    elif sys.argv[1:] == ['read']:
        OwnerDatabaseTests.setUpClass()
        Reader=OwnerDatabaseTests.Reader
        def source():
            with Reader() as session:yield session
        provider=ZitadelIntrospector(settings(),transport=httpx.MockTransport(lambda request:httpx.Response(200,json=claims()|{'sub':'A'})),clock=lambda:1000)
        app.dependency_overrides[get_introspector]=lambda:provider
        app.dependency_overrides[building_session]=source
        try:
            with patch('src.api.user_buildings.SessionLocal',Reader), TestClient(app) as client:
                result=client.get(PATH,headers={'Authorization':'Bearer A.new-session'})
            expected=json.loads(EVIDENCE.read_text())
            assert result.status_code == 200, result.status_code
            actual=result.json()
            saved=expected['saved']
            for value in (actual,saved):value['base'].pop('resolved_at',None)
            assert actual == saved
            assert result.headers['etag'] == expected['etag']
            assert os.getpid() != expected['save_pid']
            expected['read_pid']=os.getpid();expected['verified']=True
            EVIDENCE.write_text(json.dumps(expected,indent=2))
            print('Independent API process read identical UUID/revision/timestamps/sparse null/zero/snapshot; new token accepted.')
        finally:
            app.dependency_overrides.clear();OwnerDatabaseTests.doClassCleanups()
    else:
        raise SystemExit('Use save or read')


if __name__=='__main__':main()
