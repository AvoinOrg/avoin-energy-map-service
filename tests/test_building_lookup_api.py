import asyncio
import io
import logging
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from src.api.main import app
from src.api.building_lookup import LookupBoundary, source_sessions
from src.api.buildings import building_session
from src.api.log_redaction import RedactingFormatter
from src.building_lookup import LookupFailure, build_page
from tests.test_building_lookup import row, SCOPE, NOW
from tests.test_building_lookup_contract import request

URL = '/energy-data/building-lookup'
BODY = {'schema_version':1,'query':{'mode':'permanent_building_identifier','permanent_building_identifier':'0000000001'}}


class LookupApiTests(unittest.TestCase):
    def setUp(self):
        self.factory = Mock(side_effect=AssertionError('unexpected source'))
        app.dependency_overrides[source_sessions] = lambda:self.factory
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_success_is_anonymous_no_store_and_exact_key(self):
        value = build_page(request(),[row('opaque/key')],SCOPE,now=NOW)
        with patch('src.api.building_lookup.lookup',return_value=value) as call:
            response = self.client.post(URL,json=BODY)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers['cache-control'],'no-store')
        self.assertEqual(response.json()['candidates'][0]['building_key'],'opaque/key')
        self.assertEqual(call.call_args.args[0].query.permanent_building_identifier,'0000000001')
        self.factory.assert_not_called()

    def test_all_failures_sanitized_and_distinct(self):
        for code, status in [('lookup_capability_unavailable',503),('lookup_source_unavailable',503),
            ('lookup_source_contract_failure',503),('lookup_identity_conflict',409),
            ('lookup_page_invalidated',409),('lookup_query_too_broad',422)]:
            with patch('src.api.building_lookup.lookup',side_effect=LookupFailure(code)):
                response = self.client.post(URL,json=BODY)
            self.assertEqual(response.status_code,status)
            self.assertEqual(response.json()['detail']['code'],code)
            self.assertEqual(response.headers['cache-control'],'no-store')
        with patch('src.api.building_lookup.lookup',side_effect=RuntimeError('PRIVATE_SQL_DSN_SENTINEL')):
            response = self.client.post(URL,json=BODY)
        self.assertEqual(response.status_code,500)
        self.assertNotIn('SENTINEL',response.text)

    def test_unsupported_modes_validated_before_source_access(self):
        for mode,field,value in [('address','address_fin','Mäkelänkatu 1'),
                                 ('property_identifier','property_identifier','00012345678901')]:
            response = self.client.post(URL,json={'schema_version':1,'query':{'mode':mode,field:value}})
            self.assertEqual(response.status_code,503)
            self.assertEqual(response.json()['detail']['code'],'lookup_capability_unavailable')
            self.assertEqual(response.json()['detail']['mode'],mode)
        self.factory.assert_not_called()

    def test_invalid_body_media_size_and_no_source(self):
        with patch('src.api.building_lookup.lookup') as service:
            for body in ({}, {**BODY,'limit':True}, {**BODY,'schema_version':True},
                         {**BODY,'PRIVATE_UNKNOWN':'PRIVATE_INPUT_SENTINEL'},
                         {**BODY,'query':{'mode':'property_identifier','property_identifier':123}}):
                response = self.client.post(URL,json=body)
                self.assertEqual(response.status_code,422)
                self.assertNotIn('PRIVATE_',response.text)
            for payload in (b'{"PRIVATE_INPUT_SENTINEL":', b'\xff', b'[]'):
                response = self.client.post(URL,content=payload,headers={'Content-Type':'application/json'})
                self.assertEqual(response.status_code,422)
                self.assertNotIn('SENTINEL',response.text)
            self.assertEqual(self.client.post(URL,content=b'{}').status_code,415)
            self.assertEqual(self.client.post(URL,content=b'x'*16385,headers={'Content-Type':'application/json'}).status_code,413)
            self.assertEqual(self.client.post(URL+'?cursor=PRIVATE',json=BODY).status_code,422)
            service.assert_not_called()
        self.factory.assert_not_called()

    def test_chunked_body_bound_before_parser(self):
        messages = iter([{'type':'http.request','body':b'x'*10000,'more_body':True},
                         {'type':'http.request','body':b'x'*6385,'more_body':True}])
        downstream = Mock(side_effect=AssertionError('parser must not run'))
        sent = []
        async def receive(): return next(messages)
        async def send(message): sent.append(message)
        scope = {'type':'http','path':URL,'method':'POST','headers':[(b'content-type',b'application/json')]}
        asyncio.run(LookupBoundary(downstream)(scope,receive,send))
        self.assertEqual(sent[0]['status'],413)
        self.assertIn((b'cache-control',b'no-store'),sent[0]['headers'])
        downstream.assert_not_called()

    def test_framework_cors_methods_no_store_and_canonical_namespace(self):
        for method,path in [('get',URL),('put',URL),('delete',URL+'/unknown')]:
            response = getattr(self.client,method)(path)
            self.assertIn(response.status_code,(404,405))
            self.assertEqual(response.headers['cache-control'],'no-store')
        for origin in ('http://127.0.0.1:8080','https://denied.invalid'):
            response = self.client.options(URL,headers={'Origin':origin,'Access-Control-Request-Method':'POST'})
            self.assertEqual(response.headers['cache-control'],'no-store')
        app.dependency_overrides[building_session] = lambda:object()
        from src.building_resolver import Resolution
        with patch('src.api.buildings.resolve_building',return_value=Resolution('missing')) as resolver:
            response = self.client.get('/energy-data/buildings/lookup')
            self.assertEqual(response.status_code,404)
            self.assertEqual(response.headers['cache-control'],'no-cache')
            self.assertEqual(resolver.call_args.args[1],'lookup')

    def test_openapi_three_modes_closed_schema_no_auth(self):
        spec = self.client.get('/openapi.json').json()
        route = spec['paths'][URL]['post']
        self.assertFalse(route.get('security'))
        for status in ('200','409','413','415','422','503'):
            self.assertIn(status,route['responses'])
        models = spec['components']['schemas']
        query = models['BuildingLookupRequestV1']['properties']['query']
        self.assertEqual(set(query['discriminator']['mapping']),{'address','property_identifier','permanent_building_identifier'})
        self.assertFalse(models['LookupCandidateV1']['additionalProperties'])

    def test_final_logs_and_errors_do_not_echo_sentinels(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(RedactingFormatter('%(message)s'))
        logger = logging.getLogger('lookup_sentinel_test')
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler,handler)
        logger.warning('POST /energy-data/building-lookup?cursor=PRIVATE_CURSOR_SENTINEL Bearer PRIVATE_TOKEN_SENTINEL')
        logger.warning('POST /energy-data/building-lookup%%3Faddress=PRIVATE_ADDRESS_SENTINEL')
        logger.warning('GET /energy-data/shared-buildings/PRIVATE_SHARE_SENTINEL?next=/energy-data/building-lookup')
        self.assertNotIn('SENTINEL',stream.getvalue())
        response = self.client.post(URL,json={**BODY,'PRIVATE_EXTRA':'PRIVATE_BODY_SENTINEL'})
        self.assertNotIn('SENTINEL',response.text)
