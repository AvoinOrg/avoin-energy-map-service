"""Useful v4 contract and HTTP boundary proof; all identities/data synthetic."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from src.building_contract import (ContractValidationError, PersonalBuildingPutV1,
    personal_document, sparse)
from src.personal_property_contract import PersonalBuildingPutV2
from src.personal_hvac_contract import PersonalBuildingPutV3
from src.personal_renovations_contract import (CATEGORIES, RENOVATIONS_PATH, RENOVATIONS_PATHS,
    PersonalBuildingPutV4, PersonalBuildingPatchV4, project_v4, patch_v4, replace_v4)
from src.hvac_catalog import HvacCatalogUnavailable
from src.user_buildings import require_read_version, require_document_version, OwnerFailure
from tests.test_personal_hvac import full_systems
from tests import test_user_buildings as http_fixture


def document(renovations=..., **siblings):
    scenario = dict(siblings)
    if renovations is not ...:
        scenario['renovations'] = renovations
    return PersonalBuildingPutV4.model_validate({'schema_version': 4, 'scenario_inputs': scenario})


def patch_document(existing, **body):
    return patch_v4(existing, PersonalBuildingPatchV4.model_validate({'schema_version': 4, **body}), None)


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
            reopened = PersonalBuildingPutV4.model_validate_json(json.dumps(personal_document(original)), context={'stored_document': True})
            self.assertEqual(personal_document(original), personal_document(reopened))
        for extra in ('thickness_mm', 'notes', 'savings'):
            with self.assertRaises(ValidationError): document({'top_floor_insulation': {'completed': True, extra: 230}})

    def test_thickness_transport_sparse_roundtrip_and_provenance(self):
        path = RENOVATIONS_PATH + '.top_floor_insulation.added_insulation_thickness_mm'
        for value in (0, 230, 230.5, 1e-10, 1e100, 2**53, None, ...):
            with self.subTest(value=value):
                doc = document(all_renovations(value))
                reopened = PersonalBuildingPutV4.model_validate_json(json.dumps(personal_document(doc)))
                self.assertEqual(personal_document(doc), personal_document(reopened))
                detail = sparse(reopened.scenario_inputs.renovations.top_floor_insulation)
                if value is ...:
                    self.assertNotIn('added_insulation_thickness_mm', detail)
                else:
                    self.assertEqual(detail['added_insulation_thickness_mm'], value)
                projected = project_v4(None, reopened, building_key='synthetic')
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
            provenance = project_v4(None, document(ancestor), building_key='synthetic').field_provenance[path]
            source = RENOVATIONS_PATH if ancestor is None else RENOVATIONS_PATH + '.top_floor_insulation'
            self.assertEqual((provenance.status, provenance.source, provenance.dependencies), ('user_unknown', source, [source]))

    def test_optional_year_and_conditional_detail_rules(self):
        boundary = {'schema_version': 4, 'scenario_inputs': {'renovations': {
            'top_floor_insulation': {'completed': True, 'execution_year': 2025}}}}
        PersonalBuildingPutV4.model_validate(boundary, context={'current_year': 2025})
        with self.assertRaises(ValidationError):
            PersonalBuildingPutV4.model_validate(boundary, context={'current_year': 2024})
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
        stored = PersonalBuildingPutV4.model_validate({'schema_version': 4, 'scenario_inputs': {
            'renovations': {'top_floor_insulation': {'completed': True, 'execution_year': year + 1}}}},
            context={'stored_document': True})
        self.assertEqual(patch_document(stored, display_name='Renamed').scenario_inputs.renovations.top_floor_insulation.execution_year, year + 1)
        project_v4(None, stored, building_key='synthetic')

    def test_reset_replace_sparse_siblings_and_promotion(self):
        original = document(all_renovations(), systems=full_systems(), resident_count=2, apartment_two_room_count=None)
        unchanged = personal_document(original)
        updated = patch_document(original, set={'overrides': {'floor_area': 0, 'total_area': None}})
        self.assertEqual(sparse(updated.scenario_inputs), unchanged['scenario_inputs'])
        reset = patch_document(updated, unset=[RENOVATIONS_PATH])
        self.assertNotIn('renovations', sparse(reset.scenario_inputs))
        self.assertEqual(reset.schema_version, 4)
        for name in ('systems', 'resident_count', 'apartment_two_room_count'):
            self.assertEqual(sparse(reset.scenario_inputs)[name], unchanged['scenario_inputs'][name])
        unknown = patch_document(original, set={'scenario_inputs': {'renovations': None}})
        self.assertIsNone(unknown.scenario_inputs.renovations)
        no_work = patch_document(original, set={'scenario_inputs': {'renovations': {'top_floor_insulation': {'completed': False}}}})
        self.assertEqual(sparse(no_work.scenario_inputs.renovations), {'top_floor_insulation': {'completed': False}})
        reselected = patch_document(no_work, set={'scenario_inputs': {'renovations': {'top_floor_insulation': {'completed': True}}}})
        self.assertEqual(sparse(reselected.scenario_inputs.renovations.top_floor_insulation), {'completed': True})
        for cls, version in ((PersonalBuildingPutV1, 1), (PersonalBuildingPutV2, 2), (PersonalBuildingPutV3, 3)):
            with self.assertRaises(ValidationError): cls.model_validate({'schema_version': version, 'scenario_inputs': {'renovations': {}}})
            old = cls.model_validate({'schema_version': version, 'overrides': {'floor_area': 0, 'total_area': None}, 'scenario_inputs': {'resident_count': None}})
            promoted = patch_document(old)
            self.assertEqual(personal_document(promoted), personal_document(old) | {'schema_version': 4})
            projected = project_v4(None, old, building_key='synthetic')
            self.assertEqual(projected.document_schema_version, version)
            self.assertNotIn('renovations', sparse(projected.scenario_inputs))
        replacement = replace_v4(document(), None)
        self.assertEqual(personal_document(replacement)['scenario_inputs'], {})

    def test_provenance_unknown_ancestors_and_no_completion_effects(self):
        for renovations, expected in ((..., 'missing'), (None, 'user_unknown'), ({}, 'missing')):
            projection = project_v4(None, document(renovations), building_key='synthetic')
            for path in RENOVATIONS_PATHS - {RENOVATIONS_PATH}:
                self.assertEqual(projection.field_provenance[path].status, expected)
        projection = project_v4(None, document({'facade': {'completed': False}, 'windows': None,
            'roof': {'completed': None}, 'top_floor_insulation': {'completed': True, 'execution_year': None}}), building_key='synthetic')
        p = projection.field_provenance
        self.assertEqual(p[RENOVATIONS_PATH + '.facade.completed'].status, 'available')
        self.assertEqual(p[RENOVATIONS_PATH + '.windows.completed'].dependencies, [RENOVATIONS_PATH + '.windows'])
        self.assertEqual(p[RENOVATIONS_PATH + '.roof.completed'].status, 'user_unknown')
        self.assertEqual(p[RENOVATIONS_PATH + '.top_floor_insulation.execution_year'].status, 'user_unknown')
        empty = project_v4(None, document(), building_key='synthetic')
        self.assertEqual(projection.input_status, empty.input_status)
        self.assertEqual(set(type(projection.input_status).model_fields), {'property', 'systems'})

    def test_versions_reset_conflicts_and_hvac_validation_survive(self):
        for stored in (1, 2, 3, 4):
            row = SimpleNamespace(schema_version=stored)
            for view in (1, 2, 3, 4):
                if view in (2, 3) and stored > view:
                    with self.assertRaises(OwnerFailure): require_read_version(row, view)
                else: require_read_version(row, view)
                if stored > view:
                    with self.assertRaises(OwnerFailure): require_document_version(row, view)
                else: require_document_version(row, view)
        for body in ({'unset': [RENOVATIONS_PATH + '.roof']}, {'unset': [RENOVATIONS_PATH] * 2},
                     {'set': {'scenario_inputs': {'renovations': {}}}, 'unset': [RENOVATIONS_PATH]}):
            with self.assertRaises(ValidationError): PersonalBuildingPatchV4.model_validate({'schema_version': 4, **body})
        for version in (None, 3, 4.0, '4', True):
            with self.assertRaises(ValidationError): PersonalBuildingPutV4.model_validate({} if version is None else {'schema_version': version})
        original = document(all_renovations(), systems=full_systems())
        changed = copy.deepcopy(full_systems()); changed['heating']['sources'][0]['source_code'] = 'ryhti:heating_energy_source:07'
        with self.assertRaises(ContractValidationError): patch_document(original, set={'scenario_inputs': {'systems': changed}})
        with patch('src.personal_hvac_contract.load_hvac_catalog', side_effect=HvacCatalogUnavailable):
            patch_document(original, set={'scenario_inputs': {'renovations': None}})
            project_v4(None, original, building_key='synthetic')
            with self.assertRaises(HvacCatalogUnavailable): replace_v4(original, None)
            changed['heating']['sources'][0]['source_id'] = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
            changed['heating']['primary_source_id'] = changed['heating']['sources'][0]['source_id']
            with self.assertRaises(HvacCatalogUnavailable): patch_document(original, set={'scenario_inputs': {'systems': changed}})


class RenovationsHttpTests(unittest.TestCase):
    setUp = http_fixture.OwnerHttpBoundaryTests.setUp
    check = http_fixture.OwnerHttpBoundaryTests.check

    def test_v4_selected_errors_payload_bound_and_legacy_rejection(self):
        path = '/energy-data/user/buildings/synthetic?personal_schema_version=4'
        for method in ('put', 'patch'):
            for completed in ({}, {'completed': False}, {'completed': None}):
                for details in ({'execution_year': None}, {'added_insulation_thickness_mm': None},
                                {'added_insulation_thickness_mm': 0},
                                {'execution_year': 2025, 'added_insulation_thickness_mm': 230.5}):
                    payload = {'schema_version': 4, 'scenario_inputs': {'renovations': {
                        'top_floor_insulation': completed | details}}}
                    if method == 'patch': payload = {'schema_version': 4, 'set': {'scenario_inputs': payload['scenario_inputs']}}
                    response = getattr(self.client, method)(path, json=payload, headers=self.auth)
                    self.check(response, 422)
                    errors = response.json()['detail']
                    prefix = ['body'] + (['set'] if method == 'patch' else []) + ['scenario_inputs', 'renovations', 'top_floor_insulation']
                    self.assertEqual({tuple(e['loc']) for e in errors}, {tuple(prefix + [field]) for field in details})
                    self.assertTrue(all(set(e) == {'loc', 'type', 'msg'} for e in errors))
        for query in ('personal_schema_version=4.0', 'personal_schema_version=04', 'personal_schema_version=4&personal_schema_version=4'):
            self.check(self.client.get('/readyz/state?' + query), 422)
        self.check(self.client.put(path, json={'schema_version': 3}, headers=self.auth), 422)
        self.check(self.client.put(path, content=' ' * 32769, headers=self.auth), 413)


class RenovationsArtifactTests(unittest.TestCase):
    def test_examples_schema_independent_export_and_frozen_legacy_artifacts(self):
        import os
        import subprocess
        import sys
        import tempfile
        import jsonschema
        from src.api.personal_renovations_examples import renovations_examples
        root = Path(__file__).resolve().parents[1]
        schema = json.loads((root/'src/api/personal-renovations-v4.schema.json').read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        validator = jsonschema.Draft202012Validator({'$ref': '#/$defs/PersonalBuildingPutV4', '$defs': schema['$defs']})
        # Both validation and read definitions must express either-detail applicability.
        for definition in (name for name in schema['$defs'] if name.startswith('TopFloorInsulationV4')):
            detail_validator = jsonschema.Draft202012Validator({'$ref': '#/$defs/' + definition, '$defs': schema['$defs']})
            for completed in ({}, {'completed': False}, {'completed': None}):
                for details in ({'execution_year': None}, {'added_insulation_thickness_mm': None},
                                {'execution_year': 2025, 'added_insulation_thickness_mm': 0}):
                    with self.assertRaises(jsonschema.ValidationError): detail_validator.validate(completed | details)
        for value in (-1, True, '230'):
            with self.assertRaises(jsonschema.ValidationError):
                validator.validate({'schema_version': 4, 'scenario_inputs': {'renovations': {
                    'top_floor_insulation': {'completed': True, 'added_insulation_thickness_mm': value}}}})
        for name, (model, value) in renovations_examples().items():
            model.model_validate_json(json.dumps(value))
            jsonschema.Draft202012Validator({'$ref': '#/$defs/' + model.__name__, '$defs': schema['$defs']}).validate(value)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)/'src/api'; output.mkdir(parents=True)
            result = subprocess.run([sys.executable, '-m', 'src.api.export_building_contract', '--renovations-only'],
                cwd=folder, env={**os.environ, 'PYTHONPATH': str(root) + ':' + str(root/'src')}, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, 'independent candidate export failed')
            self.assertEqual(sorted(p.name for p in output.iterdir()), ['personal-renovations-v4.examples.json', 'personal-renovations-v4.schema.json'])
            for p in output.iterdir(): self.assertEqual(p.read_bytes(), (root/'src/api'/p.name).read_bytes())
        # Schema/example contracts and exact historical catalogs cannot widen.
        for glob in ('src/api/*v[123].schema.json', 'src/api/*v[123].examples.json', 'src/state_storage/expected_catalog*.json'):
            for p in root.glob(glob):
                if p.name == 'expected_catalog_v4.json': continue
                relative = p.relative_to(root).as_posix()
                original = subprocess.run(['git', 'show', 'HEAD:' + relative], cwd=root, capture_output=True)
                self.assertEqual(original.returncode, 0, relative)
                self.assertEqual(p.read_bytes(), original.stdout, relative)


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
