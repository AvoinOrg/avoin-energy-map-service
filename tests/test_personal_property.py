"""Personal property boundaries and purpose-only reference validation."""
from datetime import datetime, timezone
import json
import hashlib
import os
import subprocess
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
import jsonschema
from pydantic import ValidationError

from src.api.building_examples import saved_example, public_example
from src.api.main import app
from src.building_contract import CanonicalBuildingV1, personal_document, sparse, validation_errors
from src.personal_property_contract import APARTMENT_FIELDS, PROPERTY_FIELDS, REQUIRED_PROPERTY_PATHS
from src.personal_utilities_contract import PersonalBuildingPutV5, PersonalBuildingPatchV5, ScenarioInputsV5, patch_v5, project_v5
from src.purpose_catalog import PurposeCatalogUnavailable, load_purpose_catalog, content_version, SNAPSHOT
from src import user_buildings
from src.building_share_contract import shared_projection
from tests.test_user_buildings import OwnerHttpBoundaryTests
from tests.purpose_catalog_fixture import adopted_pin, synthetic_catalog


class PersonalPropertyTests(unittest.TestCase):
    def test_sparse_roundtrips_all_counts_and_no_total_inequalities(self):
        for name in APARTMENT_FIELDS:
            for value in (None, 0, 1, 1_000_000):
                with self.subTest(name=name, value=value):
                    doc = PersonalBuildingPutV5(schema_version=5, scenario_inputs=ScenarioInputsV5(**{name: value}))
                    raw = personal_document(doc)
                    self.assertEqual(raw['scenario_inputs'], {name: value})
                    self.assertEqual(personal_document(PersonalBuildingPutV5.model_validate_json(json.dumps(raw))), raw)
            for value in (True, False, -1, 1_000_001, 1.0, 1.5, '0', float('nan'), float('inf'), {}):
                with self.subTest(name=name, value=type(value)), self.assertRaises(ValidationError) as failure:
                    PersonalBuildingPutV5.model_validate({'schema_version': 5, 'scenario_inputs': {name: value}})
                self.assertEqual(validation_errors(failure.exception)[0]['loc'], ['body', 'scenario_inputs', name])
        doc = PersonalBuildingPutV5.model_validate({'schema_version': 5,
            'overrides': {'apartment_count': 0}, 'scenario_inputs': dict.fromkeys(APARTMENT_FIELDS, 1_000_000)})
        self.assertEqual(doc.overrides.apartment_count, 0)
        self.assertEqual(personal_document(PersonalBuildingPutV5(schema_version=5))['scenario_inputs'], {})

    def test_sparse_reset_and_precise_conflicts(self):
        old = PersonalBuildingPutV5.model_validate({'schema_version': 5, 'display_name': 'Home', 'overrides': {'total_area': None},
                                                   'scenario_inputs': {'resident_count': 2}})
        updated = patch_v5(old, PersonalBuildingPatchV5.model_validate({'schema_version': 5,
            'set': {'scenario_inputs': {'apartment_studio_count': 0, 'apartment_two_room_count': None}}}), None)
        reset = patch_v5(updated, PersonalBuildingPatchV5.model_validate({'schema_version': 5,
            'unset': ['scenario_inputs.apartment_studio_count']}), None)
        self.assertEqual(sparse(reset.scenario_inputs), {'resident_count': 2, 'apartment_two_room_count': None})
        self.assertEqual(sparse(updated.overrides), sparse(old.overrides))
        for unset, assigned, types in ((['scenario_inputs.apartment_studio_count']*2, {}, {'duplicate_unset'}),
            (['scenario_inputs.apartment_studio_count'], {'apartment_studio_count': 1}, {'set_unset_conflict'})):
            with self.assertRaises(ValidationError) as failure:
                PersonalBuildingPatchV5.model_validate({'schema_version': 5, 'set': {'scenario_inputs': assigned}, 'unset': unset})
            issues = validation_errors(failure.exception)
            self.assertEqual({e['type'] for e in issues}, types)
            self.assertTrue(all(e['loc'][:2] in (['body', 'unset'], ['body', 'set']) for e in issues))
        for body in ({}, {'schema_version': True}, {'schema_version': 1}, {'schema_version': 5, 'scenario_inputs': {'personal_energy_class': 'A'}}):
            with self.assertRaises(ValidationError): PersonalBuildingPutV5.model_validate(body)

    def test_projection_personal_provenance_source_outage_and_official_immutability(self):
        base = CanonicalBuildingV1.model_validate_json(json.dumps(public_example()))
        before = base.model_dump_json()
        doc = PersonalBuildingPutV5.model_validate({'schema_version': 5, 'scenario_inputs': {
            'apartment_studio_count': 0, 'apartment_two_room_count': None}})
        for source in (base, None):
            result = project_v5(source, doc, building_key=base.building_key, base_unavailable_reason='source_unavailable')
            self.assertEqual(sparse(result.effective.scenario_inputs), sparse(doc.scenario_inputs))
            for field, status in zip(APARTMENT_FIELDS, ('available', 'user_unknown', 'missing', 'missing')):
                provenance = result.field_provenance['scenario_inputs.'+field]
                self.assertEqual(provenance.status, status)
                self.assertIsNone(provenance.source_observed_at)
            if source:
                self.assertEqual(result.effective.certificate, base.certificate)
                self.assertEqual(result.effective.published_certificate, base.published_certificate)
                self.assertEqual(result.effective.model, base.model)
            self.assertEqual(result.input_status.property.completeness, 'incomplete' if source else 'unavailable')
            paths = result.input_status.property.missing_paths if source else result.input_status.property.unavailable_paths
            self.assertIn('details.main_purpose', paths)
        self.assertEqual(base.model_dump_json(), before)
        self.assertEqual(result.effective.certificate, None)

    def test_class_year_charge_strict_sparse_atomic_and_personal(self):
        samples = {
            'personal_energy_class': ({'grade': grade, 'scale': scale} for grade in 'ABCDEFG' for scale in (2013, 2018)),
            'personal_energy_class_date': ({'precision': 'year', 'year': year} for year in (1, 2020)),
            'annual_maintenance_charge_eur_cents': (0, 1, 123456, 99_999_999_999),
        }
        document = PersonalBuildingPutV5.model_validate({'schema_version': 5, 'scenario_inputs': {
            'resident_count': 2, 'apartment_two_room_count': 3}})
        for name, values in samples.items():
            for value in (None, *values):
                with self.subTest(name=name, value=value):
                    body = {'schema_version': 5, 'scenario_inputs': {name: value}}
                    doc = PersonalBuildingPutV5.model_validate_json(json.dumps(body))
                    self.assertEqual(personal_document(doc)['scenario_inputs'], {name: value})
                    updated = patch_v5(document, PersonalBuildingPatchV5.model_validate({
                        'schema_version': 5, 'set': {'scenario_inputs': {name: value}}}), None)
                    self.assertEqual(sparse(updated.scenario_inputs), {**sparse(document.scenario_inputs), name: value})
                    reset = patch_v5(updated, PersonalBuildingPatchV5.model_validate({
                        'schema_version': 5, 'unset': ['scenario_inputs.' + name]}), None)
                    self.assertEqual(personal_document(reset), personal_document(document))
        doc = PersonalBuildingPutV5.model_validate({'schema_version': 5, 'scenario_inputs': {
            'personal_energy_class': {'grade': 'C', 'scale': 2018},
            'personal_energy_class_date': {'precision': 'year', 'year': 2020}}})
        updated = patch_v5(doc, PersonalBuildingPatchV5.model_validate({'schema_version': 5, 'set': {
            'scenario_inputs': {'personal_energy_class': {'grade': 'A', 'scale': 2013}}}}), None)
        self.assertEqual(updated.scenario_inputs.personal_energy_class_date, doc.scenario_inputs.personal_energy_class_date)
        for name in PROPERTY_FIELDS:
            base = CanonicalBuildingV1.model_validate_json(json.dumps(public_example()))
            for value, status in ((None, 'user_unknown'),):
                doc = PersonalBuildingPutV5.model_validate({'schema_version': 5, 'scenario_inputs': {name: value}})
                projected = project_v5(base, doc)
                provenance = projected.field_provenance['scenario_inputs.' + name]
                self.assertEqual((provenance.origin, provenance.source, provenance.status, provenance.is_override),
                                 ('user', 'scenario_inputs.' + name, status, True))
                self.assertIsNone(provenance.source_observed_at)
                self.assertEqual(projected.effective.certificate, base.certificate)
                self.assertEqual(projected.effective.model, base.model)

    def test_nested_errors_date_money_bounds(self):
        invalid = {
            'personal_energy_class': ('C', {}, {'grade': 'C'}, {'scale': 2018}, {'grade': 'a', 'scale': 2018},
                *({'grade': 'A', 'scale': v} for v in (True, '2018', 2018.0, 2020)),
                {'grade': 'A', 'scale': 2018, 'certificate_id': 'SECRET'}),
            'personal_energy_class_date': ('2020-01-01', {}, {'year': 2020}, {'precision': 'day', 'year': 2020},
                *({'precision': 'year', 'year': v} for v in (True, 0, -1, 2020.0, '2020', 10000)),
                {'precision': 'year', 'year': 2020, 'month': 1}),
            'annual_maintenance_charge_eur_cents': (-1, 100_000_000_000, True, False, 1.0, 0.01, '1', '1e2', float('inf'), {}),
        }
        for name, values in invalid.items():
            for value in values:
                for model, body, prefix in (
                    (PersonalBuildingPutV5, {'schema_version': 5, 'scenario_inputs': {name: value}}, ['body', 'scenario_inputs', name]),
                    (PersonalBuildingPatchV5, {'schema_version': 5, 'set': {'scenario_inputs': {name: value}}}, ['body', 'set', 'scenario_inputs', name])):
                    with self.subTest(name=name, value=value), self.assertRaises(ValidationError) as failure:
                        model.model_validate(body)
                    self.assertTrue(all(e['loc'][:len(prefix)] == prefix for e in validation_errors(failure.exception)))
        for leaf in ('personal_energy_class.grade', 'personal_energy_class.scale',
                     'personal_energy_class_date.year', 'personal_energy_class_date.precision'):
            with self.assertRaises(ValidationError) as failure:
                PersonalBuildingPatchV5.model_validate({'schema_version': 5, 'unset': ['scenario_inputs.' + leaf]})
            self.assertEqual(validation_errors(failure.exception)[0]['loc'], ['body', 'unset', 0])
        for name in ('personal_energy_class', 'personal_energy_class_date', 'annual_maintenance_charge_eur_cents'):
            for unset, assigned in ((['scenario_inputs.' + name] * 2, {}), (['scenario_inputs.' + name], {name: None})):
                with self.assertRaises(ValidationError):
                    PersonalBuildingPatchV5.model_validate({'schema_version': 5, 'set': {'scenario_inputs': assigned}, 'unset': unset})

    def test_year_clock_rollover_structural_read_and_unrelated_patch(self):
        body = {'schema_version': 5, 'scenario_inputs': {'personal_energy_class_date': {'precision': 'year', 'year': 2027}}}
        with self.assertRaises(ValidationError) as failure:
            PersonalBuildingPutV5.model_validate(body, context={'current_year': 2026})
        self.assertEqual(validation_errors(failure.exception)[0]['loc'], ['body', 'scenario_inputs', 'personal_energy_class_date', 'year'])
        doc = PersonalBuildingPutV5.model_validate(body, context={'current_year': 2027})
        self.assertEqual(doc.scenario_inputs.personal_energy_class_date.year, 2027)
        body['scenario_inputs']['personal_energy_class_date']['year'] = 9999
        stored = PersonalBuildingPutV5.model_validate(body, context={'stored_document': True})
        updated = patch_v5(stored, PersonalBuildingPatchV5(schema_version=5, display_name='Year retained'), None)
        self.assertEqual(updated.scenario_inputs.personal_energy_class_date.year, 9999)
        self.assertEqual(project_v5(None, updated, building_key='k').scenario_inputs.personal_energy_class_date.year, 9999)
        with self.assertRaises(ValidationError):
            PersonalBuildingPutV5.model_validate(personal_document(updated))

    def test_current_input_completeness_source_effective_null_zero_and_outage(self):
        base = CanonicalBuildingV1.model_validate_json(json.dumps(public_example()))
        required = {'main_purpose': '05', 'construction_year': 2000, 'floor_area': 0, 'heating_method': '01'}
        doc = PersonalBuildingPutV5.model_validate({'schema_version': 5, 'overrides': required})
        with adopted_pin():
            complete = project_v5(None, doc, building_key='k').input_status.property
            self.assertEqual(complete.completeness, 'complete')
            self.assertTrue(complete.saveable)
            self.assertEqual((complete.missing_paths, complete.unavailable_paths), ([], []))
            from src.building_contract import FieldProvenanceV1
            filled = base.model_copy(update={
                'details': base.details.model_copy(update=required),
                'field_provenance': {**base.field_provenance, **{
                    'details.'+field: FieldProvenanceV1(origin='ryhti',status='available') for field in required}}})
            source_prefilled = project_v5(filled, PersonalBuildingPutV5(schema_version=5)).input_status.property
            self.assertEqual(source_prefilled.completeness, 'complete')
            for field in required:
                partial = PersonalBuildingPutV5.model_validate({'schema_version': 5, 'overrides': {**required, field: None}})
                status = project_v5(base, partial).input_status.property
                self.assertEqual(status.completeness, 'incomplete')
                self.assertEqual(status.missing_paths, ['details.' + field])
                self.assertEqual(status.unavailable_paths, [])
            for purpose in ('06', 'historical'):
                unknown = PersonalBuildingPutV5.model_validate({'schema_version': 5, 'overrides': {**required, 'main_purpose': purpose}})
                self.assertEqual(project_v5(base, unknown).input_status.property.unavailable_paths, ['details.main_purpose'])
            absent = project_v5(None, PersonalBuildingPutV5(schema_version=5), building_key='k').input_status.property
            self.assertEqual(absent.unavailable_paths, list(REQUIRED_PROPERTY_PATHS))
        self.assertEqual(project_v5(base, doc).input_status.property.completeness, 'complete')
        with patch('src.purpose_catalog.load_purpose_catalog', side_effect=PurposeCatalogUnavailable):
            status = project_v5(base, doc).input_status.property
            self.assertEqual(status.completeness, 'unavailable')
            self.assertEqual(status.unavailable_paths, ['details.main_purpose'])
        unknown = PersonalBuildingPutV5.model_validate({'schema_version': 5, 'overrides': {'construction_year': None}})
        status = project_v5(None, unknown, building_key='k').input_status.property
        self.assertIn('details.construction_year', status.missing_paths)
        self.assertNotIn('details.construction_year', status.unavailable_paths)

class PurposeCatalogTests(unittest.TestCase):
    def test_bundled_adopted_draft_discovery_and_validation_parity(self):
        raw = json.loads(SNAPSHOT.read_text())
        self.assertEqual(content_version(raw), raw['catalog_version'])
        self.assertEqual([c['code'] for c in raw['choices']], [f'{n:02}' for n in range(1, 8)])
        self.assertEqual(raw['scheme_status'], 'DRAFT')
        self.assertEqual(raw['product_support'], 'adopted')
        self.assertEqual(raw['approval_reference'], '.codex-orch/state/owner-purpose-catalog-adoption-2026-10-30.md')
        self.assertTrue(all(c['selectable'] and c['upstream_status'] == 'DRAFT' and
            c['lifecycle'] == 'unknown' and c['label_fi'] and c['label_sv'] and c['label_en']
            for c in raw['choices']))
        # Bind adoption to the exact owner-approved observation: only product
        # support/reference/selection changed, including no label translation.
        observation = json.loads(json.dumps(raw))
        observation.update(product_support='pending', approval_reference=None)
        for choice in observation['choices']:
            choice['selectable'] = False
        self.assertEqual(content_version(observation),
                         'sha256:d86ecda01fb415206d30bacb3ba3c0aca7ff72cbd477945c45530556ec9e4b0e')
        self.assertEqual(raw['retrieved_at'], '2026-10-29T18:46:29.288202+00:00')
        # The real bundled pin remains usable without any live upstream access.
        with patch('requests.get', side_effect=AssertionError('No live refresh')):
            catalog = load_purpose_catalog()
            with TestClient(app) as client:
                response = client.get('/energy-data/building-purpose-choices')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(response.json(), catalog.model_dump(mode='json'))
            from src.purpose_catalog import purpose_validation_catalog
            self.assertEqual(purpose_validation_catalog().codes['main_purpose'],
                             frozenset(choice.code for choice in catalog.choices))
        schema = json.loads(Path('src/api/purpose-catalog-v1.schema.json').read_text())
        jsonschema.Draft202012Validator({**schema, '$ref': '#/$defs/PurposeCatalogV1'}).validate(response.json())

    def test_snapshot_integrity_outage_labels_lifecycle_and_active_validation_parity(self):
        raw = synthetic_catalog()
        with tempfile.TemporaryDirectory() as folder, patch('src.purpose_catalog.SNAPSHOT', Path(folder)/'snapshot.json') as path:
            with self.assertRaises(PurposeCatalogUnavailable): load_purpose_catalog()
            path.write_text(json.dumps(raw))
            with patch('requests.get', side_effect=AssertionError('No live refresh')):
                catalog=load_purpose_catalog()
            self.assertIsNone(catalog.choices[0].label_en)
            self.assertEqual(catalog.scheme_status, 'DRAFT')
            self.assertTrue(catalog.choices[0].selectable)
            self.assertEqual(catalog.choices[0].lifecycle, 'unknown')
            self.assertEqual(catalog.choices[0].upstream_status, 'DRAFT')
            with TestClient(app) as client:
                response=client.get('/energy-data/building-purpose-choices')
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.headers['cache-control'],'no-store')
                self.assertEqual(response.json()['catalog_version'],raw['catalog_version'])
            from src.purpose_catalog import purpose_validation_catalog
            self.assertEqual(purpose_validation_catalog().codes['main_purpose'], frozenset(f'{n:02}' for n in range(1,6)))
            path.write_text(json.dumps(dict(raw, attribution='changed')))
            with self.assertRaises(PurposeCatalogUnavailable): load_purpose_catalog()
            path.write_text('{}')
            with self.assertRaises(PurposeCatalogUnavailable): load_purpose_catalog()
            # A valid but unadopted pin still fails closed. This fixture is
            # independent of the now-adopted bundled product data.
            pending = synthetic_catalog()
            pending.update(product_support='pending', approval_reference=None)
            for choice in pending['choices']:
                choice['selectable'] = False
            pending['catalog_version'] = content_version(pending)
            path.write_text(json.dumps(pending))
            with self.assertRaises(PurposeCatalogUnavailable): load_purpose_catalog()
            with TestClient(app) as client:
                response = client.get('/energy-data/building-purpose-choices')
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.json()['detail']['code'], 'purpose_catalog_unavailable')
                self.assertEqual(response.headers['cache-control'], 'no-store')
        # Recomputing a content digest does not authorize adoption or selection.
        from src.purpose_catalog import PurposeCatalogV1
        for changes in ({'product_support':'pending'}, {'approval_reference':None}, {'approval_reference':' '}):
            invalid={**synthetic_catalog(),**changes}
            invalid['catalog_version']=content_version(invalid)
            with self.assertRaises(ValidationError):PurposeCatalogV1.model_validate_json(json.dumps(invalid))
        invalid=synthetic_catalog()
        invalid['choices'][-1]['selectable']=True
        invalid['catalog_version']=content_version(invalid)
        with self.assertRaises(ValidationError):PurposeCatalogV1.model_validate_json(json.dumps(invalid))
        with TestClient(app) as client:
            for path, status in (('/energy-data/building-purpose-choices',200), ('/energy-data/building-purpose-choices?x=1',422)):
                response=client.get(path);self.assertEqual(response.status_code,status,response.text)
                self.assertEqual(response.headers['cache-control'],'no-store')


class PersonalVersionHttpTests(OwnerHttpBoundaryTests):
    # Reuse auth setup; inherited tests deliberately remain in their original suite.
    def test_readiness_selector_errors_are_sanitized_before_state_access(self):
        invalid_queries = (
            ('personal_schema_version=REDACTION_SENTINEL', ['query', 'personal_schema_version']),
            ('personal_schema_version=0', ['query', 'personal_schema_version']),
            ('personal_schema_version=6', ['query', 'personal_schema_version']),
            ('personal_schema_version=01', ['query', 'personal_schema_version']),
            ('personal_schema_version=1.0', ['query', 'personal_schema_version']),
            ('personal_schema_version=', ['query', 'personal_schema_version']),
            ('personal_schema_version=5&personal_schema_version=5', ['query']),
            ('personal_schema_version=1&personal_schema_version=5', ['query']),
            ('unknown=REDACTION_SENTINEL', ['query']),
        )
        with patch('src.api.user_buildings.state_ready') as state_ready:
            for query, location in invalid_queries:
                with self.subTest(query=query):
                    response = self.client.get('/readyz/state?' + query)
                    self.check(response, 422)
                    self.assertNotIn('REDACTION_SENTINEL', response.text)
                    issues = response.json()['detail']
                    self.assertTrue(issues)
                    for issue in issues:
                        self.assertEqual(set(issue), {'loc', 'type', 'msg'})
                        self.assertEqual(issue['loc'], location)
            state_ready.assert_not_called()

    def test_readiness_uses_v5_by_default_and_when_selected(self):
        for query, version in (('', 5), ('?personal_schema_version=5', 5)):
            for available, status in ((True, 200), (False, 503)):
                with self.subTest(query=query, available=available), patch(
                        'src.api.user_buildings.state_ready', return_value=available) as state_ready:
                    response = self.client.get('/readyz/state' + query)
                    self.check(response, status)
                    self.assertEqual(response.json(), {
                        'status': 'ready' if available else 'personal_state_unavailable'})
                    state_ready.assert_called_once_with(version)

    def test_version_query_body_closed_errors(self):
        for query in ('personal_schema_version=6','personal_schema_version=2&personal_schema_version=2',
                      'personal_schema_version=01','personal_schema_version=1.0','unknown=2'):
            for method in ('get','delete'):
                self.check(getattr(self.client,method)('/energy-data/user/buildings/key?'+query,headers=self.auth),422)
        for body, query in (({'schema_version':2}, ''), ({}, '?personal_schema_version=2'),
            ({'schema_version':2,'scenario_inputs':{'apartment_studio_count':True}}, '?personal_schema_version=2')):
            response=self.client.put('/energy-data/user/buildings/key'+query,json=body,headers=self.auth)
            self.check(response,422)
            self.assertTrue(all(not any('PersonalBuilding' in str(p) for p in e['loc']) for e in response.json()['detail']))
        for name, value, member in (
            ('personal_energy_class', {'grade':'A','scale':2018.0}, 'scale'),
            ('personal_energy_class_date', {'precision':'year','year':9999}, 'year'),
            ('annual_maintenance_charge_eur_cents', 'SECRET', None),
            ('apartment_studio_count', True, None)):
            for method in ('put','patch'):
                body={'schema_version':5,'scenario_inputs':{name:value}}
                prefix=['body','scenario_inputs',name]
                if method=='patch':
                    body={'schema_version':5,'set':{'scenario_inputs':{name:value}}}
                    prefix=['body','set','scenario_inputs',name]
                result=getattr(self.client,method)('/energy-data/user/buildings/key?personal_schema_version=5',json=body,headers=self.auth)
                self.check(result,422)
                self.assertEqual(result.json()['detail'][0]['loc'],prefix+([member] if member else []))


def load_tests(loader, tests, pattern):
    suite=unittest.TestSuite()
    for cls in (PersonalPropertyTests, PurposeCatalogTests): suite.addTests(loader.loadTestsFromTestCase(cls))
    suite.addTest(PersonalVersionHttpTests('test_readiness_selector_errors_are_sanitized_before_state_access'))
    suite.addTest(PersonalVersionHttpTests('test_readiness_uses_v5_by_default_and_when_selected'))
    suite.addTest(PersonalVersionHttpTests('test_version_query_body_closed_errors'))
    return suite
