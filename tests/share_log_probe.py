"""Runtime-doctor-only ephemeral Uvicorn wire/log acceptance.

Binds an OS-assigned loopback port and uses only guarded disposable data.
Raw capability transfer and log comparison stay in memory; saved output is redacted.
"""
import io
import json
import logging
from pathlib import Path
import secrets
import socket
import threading
import time
from unittest.mock import patch
from urllib.parse import quote

import httpx
import uvicorn
from src.api.main import app
from src.db.state import StateUnavailable
from src.api.user_buildings import observe
from tests.test_user_buildings_db import OwnerDatabaseTests, PATH, guarded_urls


def main():
    guarded_urls();OwnerDatabaseTests.setUpClass()
    fixture=OwnerDatabaseTests('test_summary_adapter_empty_reads_and_readiness_do_not_write')
    try:
        fixture.setUp()
        saved=fixture.save()
        created=fixture.client.post(PATH+'/share',headers=fixture.auth(**{'If-Match':saved.headers['etag']}))
        assert created.status_code==201
        token=created.json()['share_token'];path=created.json()['resolver_path']
        output=io.StringIO()
        config=uvicorn.Config(app,host='127.0.0.1',port=0,log_config='src/api/logging.json')
        # Capture the actual configured final handlers, preserving formatters.
        handlers=[]
        for name in ('','uvicorn','uvicorn.access'):
            for handler in logging.getLogger(name).handlers:
                handlers.append((handler,handler.stream));handler.setStream(output)
        listener=socket.socket();listener.bind(('127.0.0.1',0))
        port=listener.getsockname()[1]
        server=uvicorn.Server(config)
        thread=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True)
        thread.start()
        try:
            deadline=time.monotonic()+10
            while not server.started and time.monotonic()<deadline:time.sleep(.02)
            assert server.started
            statuses=[]
            with httpx.Client(base_url=f'http://127.0.0.1:{port}',follow_redirects=False,timeout=5) as client:
                def check(method,url,status,**kwargs):
                    r=client.request(method,url,**kwargs)
                    assert r.status_code==status,(r.status_code,status)
                    assert r.headers['cache-control']=='no-store'
                    statuses.append(status)
                    return r
                check('GET',path+'?private='+token,200)
                check('GET','/energy-data/shared-buildings/'+secrets.token_urlsafe(32),404)
                check('GET','/energy-data/shared-buildings/bad/path',404)
                check('GET',path+'/',404)
                check('POST',path,405)
                encoded=''.join(f'%{b:02X}' for b in token.encode())
                check('GET','/energy-data/shared-buildings/'+encoded+'?private='+encoded,200)
                check('POST',PATH+'/share',422,headers=fixture.auth(**{'If-Match':created.headers['etag']}),json={'owner':'forged'})
                with patch('src.api.building_shares.state_session',side_effect=StateUnavailable):
                    check('GET',path,503)
                with patch('src.api.building_shares.observe',return_value=(None,'source_unavailable',None)):
                    check('GET',path,200)
                def unexpected(key):
                    try:raise RuntimeError('synthetic exception '+path+' isolated '+token)
                    except RuntimeError:
                        logging.getLogger('energy.share-probe').exception('Request failure %s',path+'?private='+token)
                    raise RuntimeError('sanitized by boundary')
                with patch('src.api.building_shares.observe',unexpected):check('GET',path,500)
                revoked=fixture.client.delete(PATH+'/share',headers=fixture.auth(**{'If-Match':created.headers['etag']}))
                assert revoked.status_code==204
                check('GET',path,404)
                check('OPTIONS',path,200,headers={'Origin':'http://127.0.0.1:8080','Access-Control-Request-Method':'GET'})
        finally:
            server.should_exit=True;thread.join(timeout=10)
            assert not thread.is_alive()
            listener.close()
            for handler,stream in handlers:handler.setStream(stream)
        formatted=output.getvalue()
        assert '[REDACTED]' in formatted
        assert 'Traceback' in formatted
        for private in (token,encoded,quote(path,safe='')):
            assert private not in formatted,'capability leakage detected'
        target=Path('.codex-orch/features/F001.4-building-share-links/evidence/wire-logs.log')
        target.write_text(formatted)
        print('Ephemeral loopback Uvicorn requests/logs verified; statuses:',','.join(map(str,statuses)))
        print('Final access/application/exception output redacted; no raw or encoded fixture capability retained.')
    finally:
        fixture.doCleanups();OwnerDatabaseTests.doClassCleanups()


if __name__=='__main__':main()
