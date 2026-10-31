"""Personal v2 boundaries and purpose-only reference validation."""
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
from src.building_contract import CanonicalBuildingV1, PersonalBuildingPutV1, personal_document, sparse, validation_errors
from src.personal_property_contract import (
    APARTMENT_FIELDS, PROPERTY_FIELDS, REQUIRED_PROPERTY_PATHS, PersonalBuildingPutV2, PersonalBuildingPatchV2, ScenarioInputsV2,
    patch_v2, project_v2, v1_document,
)
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
                    doc = PersonalBuildingPutV2(schema_version=2, scenario_inputs=ScenarioInputsV2(**{name: value}))
                    raw = personal_document(doc)
                    self.assertEqual(raw['scenario_inputs'], {name: value})
                    self.assertEqual(personal_document(PersonalBuildingPutV2.model_validate_json(json.dumps(raw))), raw)
            for value in (True, False, -1, 1_000_001, 1.0, 1.5, '0', float('nan'), float('inf'), {}):
                with self.subTest(name=name, value=type(value)), self.assertRaises(ValidationError) as failure:
                    PersonalBuildingPutV2.model_validate({'schema_version': 2, 'scenario_inputs': {name: value}})
                self.assertEqual(validation_errors(failure.exception)[0]['loc'], ['body', 'scenario_inputs', name])
        doc = PersonalBuildingPutV2.model_validate({'schema_version': 2,
            'overrides': {'apartment_count': 0}, 'scenario_inputs': dict.fromkeys(APARTMENT_FIELDS, 1_000_000)})
        self.assertEqual(doc.overrides.apartment_count, 0)
        self.assertEqual(personal_document(PersonalBuildingPutV2(schema_version=2))['scenario_inputs'], {})

    def test_promotion_sparse_reset_and_precise_conflicts(self):
        old = PersonalBuildingPutV1.model_validate({'display_name': 'Home', 'overrides': {'total_area': None},
                                                   'scenario_inputs': {'resident_count': 2}})
        promoted = patch_v2(old, PersonalBuildingPatchV2(schema_version=2), None)
        self.assertEqual(personal_document(promoted), dict(personal_document(old), schema_version=2))
        updated = patch_v2(promoted, PersonalBuildingPatchV2.model_validate({'schema_version': 2,
            'set': {'scenario_inputs': {'apartment_studio_count': 0, 'apartment_two_room_count': None}}}), None)
        reset = patch_v2(updated, PersonalBuildingPatchV2.model_validate({'schema_version': 2,
            'unset': ['scenario_inputs.apartment_studio_count']}), None)
        self.assertEqual(sparse(reset.scenario_inputs), {'resident_count': 2, 'apartment_two_room_count': None})
        self.assertEqual(v1_document(updated), old)
        for unset, assigned, types in ((['scenario_inputs.apartment_studio_count']*2, {}, {'duplicate_unset'}),
            (['scenario_inputs.apartment_studio_count'], {'apartment_studio_count': 1}, {'set_unset_conflict'})):
            with self.assertRaises(ValidationError) as failure:
                PersonalBuildingPatchV2.model_validate({'schema_version': 2, 'set': {'scenario_inputs': assigned}, 'unset': unset})
            issues = validation_errors(failure.exception)
            self.assertEqual({e['type'] for e in issues}, types)
            self.assertTrue(all(e['loc'][:2] in (['body', 'unset'], ['body', 'set']) for e in issues))
        for body in ({}, {'schema_version': True}, {'schema_version': 1}, {'schema_version': 2, 'scenario_inputs': {'personal_energy_class': 'A'}}):
            with self.assertRaises(ValidationError): PersonalBuildingPutV2.model_validate(body)

    def test_projection_personal_provenance_source_outage_and_official_immutability(self):
        base = CanonicalBuildingV1.model_validate_json(json.dumps(public_example()))
        before = base.model_dump_json()
        doc = PersonalBuildingPutV2.model_validate({'schema_version': 2, 'scenario_inputs': {
            'apartment_studio_count': 0, 'apartment_two_room_count': None}})
        for source in (base, None):
            result = project_v2(source, doc, building_key=base.building_key, base_unavailable_reason='source_unavailable')
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
        document = PersonalBuildingPutV2.model_validate({'schema_version': 2, 'scenario_inputs': {
            'resident_count': 2, 'apartment_two_room_count': 3}})
        for name, values in samples.items():
            for value in (None, *values):
                with self.subTest(name=name, value=value):
                    body = {'schema_version': 2, 'scenario_inputs': {name: value}}
                    doc = PersonalBuildingPutV2.model_validate_json(json.dumps(body))
                    self.assertEqual(personal_document(doc)['scenario_inputs'], {name: value})
                    updated = patch_v2(document, PersonalBuildingPatchV2.model_validate({
                        'schema_version': 2, 'set': {'scenario_inputs': {name: value}}}), None)
                    self.assertEqual(sparse(updated.scenario_inputs), {**sparse(document.scenario_inputs), name: value})
                    reset = patch_v2(updated, PersonalBuildingPatchV2.model_validate({
                        'schema_version': 2, 'unset': ['scenario_inputs.' + name]}), None)
                    self.assertEqual(personal_document(reset), personal_document(document))
        doc = PersonalBuildingPutV2.model_validate({'schema_version': 2, 'scenario_inputs': {
            'personal_energy_class': {'grade': 'C', 'scale': 2018},
            'personal_energy_class_date': {'precision': 'year', 'year': 2020}}})
        updated = patch_v2(doc, PersonalBuildingPatchV2.model_validate({'schema_version': 2, 'set': {
            'scenario_inputs': {'personal_energy_class': {'grade': 'A', 'scale': 2013}}}}), None)
        self.assertEqual(updated.scenario_inputs.personal_energy_class_date, doc.scenario_inputs.personal_energy_class_date)
        for name in PROPERTY_FIELDS:
            base = CanonicalBuildingV1.model_validate_json(json.dumps(public_example()))
            for value, status in ((None, 'user_unknown'),):
                doc = PersonalBuildingPutV2.model_validate({'schema_version': 2, 'scenario_inputs': {name: value}})
                projected = project_v2(base, doc)
                provenance = projected.field_provenance['scenario_inputs.' + name]
                self.assertEqual((provenance.origin, provenance.source, provenance.status, provenance.is_override),
                                 ('user', 'scenario_inputs.' + name, status, True))
                self.assertIsNone(provenance.source_observed_at)
                self.assertEqual(projected.effective.certificate, base.certificate)
                self.assertEqual(projected.effective.model, base.model)

    def test_nested_errors_date_money_bounds_and_v1_rejection(self):
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
                    (PersonalBuildingPutV2, {'schema_version': 2, 'scenario_inputs': {name: value}}, ['body', 'scenario_inputs', name]),
                    (PersonalBuildingPatchV2, {'schema_version': 2, 'set': {'scenario_inputs': {name: value}}}, ['body', 'set', 'scenario_inputs', name])):
                    with self.subTest(name=name, value=value), self.assertRaises(ValidationError) as failure:
                        model.model_validate(body)
                    self.assertTrue(all(e['loc'][:len(prefix)] == prefix for e in validation_errors(failure.exception)))
        for leaf in ('personal_energy_class.grade', 'personal_energy_class.scale',
                     'personal_energy_class_date.year', 'personal_energy_class_date.precision'):
            with self.assertRaises(ValidationError) as failure:
                PersonalBuildingPatchV2.model_validate({'schema_version': 2, 'unset': ['scenario_inputs.' + leaf]})
            self.assertEqual(validation_errors(failure.exception)[0]['loc'], ['body', 'unset', 0])
        for name in PROPERTY_FIELDS:
            with self.assertRaises(ValidationError):
                PersonalBuildingPutV1.model_validate({'scenario_inputs': {name: None}})
        for name in ('personal_energy_class', 'personal_energy_class_date', 'annual_maintenance_charge_eur_cents'):
            for unset, assigned in ((['scenario_inputs.' + name] * 2, {}), (['scenario_inputs.' + name], {name: None})):
                with self.assertRaises(ValidationError):
                    PersonalBuildingPatchV2.model_validate({'schema_version': 2, 'set': {'scenario_inputs': assigned}, 'unset': unset})

    def test_year_clock_rollover_structural_read_and_unrelated_patch(self):
        body = {'schema_version': 2, 'scenario_inputs': {'personal_energy_class_date': {'precision': 'year', 'year': 2027}}}
        with self.assertRaises(ValidationError) as failure:
            PersonalBuildingPutV2.model_validate(body, context={'current_year': 2026})
        self.assertEqual(validation_errors(failure.exception)[0]['loc'], ['body', 'scenario_inputs', 'personal_energy_class_date', 'year'])
        doc = PersonalBuildingPutV2.model_validate(body, context={'current_year': 2027})
        self.assertEqual(doc.scenario_inputs.personal_energy_class_date.year, 2027)
        body['scenario_inputs']['personal_energy_class_date']['year'] = 9999
        stored = PersonalBuildingPutV2.model_validate(body, context={'stored_document': True})
        updated = patch_v2(stored, PersonalBuildingPatchV2(schema_version=2, display_name='Year retained'), None)
        self.assertEqual(updated.scenario_inputs.personal_energy_class_date.year, 9999)
        self.assertEqual(project_v2(None, updated, building_key='k').scenario_inputs.personal_energy_class_date.year, 9999)
        with self.assertRaises(ValidationError):
            PersonalBuildingPutV2.model_validate(personal_document(updated))

    def test_current_input_completeness_source_effective_null_zero_and_outage(self):
        base = CanonicalBuildingV1.model_validate_json(json.dumps(public_example()))
        required = {'main_purpose': '05', 'construction_year': 2000, 'floor_area': 0, 'heating_method': '01'}
        doc = PersonalBuildingPutV2.model_validate({'schema_version': 2, 'overrides': required})
        with adopted_pin():
            complete = project_v2(None, doc, building_key='k').input_status.property
            self.assertEqual(complete.completeness, 'complete')
            self.assertTrue(complete.saveable)
            self.assertEqual((complete.missing_paths, complete.unavailable_paths), ([], []))
            from src.building_contract import FieldProvenanceV1
            filled = base.model_copy(update={
                'details': base.details.model_copy(update=required),
                'field_provenance': {**base.field_provenance, **{
                    'details.'+field: FieldProvenanceV1(origin='ryhti',status='available') for field in required}}})
            source_prefilled = project_v2(filled, PersonalBuildingPutV2(schema_version=2)).input_status.property
            self.assertEqual(source_prefilled.completeness, 'complete')
            for field in required:
                partial = PersonalBuildingPutV2.model_validate({'schema_version': 2, 'overrides': {**required, field: None}})
                status = project_v2(base, partial).input_status.property
                self.assertEqual(status.completeness, 'incomplete')
                self.assertEqual(status.missing_paths, ['details.' + field])
                self.assertEqual(status.unavailable_paths, [])
            for purpose in ('06', 'historical'):
                unknown = PersonalBuildingPutV2.model_validate({'schema_version': 2, 'overrides': {**required, 'main_purpose': purpose}})
                self.assertEqual(project_v2(base, unknown).input_status.property.unavailable_paths, ['details.main_purpose'])
            absent = project_v2(None, PersonalBuildingPutV2(schema_version=2), building_key='k').input_status.property
            self.assertEqual(absent.unavailable_paths, list(REQUIRED_PROPERTY_PATHS))
        self.assertEqual(project_v2(base, doc).input_status.property.completeness, 'complete')
        with patch('src.purpose_catalog.load_purpose_catalog', side_effect=PurposeCatalogUnavailable):
            status = project_v2(base, doc).input_status.property
            self.assertEqual(status.completeness, 'unavailable')
            self.assertEqual(status.unavailable_paths, ['details.main_purpose'])
        unknown = PersonalBuildingPutV2.model_validate({'schema_version': 2, 'overrides': {'construction_year': None}})
        status = project_v2(None, unknown, building_key='k').input_status.property
        self.assertIn('details.construction_year', status.missing_paths)
        self.assertNotIn('details.construction_year', status.unavailable_paths)

    def test_stored_mixed_formats_and_safe_shared_projection(self):
        example = saved_example()
        row = SimpleNamespace(**{k: example[k] for k in ('display_name', 'revision', 'share_enabled')},
            saved_id=uuid4(), building_key=example['building_key'], schema_version=2,
            overrides={'main_purpose': 'retired-code'}, scenario_inputs={'resident_count': 1, 'apartment_studio_count': 0},
            last_known_summary=example['last_known_summary'], created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
        for version in (1, 2):
            owner = user_buildings.detail(row, None, 'source_unavailable', version)
            shared = shared_projection(row, None, 'source_unavailable', version)
            self.assertEqual(owner.schema_version, version)
            data = shared.model_dump(mode='json')
            self.assertNotIn('saved_id', data)
            self.assertNotIn('owner_', json.dumps(data))
            self.assertEqual(data['overrides']['main_purpose'], 'retired-code')
            self.assertEqual('apartment_studio_count' in data['scenario_inputs'], version == 2)
            self.assertEqual('input_status' in data, version == 2)
        row.schema_version=1
        row.scenario_inputs={'resident_count':1}
        self.assertEqual(shared_projection(row, None, 'missing', 2).document_schema_version, 1)
        row.schema_version=4
        from src.db.state import StateUnavailable
        with self.assertRaises(StateUnavailable): user_buildings.stored_document(row)

    def test_versioned_exports_are_deterministic_closed_and_validate_examples(self):
        from src.api.personal_property_examples import property_examples
        from src.api.export_building_contract import contract_examples
        for examples, filename in ((contract_examples(), 'building-api-v1.examples.json'),
                                   (property_examples(), 'personal-property-v2.examples.json')):
            expected=json.loads((Path('src/api')/filename).read_text())
            self.assertEqual({k:v for k,(_,v) in examples.items()}, expected)
            for model, value in examples.values():
                model.model_validate_json(json.dumps(value))
                mode = 'validation' if model in (PersonalBuildingPutV1, PersonalBuildingPutV2, PersonalBuildingPatchV2) else 'serialization'
                jsonschema.Draft202012Validator(model.model_json_schema(mode=mode)).validate(value)
        # Provenance key order must be stable across independent hash seeds.
        code = ("import hashlib,json;from src.api.personal_property_examples import property_examples;"
                "print(hashlib.sha256((json.dumps({k:v for k,(_,v) in property_examples().items()},indent=2)+'\\n').encode()).hexdigest())")
        expected_hash=hashlib.sha256(Path('src/api/personal-property-v2.examples.json').read_bytes()).hexdigest()
        for seed in ('1','2'):
            result=subprocess.run([sys.executable,'-c',code],env=dict(os.environ,PYTHONHASHSEED=seed),
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=15)
            self.assertEqual(result.returncode,0,'independent example generation failed')
            self.assertEqual(result.stdout.strip(),expected_hash)
        for model in (PersonalBuildingPutV2, PersonalBuildingPatchV2, ScenarioInputsV2):
            schema=model.model_json_schema()
            self.assertFalse(schema['additionalProperties'])
            for definition in schema.get('$defs',{}).values():
                if definition.get('type')=='object':self.assertFalse(definition['additionalProperties'])


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
            ('personal_schema_version=4', ['query', 'personal_schema_version']),
            ('personal_schema_version=01', ['query', 'personal_schema_version']),
            ('personal_schema_version=1.0', ['query', 'personal_schema_version']),
            ('personal_schema_version=', ['query', 'personal_schema_version']),
            ('personal_schema_version=2&personal_schema_version=2', ['query']),
            ('personal_schema_version=1&personal_schema_version=2', ['query']),
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

    def test_readiness_default_v1_v2_and_unavailable_state(self):
        for query, version in (('', 1), ('?personal_schema_version=1', 1), ('?personal_schema_version=2', 2), ('?personal_schema_version=3', 3)):
            for available, status in ((True, 200), (False, 503)):
                with self.subTest(query=query, available=available), patch(
                        'src.api.user_buildings.state_ready', return_value=available) as state_ready:
                    response = self.client.get('/readyz/state' + query)
                    self.check(response, status)
                    self.assertEqual(response.json(), {
                        'status': 'ready' if available else 'personal_state_unavailable'})
                    state_ready.assert_called_once_with(version)

    def test_version_query_body_closed_errors(self):
        for query in ('personal_schema_version=4','personal_schema_version=2&personal_schema_version=2',
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
                body={'schema_version':2,'scenario_inputs':{name:value}}
                prefix=['body','scenario_inputs',name]
                if method=='patch':
                    body={'schema_version':2,'set':{'scenario_inputs':{name:value}}}
                    prefix=['body','set','scenario_inputs',name]
                result=getattr(self.client,method)('/energy-data/user/buildings/key?personal_schema_version=2',json=body,headers=self.auth)
                self.check(result,422)
                self.assertEqual(result.json()['detail'][0]['loc'],prefix+([member] if member else []))


def load_tests(loader, tests, pattern):
    suite=unittest.TestSuite()
    for cls in (PersonalPropertyTests, PurposeCatalogTests): suite.addTests(loader.loadTestsFromTestCase(cls))
    suite.addTest(PersonalVersionHttpTests('test_readiness_selector_errors_are_sanitized_before_state_access'))
    suite.addTest(PersonalVersionHttpTests('test_readiness_default_v1_v2_and_unavailable_state'))
    suite.addTest(PersonalVersionHttpTests('test_version_query_body_closed_errors'))
    return suite
