import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.buildings import building_session, ContractRoute, contract_validation_response, public_example
from src.building_contract import CanonicalBuildingV1, PersonalBuildingPutV1, PersonalBuildingPatchV1, ContractValidationError
from src.building_resolver import Resolution, BuildingSourceUnavailable


class BuildingApiTests(unittest.TestCase):
    def setUp(self):
        app.dependency_overrides[building_session]=lambda: object()
        self.addCleanup(app.dependency_overrides.clear)
        self.client=TestClient(app)
        self.addCleanup(self.client.close)

    def test_public_success_allowlist_and_no_auth(self):
        base=CanonicalBuildingV1.model_validate_json(__import__('json').dumps(public_example()))
        with patch('src.api.buildings.resolve_building',return_value=Resolution('available',base)) as resolve:
            response=self.client.get('/energy-data/buildings/000-fixture-opaque-key')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers['cache-control'],'no-cache')
        self.assertEqual(response.json(),public_example())
        self.assertNotIn('overrides',response.json())
        self.assertEqual(resolve.call_args.args[1],'000-fixture-opaque-key')
        with patch('src.api.buildings.resolve_building',return_value=Resolution('missing')) as resolve:
            self.client.get('/energy-data/buildings/opaque%2Fslash')
            self.assertEqual(resolve.call_args.args[1],'opaque/slash')

    def test_missing_ambiguous_unavailable_are_sanitized(self):
        for state,status,code in (('missing',404,'building_not_found'),('ambiguous',409,'building_ambiguous')):
            with patch('src.api.buildings.resolve_building',return_value=Resolution(state)):
                response=self.client.get('/energy-data/buildings/unknown')
            self.assertEqual(response.status_code,status)
            self.assertEqual(response.json()['detail']['code'],code)
            self.assertEqual(response.headers['cache-control'],'no-cache')
        with patch('src.api.buildings.resolve_building',side_effect=BuildingSourceUnavailable('PRIVATE SQL SECRET')):
            response=self.client.get('/energy-data/buildings/unknown')
        self.assertEqual(response.status_code,503)
        self.assertNotIn('SECRET',response.text)

    def test_path_validation_and_no_mutation_placeholders(self):
        with patch('src.api.buildings.resolve_building') as resolve:
            for key in ('a'*257,'key%00'):
                response=self.client.get('/energy-data/buildings/'+key)
                self.assertEqual(response.status_code,422)
                self.assertNotIn('input',response.json()['detail'][0])
            resolve.assert_not_called()
        self.assertEqual(self.client.put('/energy-data/buildings/key',json={}).status_code,405)
        self.assertEqual(self.client.get('/energy-data/user/buildings').status_code,401)
        self.assertEqual(self.client.get('/healthz').json(),{'status':'ok'})
        self.assertEqual(self.client.get('/energy-data/tiles/10/0/0.pbf').content,b'')

    def test_openapi_complete_example_and_error_contract(self):
        schema=self.client.get('/openapi.json').json()
        route=schema['paths']['/energy-data/buildings/{building_key}']['get']
        self.assertFalse(route.get('security'))
        for code in ('200','404','409','422','503'):
            self.assertIn(code,route['responses'])
        example=route['responses']['200']['content']['application/json']['example']
        CanonicalBuildingV1.model_validate_json(__import__('json').dumps(example))

    def test_future_dtos_in_test_only_http_app_use_sanitized_422(self):
        demo=FastAPI();demo.router.route_class=ContractRoute
        @demo.put('/put')
        def put(value:PersonalBuildingPutV1): return value
        @demo.patch('/patch')
        def patch_value(value:PersonalBuildingPatchV1): return value
        with TestClient(demo) as client:
            for method,url,body in (('put','/put',{'owner':'PRIVATE'}),
                                    ('put','/put',{'overrides':{'floor_area':True}}),
                                    ('patch','/patch',{'set':{'overrides':{'floor_area':1}},'unset':['overrides.floor_area']})):
                response=getattr(client,method)(url,json=body)
                self.assertEqual(response.status_code,422)
                self.assertNotIn('PRIVATE',response.text)
                self.assertTrue(all(set(e)=={'loc','type','msg'} for e in response.json()['detail']))
            response=client.patch('/patch',json={'set':{'overrides':{'floor_area':0,'heated_net_area':None}}})
            self.assertEqual(response.status_code,200)
