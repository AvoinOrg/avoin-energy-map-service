"""Sharing's closed HTTP/serialization/logging boundary; DB evidence is separate."""
from datetime import datetime, timezone
import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from starlette.routing import Match

from src.api.main import app
from src.api.buildings import public_example
from src.api.log_redaction import RedactingFormatter, RedactingAccessFormatter, redact
from src.building_contract import CanonicalBuildingV1, PersonalBuildingPutV1, FieldProvenanceV1, AvailabilityV1
from src.building_share_contract import SharedBuildingV1, shared_projection
from src.building_shares import digest, ShareNotFound
from src.user_buildings import capture_summary, page_query
from src.api.auth import AuthenticatedPrincipal


class ShareBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_public_malformed_paths_are_uniform_and_never_authenticate(self):
        paths = ['', '/', '?share_token='+'x'*43, '/bad', '/bad/extra', '/%FF', '/' + 'x'*1000, '/a%2Fb', '/%252F']
        expected = None
        with patch('src.api.auth.get_introspector', side_effect=AssertionError('provider touched')):
            for suffix in paths:
                r = self.client.get('/energy-data/shared-buildings'+suffix, headers={'Authorization': 'Bearer invalid'}, follow_redirects=False)
                self.assertEqual(r.status_code, 404, r.text)
                self.assertEqual(r.headers['cache-control'], 'no-store')
                actual = (r.content, r.headers['content-type'])
                expected = expected or actual
                self.assertEqual(actual, expected)
                self.assertNotIn('etag', r.headers)

    def test_cors_framework_state_and_unexpected_errors_no_store(self):
        path = '/energy-data/shared-buildings/'+'T'*43
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            r=self.client.request(method,path,json={'owner':'forged'})
            self.assertEqual(r.status_code,405)
            self.assertEqual(r.headers['cache-control'],'no-store')
        for origin in ('http://127.0.0.1:8080', 'https://untrusted.invalid'):
            r=self.client.options(path, headers={'Origin':origin,'Access-Control-Request-Method':'GET'})
            self.assertEqual(r.headers['cache-control'],'no-store')
            self.assertEqual(r.status_code,200 if origin.startswith('http:') else 400)
        from contextlib import contextmanager
        @contextmanager
        def broken(**kw):
            raise RuntimeError(path)
            yield
        with patch('src.api.building_shares.state_session',broken):
            r=self.client.get(path,headers={'Origin':'http://127.0.0.1:8080'})
        self.assertEqual(r.headers['access-control-allow-origin'],'http://127.0.0.1:8080')
        self.assertEqual(r.status_code,500)
        self.assertEqual(r.headers['cache-control'],'no-store')
        self.assertNotIn('T'*43,r.text)

    def test_raw_suffix_dispatch_does_not_guess_record_existence(self):
        routes=[r for r in app.routes if getattr(r,'path','').startswith('/energy-data/user/buildings/')]
        def matched(raw,decoded,method):
            scope={'type':'http','method':method,'path':decoded,'raw_path':raw,'root_path':''}
            return next(r.path for r in routes if r.matches(scope)[0] == Match.FULL)
        root='/energy-data/user/buildings/'
        self.assertEqual(matched((root+'a/share').encode(),root+'a/share','DELETE'),root+'{building_key:path}/share')
        self.assertEqual(matched((root+'a%2Fshare').encode(),root+'a/share','DELETE'),root+'{building_key:path}')
        self.assertEqual(matched((root+'a%2Fshare/share').encode(),root+'a/share/share','POST'),root+'{building_key:path}/share')

    def test_nested_public_allowlist_and_sparse_semantics(self):
        base=CanonicalBuildingV1.model_validate_json(json.dumps(public_example()))
        base.identity.address_key='INTERNAL-ADDRESS-SENTINEL'
        base.field_provenance['identity.address_key']=FieldProvenanceV1(origin='ryhti',status='available',source='INTERNAL-META-SENTINEL')
        base.field_provenance['owner_subject']=FieldProvenanceV1(origin='ryhti',status='available',source='OWNER-SENTINEL')
        base.source_availability['identity.address_key']=AvailabilityV1(status='available')
        now=datetime.now(timezone.utc)
        row=SimpleNamespace(saved_id=uuid4(),owner_issuer='PRIVATE-ISSUER',owner_subject='PRIVATE-SUB',
            building_key=base.building_key,display_name='Shared name',schema_version=1,
            overrides={'address_fin':'Shared personal address','floor_area':0,'total_area':None},scenario_inputs={'resident_count':None},
            revision=4,created_at=now,updated_at=now,last_known_summary=capture_summary(base,now).model_dump(mode='json'))
        value=shared_projection(row,base,'available').model_dump(mode='json')
        self.assertEqual(set(value),set(SharedBuildingV1.model_fields))
        self.assertEqual(set(value['base']['identity']),{'permanent_building_identifier','property_identifier'})
        self.assertEqual(value['overrides'],row.overrides)
        self.assertEqual(value['scenario_inputs'],{'resident_count':None})
        self.assertEqual(value['field_provenance']['details.total_area']['status'],'user_unknown')
        self.assertEqual(value['effective']['details']['floor_area'],0)
        serialized=json.dumps(value)
        for private in ('INTERNAL','SENTINEL','PRIVATE',str(row.saved_id),'address_key','owner_subject','token_digest'):
            self.assertNotIn(private,serialized)
        missing=shared_projection(row,None,'missing')
        self.assertEqual(missing.base_status,'unavailable')
        self.assertIsNone(missing.base)
        self.assertIsNone(missing.effective.details.construction_year)
        self.assertEqual(missing.last_known_summary.address_fin,'Example street 1')

    def test_summary_query_has_indexed_exists_without_payload(self):
        sql=str(page_query(AuthenticatedPrincipal('issuer','subject'),100,None))
        self.assertIn('EXISTS',sql)
        self.assertIn('energymap_user_building_shares.saved_id',sql)
        for private in ('token_digest','scenario_inputs','overrides'):
            self.assertNotIn(private,sql)

    def test_openapi_security_closed_schemas_and_conditional_headers(self):
        schema=app.openapi()
        public=schema['paths']['/energy-data/shared-buildings/{share_token}']['get']
        self.assertNotIn('security',public)
        manage=schema['paths']['/energy-data/user/buildings/{building_key}/share']
        for method in ('post','delete'):
            self.assertEqual(manage[method]['security'],[{'ZitadelAccessToken':[]}])
            self.assertIn('If-Match',[p['name'] for p in manage[method]['parameters']])
        for name in ('SharedBuildingV1','SharedBaseV1','SharedIdentityV1','ShareCreatedV1'):
            self.assertFalse(schema['components']['schemas'][name]['additionalProperties'])
        self.assertNotIn('saved_id',schema['components']['schemas']['SharedBuildingV1']['properties'])
        self.assertNotIn('address_key',schema['components']['schemas']['SharedIdentityV1']['properties'])

    def test_checked_examples_and_schema_match_typed_contracts(self):
        from src.api.export_building_contract import contract_examples
        checked = json.loads(Path('src/api/building-api-v1.examples.json').read_text())
        for name, (model, value) in contract_examples().items():
            self.assertEqual(checked[name], value)
            model.model_validate_json(json.dumps(value))
        definitions = json.loads(Path('src/api/building-contract-v1.schema.json').read_text())['$defs']
        for name in ('SharedBuildingV1', 'SharedBaseV1', 'SharedIdentityV1', 'ShareCreatedV1', 'SavedBuildingV1'):
            self.assertIn(name, definitions)
        self.assertEqual(definitions['SavedBuildingV1']['properties']['share_enabled']['type'], 'boolean')

    def test_final_formatted_access_application_and_exception_logs(self):
        token='synthetic'+ '_'*33+'X'
        self.assertEqual(len(token),43)
        from urllib.parse import quote
        path='/energy-data/shared-buildings/'+token+'?trace='+token
        output=io.StringIO()
        handler=logging.StreamHandler(output)
        logger=logging.getLogger('share-test-capture');logger.propagate=False
        logger.addHandler(handler);self.addCleanup(logger.removeHandler,handler)
        logger.setLevel(logging.INFO)
        handler.setFormatter(RedactingFormatter('%(levelname)s %(message)s'))
        for status in (200,404,405,422,503,500):
            logger.info('GET %s status=%s',path,status)
            logger.info('encoded %s',quote(path,safe=''))
        try:
            raise RuntimeError('request '+path+' isolated '+token)
        except RuntimeError:
            logger.exception('request failure')
        handler.setFormatter(RedactingAccessFormatter('%(client_addr)s %(request_line)s %(status_code)s',use_colors=False))
        logger.info('%s - "%s %s HTTP/%s" %d', '127.0.0.1:1','GET',path,'1.1',200)
        handler.setFormatter(RedactingFormatter('%(message)s'))
        logger.info('%s', '/energy-data/shared-buildings/'+token+'?private=QUERY%20PRIVACY%22SENTINEL')
        final=output.getvalue()
        self.assertIn('[REDACTED]',final)
        for private in (token,quote(path,safe=''),digest(token).hex(),'QUERY','PRIVACY','SENTINEL'):
            self.assertNotIn(private,final)
        config=json.loads(Path('src/api/logging.json').read_text())
        self.assertIn('RedactingAccessFormatter',config['formatters']['access']['()'])
        self.assertIn('--log-config /app/src/api/logging.json',Path('entrypoint.sh').read_text())
        self.assertIn('COPY src /app/src',Path('Dockerfile').read_text())
