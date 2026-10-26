"""Pure/HTTP boundary checks; PostgreSQL acceptance is in the guarded suite."""
import asyncio
from datetime import datetime, timezone
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from src.api.auth import get_introspector, ZitadelIntrospector
from src.api.main import app
from src.db.state import state_engine, StateUnavailable
from src.user_building_contract import encode_cursor, decode_cursor
from src.user_buildings import etag, preconditions, OwnerFailure
from tests.test_auth import claims, settings


class OwnerPureTests(unittest.TestCase):
    def test_snapshot_location_is_independent_of_full_geometry_serialization(self):
        from src.api.buildings import public_example
        from src.building_contract import CanonicalBuildingV1, LocationV1, AvailabilityV1
        from src.user_buildings import capture_summary
        base=CanonicalBuildingV1.model_validate_json(json.dumps(public_example()))
        base.location=LocationV1(longitude=24.0,latitude=60.0)
        base.selection.geometry_status=AvailabilityV1(status='invalid',reason='invalid_geometry')
        summary=capture_summary(base,datetime.now(timezone.utc))
        self.assertEqual(summary.location,base.location)
        self.assertEqual(summary.source_availability['location'].status,'available')

    def test_destructive_database_guard_requires_exact_fixture(self):
        from tests.test_user_buildings_db import guarded_urls, DATABASE
        urls={'OWNER_TEST_ADMIN_URL':f'postgresql://migrator@127.0.0.1:55432/{DATABASE}',
              'OWNER_TEST_READER_URL':f'postgresql://reader@127.0.0.1:55432/{DATABASE}',
              'ENERGY_MAP_STATE_DATABASE_URL':f'postgresql://writer@127.0.0.1:55432/{DATABASE}',
              'RUN_OWNER_BUILDINGS_DB_TESTS':'1'}
        with patch.dict(os.environ,urls,clear=True):self.assertEqual(len(guarded_urls()),3)
        for wrong in ({'RUN_OWNER_BUILDINGS_DB_TESTS':'0'},{'DATABASE_URL':'ambient'},
                      {'OWNER_TEST_ADMIN_URL':'postgresql://admin@localhost:55432/production'},
                      {'OWNER_TEST_READER_URL':urls['OWNER_TEST_ADMIN_URL']}):
            with patch.dict(os.environ,urls|wrong,clear=True),self.assertRaises(RuntimeError):guarded_urls()

    def test_cursor_closed_aware_exact_and_bounded(self):
        now = datetime(2026, 10, 23, 1, 2, 3, 987654, tzinfo=timezone.utc)
        identity = uuid4()
        cursor = encode_cursor(now, identity)
        result = decode_cursor(cursor)
        self.assertEqual((result.updated_at, result.saved_id), (now, identity))
        import base64
        for value in ('', 'bad', 'x'*513, cursor+'=',
                *[base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=') for payload in (
                    {'updated_at': '2026-01-01', 'saved_id': str(identity)},
                    {'schema_version': 2, 'updated_at': now.isoformat(), 'saved_id': str(identity)},
                    {'updated_at': now.isoformat(), 'saved_id': str(identity), 'owner': 'forged'})]):
            with self.subTest(value=value[:20]), self.assertRaises(ValueError):
                decode_cursor(value)

    def test_complete_precondition_parser_and_incarnation(self):
        row = SimpleNamespace(saved_id=uuid4(), revision=1)
        tag = etag(row)
        def headers(pairs):
            return Headers(raw=[(k.encode(), v.encode()) for k,v in pairs])
        cases = [([], row, 428), ([], None, 428), ([('if-match', tag)], None, 412),
            ([('if-none-match', '*')], row, 412), ([('if-match', '"other"')], row, 412),
            ([('if-match', '*')], row, 400), ([('if-match', 'W/'+tag)], row, 400),
            ([('if-match', tag), ('if-match', tag)], row, 400),
            ([('if-match', tag), ('if-none-match', '*')], row, 400),
            ([('if-none-match', '"x"')], None, 400),
            ([('if-match', tag+','+tag)], row, 400),
            ([('if-match', etag(SimpleNamespace(saved_id=uuid4(), revision=1)))], row, 412)]
        for pairs, saved, expected in cases:
            with self.subTest(pairs=pairs), self.assertRaises(OwnerFailure) as result:
                preconditions(headers(pairs), saved, create=True)
            self.assertEqual(result.exception.status, expected)
        preconditions(headers([('if-match', tag)]), row)
        preconditions(headers([('if-none-match', '*')]), None, create=True)

    def test_missing_disabled_malformed_settings_never_connect(self):
        for env in ({}, {'ENERGY_MAP_STATE_ENABLED':'true'},
                {'ENERGY_MAP_STATE_ENABLED':'true', 'ENERGY_MAP_STATE_DATABASE_URL':'sqlite://'},
                {'ENERGY_MAP_STATE_ENABLED':'true', 'ENERGY_MAP_STATE_DATABASE_URL':'postgresql://host/db?options=unsafe'}):
            with patch.dict(os.environ, env, clear=True), patch('src.db.state.create_engine') as create:
                with self.assertRaises(StateUnavailable): state_engine()
                create.assert_not_called()


class OwnerHttpBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.body = claims()
        self.reply = lambda req: httpx.Response(200, json=self.body)
        self.introspector = ZitadelIntrospector(settings(), transport=httpx.MockTransport(lambda r:self.reply(r)), clock=lambda:1000)
        app.dependency_overrides[get_introspector] = lambda: self.introspector
        self.addCleanup(app.dependency_overrides.clear)
        self.environ = patch.dict(os.environ, {'ENERGY_MAP_STATE_ENABLED':'false'})
        self.environ.start(); self.addCleanup(self.environ.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.auth = {'Authorization':'Bearer valid-fixture'}

    def check(self, response, status):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertNotIn('SECRET', response.text)

    def test_protected_routes_fail_closed_public_independent(self):
        with patch('src.api.user_buildings.state_session', side_effect=AssertionError('auth must precede state')):
            for method, path in [('get',''), ('get','/key'), ('put','/key'), ('patch','/key'), ('delete','/key')]:
                kwargs = {'json':{}} if method in ('put','patch') else {}
                response = getattr(self.client,method)('/energy-data/user/buildings'+path, **kwargs)
                self.check(response,401)
                self.assertEqual(response.headers['www-authenticate'],'Bearer')
        self.check(self.client.get('/energy-data/user/buildings', headers=self.auth),503)
        self.check(self.client.get('/readyz/state'),503)
        self.assertEqual(self.client.get('/healthz').status_code,200)

    def test_provider_failures_and_invalid_tokens_no_state(self):
        variants = [({'active':False},401), ({'exp':999},401), ({'aud':'wrong'},401), ({'iss':'wrong'},401)]
        with patch('src.api.user_buildings.state_session', side_effect=AssertionError('no state')):
            for changes,status in variants:
                self.body = {**claims(), **changes}
                self.check(self.client.put('/energy-data/user/buildings/key',json={},headers=self.auth),status)
            self.reply = lambda req: httpx.Response(503,text='SECRET')
            self.check(self.client.put('/energy-data/user/buildings/key',json={},headers=self.auth),503)

    def test_validation_limits_sanitization_and_cors_errors(self):
        for body in ({'owner':'SECRET'}, {'overrides':{'floor_area':True}}, {'set':{}},
                {'overrides':{'main_purpose':10}}, {'display_name':'x'*121}):
            self.check(self.client.put('/energy-data/user/buildings/key',json=body,headers=self.auth),422)
        for raw, expected in ((b'{"overrides":',422), (b' '*32769,413),
                               (b' '*32766+b'{}',503), (b'{"overrides":{"floor_area":NaN}}',422)):
            self.check(self.client.put('/energy-data/user/buildings/key',content=raw,headers={**self.auth,'Content-Type':'application/json'}), expected)
        response=self.client.get('/energy-data/user/buildings',headers={**self.auth,'Origin':'http://127.0.0.1:8080'})
        self.check(response,503)
        self.assertEqual(response.headers['access-control-expose-headers'],'ETag')
        self.assertEqual(response.headers['access-control-allow-origin'],'http://127.0.0.1:8080')
        preflight=self.client.options('/energy-data/user/buildings/key',headers={'Origin':'http://127.0.0.1:8080','Access-Control-Request-Method':'PUT','Access-Control-Request-Headers':'Authorization,Content-Type,If-Match,If-None-Match'})
        self.assertEqual(preflight.status_code,200)
        self.assertIn('If-Match',preflight.headers['access-control-allow-headers'])

    def test_chunked_dishonest_content_length_is_bounded_before_parsing(self):
        async def run():
            chunks=iter([b'x'*20000,b'x'*20000,b'x'*20000])
            reads=[]; messages=[]
            async def receive():
                reads.append(1)
                return {'type':'http.request','body':next(chunks), 'more_body':True}
            async def send(message): messages.append(message)
            scope={'type':'http','asgi':{'version':'3.0'},'method':'PUT','path':'/energy-data/user/buildings/key','raw_path':b'/energy-data/user/buildings/key','query_string':b'','headers':[(b'content-length',b'2')],'scheme':'http','server':('test',80),'client':('test',123)}
            await app(scope,receive,send)
            self.assertEqual(len(reads),2)
            self.assertEqual(messages[0]['status'],413)
            self.assertIn((b'cache-control',b'no-store'),messages[0]['headers'])
        asyncio.run(run())

    def test_openapi_owner_and_share_contract(self):
        schema = self.client.get('/openapi.json').json()
        self.assertIn('/energy-data/shared-buildings/{share_token}', schema['paths'])
        self.assertNotIn('security', schema['paths']['/energy-data/shared-buildings/{share_token}']['get'])
        path=schema['paths']['/energy-data/user/buildings/{building_key}']
        for method in ('get','put','patch','delete'):
            self.assertEqual(path[method]['security'],[{'ZitadelAccessToken':[]}])
        self.assertIn('201',path['put']['responses'])
        self.assertIn('204',path['delete']['responses'])
        for method in ('put','patch','delete'):
            self.assertIn('If-Match',[p['name'] for p in path[method]['parameters']])
