"""Renovations in the v5 contract and HTTP boundary proof; all identities/data synthetic."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from src.building_contract import (ContractValidationError,
    personal_document, sparse)
from src.personal_renovations_contract import CATEGORIES, RENOVATIONS_PATH, RENOVATIONS_PATHS
from src.personal_utilities_contract import PersonalBuildingPutV5, PersonalBuildingPatchV5, project_v5, patch_v5, replace_v5
from src.hvac_catalog import HvacCatalogUnavailable
from tests.test_personal_hvac import full_systems
from tests import test_user_buildings as http_fixture


def document(renovations=..., **siblings):
    scenario = dict(siblings)
    if renovations is not ...:
        scenario['renovations'] = renovations
    return PersonalBuildingPutV5.model_validate({'schema_version': 5, 'scenario_inputs': scenario})


def patch_document(existing, **body):
    return patch_v5(existing, PersonalBuildingPatchV5.model_validate({'schema_version': 5, **body}), None)


def all_renovations(thickness=230.5):
    return {name: {'completed': True} for name in CATEGORIES} | {
        'top_floor_insulation': {'completed': True, 'execution_year': 2025,
            **({} if thickness is ... else {'added_insulation_thickness_mm': thickness})}}


class RenovationsTests(unittest.TestCase):
    def test_every_flag_strict_sparse_and_closed(self):
        for name in CATEGORIES:
            for value in (True, False, None):
                doc = document({name: {'completed': value}})
                self.assertEqual(personal_document(doc)['scenario_inputs']['renovations'], {name: {'completed': value}})
            for value in (0, 1, 'false', [], {}):
                with self.subTest(name=name, value=value), self.assertRaises(ValidationError):
                    document({name: {'completed': value}})
            for value in ({}, None):
                self.assertEqual(sparse(document({name: value}).scenario_inputs.renovations), {name: value})
            if name != 'top_floor_insulation':
                for detail, value in (('execution_year', 2025), ('added_insulation_thickness_mm', 230)):
                    with self.assertRaises(ValidationError): document({name: {'completed': True, detail: value}})
        for value in (None, {}, all_renovations()):
            original = document(value)
            reopened = PersonalBuildingPutV5.model_validate_json(json.dumps(personal_document(original)), context={'stored_document': True})
            self.assertEqual(personal_document(original), personal_document(reopened))
        for extra in ('thickness_mm', 'notes', 'savings'):
            with self.assertRaises(ValidationError): document({'top_floor_insulation': {'completed': True, extra: 230}})

    def test_thickness_transport_sparse_roundtrip_and_provenance(self):
        path = RENOVATIONS_PATH + '.top_floor_insulation.added_insulation_thickness_mm'
        for value in (0, 230, 230.5, 1e-10, 1e100, 2**53, None, ...):
            with self.subTest(value=value):
                doc = document(all_renovations(value))
                reopened = PersonalBuildingPutV5.model_validate_json(json.dumps(personal_document(doc)))
                self.assertEqual(personal_document(doc), personal_document(reopened))
                detail = sparse(reopened.scenario_inputs.renovations.top_floor_insulation)
                if value is ...:
                    self.assertNotIn('added_insulation_thickness_mm', detail)
                else:
                    self.assertEqual(detail['added_insulation_thickness_mm'], value)
                projected = project_v5(None, reopened, building_key='synthetic')
                provenance = projected.field_provenance[path]
                expected = 'missing' if value is ... else 'user_unknown' if value is None else 'available'
                self.assertEqual(provenance.status, expected)
                self.assertEqual(projected.source_availability[path].status, expected)
                self.assertEqual(provenance.origin, 'unavailable' if value is ... else 'user')
                self.assertEqual(provenance.is_override, value is not ...)
                self.assertEqual(provenance.source, None if value is ... else path)
        for value in (-1, -0.1, True, False, '230', float('nan'), float('inf'), -float('inf'), 10**400, 2**53 + 1):
            with self.subTest(value=str(value)), self.assertRaises(ValidationError):
                document(all_renovations(value))
        for ancestor in (None, {'top_floor_insulation': None}):
            provenance = project_v5(None, document(ancestor), building_key='synthetic').field_provenance[path]
            source = RENOVATIONS_PATH if ancestor is None else RENOVATIONS_PATH + '.top_floor_insulation'
            self.assertEqual((provenance.status, provenance.source, provenance.dependencies), ('user_unknown', source, [source]))

    def test_optional_year_and_conditional_detail_rules(self):
        boundary = {'schema_version': 5, 'scenario_inputs': {'renovations': {
            'top_floor_insulation': {'completed': True, 'execution_year': 2025}}}}
        PersonalBuildingPutV5.model_validate(boundary, context={'current_year': 2025})
        with self.assertRaises(ValidationError):
            PersonalBuildingPutV5.model_validate(boundary, context={'current_year': 2024})
        year = datetime.now(timezone.utc).year
        for value in (None, 1, 2025, year):
            document({'top_floor_insulation': {'completed': True, 'execution_year': value}})
        document({'top_floor_insulation': {'completed': True}})
        for value in (0, -1, 99999, year + 1, True, False, 2025.5, '2025'):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                document({'top_floor_insulation': {'completed': True, 'execution_year': value}})
        for completed in ({}, {'completed': False}, {'completed': None}):
            for details in ({'execution_year': None}, {'execution_year': 2025},
                            {'added_insulation_thickness_mm': None}, {'added_insulation_thickness_mm': 0},
                            {'execution_year': None, 'added_insulation_thickness_mm': None}):
                with self.assertRaises(ValidationError) as error:
                    document({'top_floor_insulation': completed | details})
                self.assertEqual({e['loc'][-1] for e in error.exception.errors()}, set(details))
        # Stored reads and unrelated edits are not time-dependent revalidation.
        stored = PersonalBuildingPutV5.model_validate({'schema_version': 5, 'scenario_inputs': {
            'renovations': {'top_floor_insulation': {'completed': True, 'execution_year': year + 1}}}},
            context={'stored_document': True})
        self.assertEqual(patch_document(stored, display_name='Renamed').scenario_inputs.renovations.top_floor_insulation.execution_year, year + 1)
        project_v5(None, stored, building_key='synthetic')

    def test_reset_and_replace_preserve_sparse_siblings(self):
        original = document(all_renovations(), systems=full_systems(), resident_count=2, apartment_two_room_count=None)
        unchanged = personal_document(original)
        updated = patch_document(original, set={'overrides': {'floor_area': 0, 'total_area': None}})
        self.assertEqual(sparse(updated.scenario_inputs), unchanged['scenario_inputs'])
        reset = patch_document(updated, unset=[RENOVATIONS_PATH])
        self.assertNotIn('renovations', sparse(reset.scenario_inputs))
        self.assertEqual(reset.schema_version, 5)
        for name in ('systems', 'resident_count', 'apartment_two_room_count'):
            self.assertEqual(sparse(reset.scenario_inputs)[name], unchanged['scenario_inputs'][name])
        unknown = patch_document(original, set={'scenario_inputs': {'renovations': None}})
        self.assertIsNone(unknown.scenario_inputs.renovations)
        no_work = patch_document(original, set={'scenario_inputs': {'renovations': {'top_floor_insulation': {'completed': False}}}})
        self.assertEqual(sparse(no_work.scenario_inputs.renovations), {'top_floor_insulation': {'completed': False}})
        reselected = patch_document(no_work, set={'scenario_inputs': {'renovations': {'top_floor_insulation': {'completed': True}}}})
        self.assertEqual(sparse(reselected.scenario_inputs.renovations.top_floor_insulation), {'completed': True})
        replacement = replace_v5(document(), None)
        self.assertEqual(personal_document(replacement)['scenario_inputs'], {})

    def test_provenance_unknown_ancestors_and_no_completion_effects(self):
        for renovations, expected in ((..., 'missing'), (None, 'user_unknown'), ({}, 'missing')):
            projection = project_v5(None, document(renovations), building_key='synthetic')
            for path in RENOVATIONS_PATHS - {RENOVATIONS_PATH}:
                self.assertEqual(projection.field_provenance[path].status, expected)
        projection = project_v5(None, document({'facade': {'completed': False}, 'windows': None,
            'roof': {'completed': None}, 'top_floor_insulation': {'completed': True, 'execution_year': None}}), building_key='synthetic')
        p = projection.field_provenance
        self.assertEqual(p[RENOVATIONS_PATH + '.facade.completed'].status, 'available')
        self.assertEqual(p[RENOVATIONS_PATH + '.windows.completed'].dependencies, [RENOVATIONS_PATH + '.windows'])
        self.assertEqual(p[RENOVATIONS_PATH + '.roof.completed'].status, 'user_unknown')
        self.assertEqual(p[RENOVATIONS_PATH + '.top_floor_insulation.execution_year'].status, 'user_unknown')
        empty = project_v5(None, document(), building_key='synthetic')
        self.assertEqual(projection.input_status, empty.input_status)
        self.assertEqual(set(type(projection.input_status).model_fields), {'property', 'systems'})

    def test_versions_reset_conflicts_and_hvac_validation_survive(self):
        for body in ({'unset': [RENOVATIONS_PATH + '.roof']}, {'unset': [RENOVATIONS_PATH] * 2},
                     {'set': {'scenario_inputs': {'renovations': {}}}, 'unset': [RENOVATIONS_PATH]}):
            with self.assertRaises(ValidationError): PersonalBuildingPatchV5.model_validate({'schema_version': 5, **body})
        for version in (None, 4, 5.0, '5', True):
            with self.assertRaises(ValidationError): PersonalBuildingPutV5.model_validate({} if version is None else {'schema_version': version})
        original = document(all_renovations(), systems=full_systems())
        changed = copy.deepcopy(full_systems()); changed['heating']['sources'][0]['source_code'] = 'ryhti:heating_energy_source:07'
        with self.assertRaises(ContractValidationError): patch_document(original, set={'scenario_inputs': {'systems': changed}})
        with patch('src.personal_hvac_contract.load_hvac_catalog', side_effect=HvacCatalogUnavailable):
            patch_document(original, set={'scenario_inputs': {'renovations': None}})
            project_v5(None, original, building_key='synthetic')
            with self.assertRaises(HvacCatalogUnavailable): replace_v5(original, None)
            changed['heating']['sources'][0]['source_id'] = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
            changed['heating']['primary_source_id'] = changed['heating']['sources'][0]['source_id']
            with self.assertRaises(HvacCatalogUnavailable): patch_document(original, set={'scenario_inputs': {'systems': changed}})


class RenovationsHttpTests(unittest.TestCase):
    setUp = http_fixture.OwnerHttpBoundaryTests.setUp
    check = http_fixture.OwnerHttpBoundaryTests.check

    def test_renovations_errors_payload_bound_and_legacy_rejection(self):
        path = '/energy-data/user/buildings/synthetic?personal_schema_version=5'
        for method in ('put', 'patch'):
            for completed in ({}, {'completed': False}, {'completed': None}):
                for details in ({'execution_year': None}, {'added_insulation_thickness_mm': None},
                                {'added_insulation_thickness_mm': 0},
                                {'execution_year': 2025, 'added_insulation_thickness_mm': 230.5}):
                    payload = {'schema_version': 5, 'scenario_inputs': {'renovations': {
                        'top_floor_insulation': completed | details}}}
                    if method == 'patch': payload = {'schema_version': 5, 'set': {'scenario_inputs': payload['scenario_inputs']}}
                    response = getattr(self.client, method)(path, json=payload, headers=self.auth)
                    self.check(response, 422)
                    errors = response.json()['detail']
                    prefix = ['body'] + (['set'] if method == 'patch' else []) + ['scenario_inputs', 'renovations', 'top_floor_insulation']
                    self.assertEqual({tuple(e['loc']) for e in errors}, {tuple(prefix + [field]) for field in details})
                    self.assertTrue(all(set(e) == {'loc', 'type', 'msg'} for e in errors))
        for query in ('personal_schema_version=5.0', 'personal_schema_version=04', 'personal_schema_version=5&personal_schema_version=5'):
            self.check(self.client.get('/readyz/state?' + query), 422)
        self.check(self.client.put(path, json={'schema_version': 3}, headers=self.auth), 422)
        self.check(self.client.put(path, content=' ' * 32769, headers=self.auth), 413)


class RenovationsFixtureGuardTests(unittest.TestCase):
    def test_fixture_rejects_ambient_or_wrong_identity_before_connection(self):
        from src.state_storage.config import Target
        from src.state_storage.private import StorageError
        from tests import personal_state_fixture as fixture
        baseline = dict(mode='disposable', host='127.0.0.1', port=55433,
            source_database='energymap_f0031_source_test', admin_user='energymap_f0031_admin_test',
            source_reader='energymap_f0031_reader_test')
        for changed in ({'mode': 'sandbox'}, {'port': 6903}, {'source_database': 'sandbox_energymap'},
                        {'source_reader': 'other'}, {'source_entry': {'mode': 'hba'}}):
            with patch.object(fixture, 'load_target', return_value=Target(baseline | changed)), \
                 patch.object(fixture, 'connected') as connection, self.assertRaises(StorageError):
                fixture.fixture_template()
            connection.assert_not_called()
