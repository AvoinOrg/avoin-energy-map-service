"""Closed v5 measurement transport, provenance, compatibility and HTTP errors."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from src.building_contract import personal_document, sparse, PersonalBuildingPutV1
from src.personal_property_contract import PersonalBuildingPutV2
from src.personal_hvac_contract import PersonalBuildingPutV3
from src.personal_renovations_contract import PersonalBuildingPutV4
from src.personal_utilities_contract import (PersonalBuildingPutV5, PersonalBuildingPatchV5,
    UTILITIES_PATH, UTILITIES_PATHS, project_v5, patch_v5, replace_v5)
from src.user_buildings import require_read_version, require_document_version, OwnerFailure
from src.hvac_catalog import HvacCatalogUnavailable
from tests.test_personal_hvac import full_systems
from tests.test_personal_renovations import all_renovations
from tests import test_user_buildings as http_fixture


def utilities(amount=12345.5):
    return {'electricity': {'grid_import_kwh': amount, 'calendar_year': 2025},
        'water': {'supplied_volume_m3': 456.25, 'calendar_year': 2024}}


def document(value=..., **siblings):
    if value is not ...: siblings['utilities'] = value
    return PersonalBuildingPutV5.model_validate({'schema_version': 5, 'scenario_inputs': siblings})


def change(existing, **body):
    return patch_v5(existing, PersonalBuildingPatchV5.model_validate({'schema_version': 5, **body}), None)


class UtilitiesTests(unittest.TestCase):
    def test_quantities_sparse_numeric_rules_and_closed_objects(self):
        for group, leaf in (('electricity', 'grid_import_kwh'), ('water', 'supplied_volume_m3')):
            for value in (0, 1, .25, 1e-10, 1e100, 2**53, None):
                doc = document({group: {leaf: value}})
                reopened = PersonalBuildingPutV5.model_validate_json(json.dumps(personal_document(doc)))
                self.assertEqual(sparse(reopened.scenario_inputs), {'utilities': {group: {leaf: value}}})
                provenance = project_v5(None, reopened, building_key='synthetic').field_provenance[UTILITIES_PATH + '.' + group + '.' + leaf]
                self.assertEqual(provenance.status, 'user_unknown' if value is None else 'available')
                self.assertTrue(provenance.is_override)
            for value in (-1, -.1, True, False, '1', float('nan'), float('inf'), -float('inf'), 10**400, 2**53 + 1):
                with self.subTest(group=group, value=str(value)), self.assertRaises(ValidationError):
                    document({group: {leaf: value}})
            for extra in ('unit', 'scope', 'meter', 'period', 'billing', 'provenance', 'hot_water_m3'):
                with self.assertRaises(ValidationError): document({group: {extra: 1}})
            for value in ({}, None): document({group: value})
        for value in ({}, None): document(value)
        with self.assertRaises(ValidationError): document({'gas': {}})

    def test_optional_independent_years_completed_boundary_and_stored_clock(self):
        for group in ('electricity', 'water'):
            for year in (None, 1, 2025):
                value = {'schema_version': 5, 'scenario_inputs': {'utilities': {group: {'calendar_year': year}}}}
                PersonalBuildingPutV5.model_validate(value, context={'current_year': 2026})
            for year in (0, -1, 10000, 2026, 2027, True, False, 2025.0, '2025'):
                with self.subTest(group=group, year=year), self.assertRaises(ValidationError):
                    PersonalBuildingPutV5.model_validate({'schema_version': 5, 'scenario_inputs': {
                        'utilities': {group: {'calendar_year': year}}}}, context={'current_year': 2026})
        document(utilities())  # deliberately independent years
        stored = PersonalBuildingPutV5.model_validate({'schema_version': 5, 'scenario_inputs': {
            'utilities': {'water': {'calendar_year': 9999}}}}, context={'stored_document': True})
        self.assertEqual(change(stored, display_name='Unrelated').scenario_inputs.utilities.water.calendar_year, 9999)
        project_v5(None, stored, building_key='synthetic')

    def test_provenance_null_ancestors_no_prefill_or_completion(self):
        baseline = project_v5(None, document(), building_key='synthetic')
        for u, source in ((None, UTILITIES_PATH), ({'water': None}, UTILITIES_PATH + '.water'),
                          ({'water': {'calendar_year': None}}, UTILITIES_PATH + '.water.calendar_year')):
            projection = project_v5(None, document(u), building_key='synthetic')
            path = UTILITIES_PATH + '.water.calendar_year'
            p = projection.field_provenance[path]
            self.assertEqual((p.status, p.source), ('user_unknown', source))
            self.assertEqual(p.dependencies, [source] if source != path else [])
            self.assertEqual(projection.input_status, baseline.input_status)
            self.assertEqual(projection.systems_defaults, baseline.systems_defaults)
        for path in UTILITIES_PATHS:
            p = baseline.field_provenance[path]
            self.assertEqual((p.status, p.reason, p.origin), ('missing', 'no_personal_default', 'unavailable'))
        self.assertNotIn('utilities', sparse(baseline.scenario_inputs))

    def test_atomic_replacement_reset_promotion_and_frozen_old_models(self):
        doc = document(utilities(), resident_count=2, systems=full_systems(), renovations=all_renovations(0))
        original = personal_document(doc)
        replaced = change(doc, set={'scenario_inputs': {'utilities': {'electricity': {'grid_import_kwh': 0}, 'water': None}}})
        self.assertEqual(sparse(replaced.scenario_inputs.utilities), {'electricity': {'grid_import_kwh': 0}, 'water': None})
        for key in ('resident_count', 'systems', 'renovations'):
            self.assertEqual(personal_document(replaced)['scenario_inputs'][key], original['scenario_inputs'][key])
        sibling = change(replaced, set={'scenario_inputs': {'renovations': all_renovations(None)}})
        self.assertEqual(sparse(sibling.scenario_inputs.utilities), sparse(replaced.scenario_inputs.utilities))
        cleared = change(replaced, unset=[UTILITIES_PATH])
        self.assertNotIn('utilities', sparse(cleared.scenario_inputs))
        readded = change(cleared, set={'scenario_inputs': {'utilities': {'water': {}}}})
        self.assertEqual(sparse(readded.scenario_inputs.utilities), {'water': {}})
        with patch('src.personal_hvac_contract.load_hvac_catalog', side_effect=HvacCatalogUnavailable):
            change(doc, unset=[UTILITIES_PATH]); project_v5(None, doc, building_key='synthetic')
            with self.assertRaises(HvacCatalogUnavailable): replace_v5(doc, None)
        self.assertEqual(personal_document(replace_v5(document(), None))['scenario_inputs'], {})
        for version, model in enumerate((PersonalBuildingPutV1, PersonalBuildingPutV2, PersonalBuildingPutV3, PersonalBuildingPutV4), 1):
            with self.assertRaises(ValidationError): model.model_validate({'schema_version': version, 'scenario_inputs': {'utilities': {}}})
            old = model.model_validate({'schema_version': version, 'scenario_inputs': {'resident_count': None}})
            self.assertEqual(personal_document(change(old)), personal_document(old) | {'schema_version': 5})
            view = project_v5(None, old, building_key='synthetic')
            self.assertEqual(view.document_schema_version, version)
            self.assertNotIn('utilities', sparse(view.scenario_inputs))
        for body in ({'unset': [UTILITIES_PATH] * 2}, {'unset': [UTILITIES_PATH + '.water']},
                     {'unset': [UTILITIES_PATH], 'set': {'scenario_inputs': {'utilities': None}}}):
            with self.assertRaises(ValidationError): PersonalBuildingPatchV5.model_validate({'schema_version': 5, **body})
        for value in (None, 4, 5.0, '5', True):
            with self.assertRaises(ValidationError): PersonalBuildingPutV5.model_validate({'schema_version': value})
        for stored in range(1, 6):
            for view in range(1, 6):
                row = SimpleNamespace(schema_version=stored)
                if view != 1 and view < stored:
                    with self.assertRaises(OwnerFailure): require_read_version(row, view)
                else: require_read_version(row, view)
                if view < stored:
                    with self.assertRaises(OwnerFailure): require_document_version(row, view)
                else: require_document_version(row, view)


class UtilitiesHttpTests(unittest.TestCase):
    setUp = http_fixture.OwnerHttpBoundaryTests.setUp
    check = http_fixture.OwnerHttpBoundaryTests.check

    def test_selected_private_errors_query_and_payload(self):
        path = '/energy-data/user/buildings/synthetic?personal_schema_version=5'
        for method in ('put', 'patch'):
            for group, leaf, value in (('electricity', 'grid_import_kwh', -1), ('water', 'supplied_volume_m3', 'secret-canary'),
                                       ('electricity', 'calendar_year', 9999), ('water', 'meter', 'secret-canary')):
                scenario = {'utilities': {group: {leaf: value}}}
                body = {'schema_version': 5, **({'set': {'scenario_inputs': scenario}} if method == 'patch' else {'scenario_inputs': scenario})}
                response = getattr(self.client, method)(path, json=body, headers=self.auth); self.check(response, 422)
                errors = response.json()['detail']
                self.assertEqual([e['loc'] for e in errors], [['body'] + (['set'] if method == 'patch' else []) + ['scenario_inputs', 'utilities', group, leaf]])
                self.assertTrue(all(set(e) == {'loc', 'type', 'msg'} for e in errors))
                self.assertNotIn('secret-canary', response.text)
        for query in ('personal_schema_version=5.0', 'personal_schema_version=05', 'personal_schema_version=5&personal_schema_version=5'):
            self.check(self.client.get('/readyz/state?' + query), 422)
        self.check(self.client.put(path, json={'schema_version': 4}, headers=self.auth), 422)
        self.check(self.client.put(path, content=' ' * 32769, headers=self.auth), 413)


class UtilitiesArtifactTests(unittest.TestCase):
    def test_maintained_schema_examples_isolated_export_and_frozen_baselines(self):
        import os
        import jsonschema
        from src.api.personal_utilities_examples import utilities_examples
        root = Path(__file__).resolve().parents[1]
        schema = json.loads((root/'src/api/personal-utilities-v5.schema.json').read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        for name, (model, value) in utilities_examples().items():
            model.model_validate_json(json.dumps(value))
            jsonschema.Draft202012Validator({'$ref': '#/$defs/' + model.__name__, '$defs': schema['$defs']}).validate(value)
        validator = jsonschema.Draft202012Validator({'$ref': '#/$defs/PersonalBuildingPutV5', '$defs': schema['$defs']})
        for u in ({'water': {'supplied_volume_m3': -1}}, {'electricity': {'calendar_year': 0}}, {'water': {'unit': 'l'}}):
            with self.assertRaises(jsonschema.ValidationError): validator.validate({'schema_version': 5, 'scenario_inputs': {'utilities': u}})
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)/'src/api'; output.mkdir(parents=True)
            result = subprocess.run([sys.executable, '-m', 'src.api.export_building_contract', '--utilities-only'],
                cwd=folder, env={**os.environ, 'PYTHONPATH': str(root) + ':' + str(root/'src')}, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, 'isolated utilities export failed')
            self.assertEqual(sorted(p.name for p in output.iterdir()), ['personal-utilities-v5.examples.json', 'personal-utilities-v5.schema.json'])
            for p in output.iterdir(): self.assertEqual(p.read_bytes(), (root/'src/api'/p.name).read_bytes())
        for pattern in ('src/api/*v[1234].schema.json', 'src/api/*v[1234].examples.json',
                        'src/state_storage/expected_catalog*.json', 'db_migrations/versions/*.py', 'state_migrations/versions/*.py'):
            for p in root.glob(pattern):
                if p.name in ('expected_catalog_v5.json', 'b825df0a431c_015_personal_utilities_v5.py', 'es0005_utilities_v5.py'): continue
                relative = p.relative_to(root).as_posix()
                original = subprocess.run(['git', 'show', 'HEAD:' + relative], cwd=root, capture_output=True)
                self.assertEqual(original.returncode, 0, relative)
                self.assertEqual(p.read_bytes(), original.stdout, relative)
