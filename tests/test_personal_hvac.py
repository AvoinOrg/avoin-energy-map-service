"""Structured personal HVAC contract; HTTP provider fixtures are synthetic."""
import json
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from src.building_contract import (ContractValidationError, PersonalBuildingPutV1,
    personal_document, sparse, validation_errors)
from src.personal_property_contract import PersonalBuildingPutV2
from src.personal_hvac_contract import (PersonalBuildingPutV3, PersonalBuildingPatchV3,
    EQUIPMENT_FIELDS, QUANTITY_FIELDS, SystemsV3, project_v3, patch_v3, replace_v3, validate_systems)
from src.hvac_catalog import LEGACY_DEFAULTS_VERSION, DEFAULTS_VERSION, HvacCatalogUnavailable, load_hvac_catalog
from src.geoserver.ventilation import VENTILATION_TYPES
from src.user_buildings import OwnerFailure, require_document_version, require_read_version
from tests import test_user_buildings as http_fixture


def systems(ventilation=...):
    result = {'defaults_version': DEFAULTS_VERSION}
    if ventilation is not ...:
        result['ventilation'] = ventilation
    return result


def document(value=...):
    scenario = {} if value is ... else {'systems': value}
    return PersonalBuildingPutV3.model_validate({'schema_version': 3, 'scenario_inputs': scenario})


IDS = ('11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222',
       '33333333-3333-4333-8333-333333333333')


def full_systems():
    """Synthetic answers using real bundled choices; deliberately not a 100% sum."""
    return systems({'kind': 'multiple', 'codes': ['0', '8']}) | {
        'heating': {'sources': [
            {'source_id': IDS[0], 'source_code': 'ryhti:heating_energy_source:01', 'share_percent': 0},
            {'source_id': IDS[1], 'source_code': 'ryhti:heating_energy_source:1101', 'share_percent': 121.125},
            {'source_id': IDS[2], 'source_code': 'ryhti:heating_energy_source:04', 'share_percent': None}],
            'primary_source_id': IDS[0]},
        'heated_quantities': dict(total_area_m2=0, total_volume_m3=None, residential_area_m2=123.45, residential_volume_m3=-1.25),
        'cooling': {'kind': 'selected', 'codes': ['energy:cooling:district', 'energy:cooling:heat_pump']},
        'equipment': dict(geothermal_installed=False, solar_panels_installed=True, energy_storage_ready=None)}


class HvacPureTests(unittest.TestCase):
    def test_closed_old_contracts_and_proposed_controls_do_not_activate(self):
        for model, version in ((PersonalBuildingPutV1, 1), (PersonalBuildingPutV2, 2)):
            with self.assertRaises(ValidationError):
                model.model_validate({'schema_version': version, 'scenario_inputs': {'systems': systems()}})
        for key in ('heated_total_area_m2', 'heated_total_volume_m3', 'coefficients', 'attribution', 'reset_coefficients'):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                document(systems() | {key: None})
        for version in (True, 3.0, '3', 2):
            with self.assertRaises(ValidationError): PersonalBuildingPutV3.model_validate({'schema_version': version})

    def test_distinct_omitted_unknown_none_gravity_and_sparse_reopen(self):
        for value in (..., None, systems(), systems(None), systems({'kind': 'none'}), systems({'kind': 'selected', 'code': '0'})):
            doc = document(value)
            self.assertEqual(personal_document(PersonalBuildingPutV3.model_validate_json(json.dumps(personal_document(doc)))), personal_document(doc))
            projection = project_v3(None, doc, building_key='synthetic')
            self.assertEqual(sparse(projection.effective.scenario_inputs), sparse(doc.scenario_inputs))
            self.assertEqual(projection.input_status.systems.completeness, 'unavailable')
        unknown=project_v3(None,document(None),building_key='synthetic')
        self.assertEqual(unknown.field_provenance['scenario_inputs.systems'].status,'user_unknown')
        self.assertEqual(unknown.field_provenance['scenario_inputs.systems.ventilation'].source,'scenario_inputs.systems')
        self.assertEqual(unknown.field_provenance['scenario_inputs.systems.ventilation'].dependencies,['scenario_inputs.systems'])

    def test_invalid_selections_and_exact_pin(self):
        for selection in ({'kind':'selected','code':0}, {'kind':'selected','code':''},
                {'kind':'selected','codes':['0']}, {'kind':'none','code':'0'}, {}, True,
                {'kind':'selected','code':'bad secret'}):
            with self.subTest(selection=selection), self.assertRaises(ValidationError): document(systems(selection))
        for value in ({}, {'defaults_version':'new'}, {'defaults_version':None}):
            with self.assertRaises(ValidationError): document(value)

    def test_actual_pin_choices_labels_missing_en_and_applicability(self):
        pin = load_hvac_catalog()
        self.assertEqual(len(pin.ventilation_choices),9)
        self.assertEqual([(c.code,c.label_fi,c.label_sv) for c in pin.ventilation_choices],
            [(code,row[1],row[2]) for code,row in VENTILATION_TYPES.items()])
        self.assertTrue(all(c.label_en is None and c.selectable for c in pin.ventilation_choices))
        self.assertEqual(pin.schema_version,2)
        self.assertEqual(pin.unavailable_capabilities,['coefficients'])
        self.assertEqual(pin.coefficients.status,'deferred')
        self.assertFalse(pin.coefficients.required_for_completion)
        self.assertIn('class 1',pin.ventilation_choices[3].applicability_note)
        for c in pin.ventilation_choices: replace_v3(document(systems({'kind':'selected','code':c.code})),None)
        with self.assertRaises(ContractValidationError) as rejected:
            replace_v3(document(systems({'kind':'selected','code':'unknown'})),None)
        self.assertEqual(rejected.exception.errors[0]['loc'],['scenario_inputs','systems','ventilation','code'])

    def test_pin_corruption_and_missing_fail_closed(self):
        with patch('src.hvac_catalog.CATALOG_V2_SNAPSHOT',Path('/does-not-exist')):
            with self.assertRaises(HvacCatalogUnavailable): load_hvac_catalog()
        with patch.object(Path,'read_bytes',return_value=b'{"catalog_version":"forged"}'):
            with self.assertRaises(HvacCatalogUnavailable): load_hvac_catalog()

    def test_atomic_promotion_preserves_property_legacy_unknown_zero_and_reset(self):
        old=PersonalBuildingPutV2.model_validate({'schema_version':2,'display_name':'Home',
            'overrides':{'floor_area':0,'ventilation_type_id':'0','ventilation_description':'Existing note'},
            'scenario_inputs':{'apartment_studio_count':0,'apartment_two_room_count':None,'resident_count':2}})
        changed=patch_v3(old,PersonalBuildingPatchV3.model_validate({'schema_version':3,
            'set':{'scenario_inputs':{'systems':systems({'kind':'selected','code':'8'})}}}),None)
        self.assertEqual(sparse(changed.overrides),sparse(old.overrides))
        self.assertEqual(changed.scenario_inputs.apartment_studio_count,0)
        self.assertIsNone(changed.scenario_inputs.apartment_two_room_count)
        reset=patch_v3(changed,PersonalBuildingPatchV3.model_validate({'schema_version':3,
            'set':{'scenario_inputs':{'systems':systems()}}}),None)
        self.assertNotIn('ventilation',sparse(reset.scenario_inputs.systems))
        unset=patch_v3(reset,PersonalBuildingPatchV3.model_validate({'schema_version':3,'unset':['scenario_inputs.systems']}),None)
        self.assertEqual(sparse(unset.scenario_inputs),sparse(old.scenario_inputs))
        self.assertEqual(unset.schema_version,3)
        replaced=replace_v3(document(),None)
        self.assertEqual(personal_document(replaced)['scenario_inputs'],{})

    def test_unset_conflicts_descendants_and_duplicates(self):
        for body in ({'unset':['scenario_inputs.systems.ventilation']},
                {'unset':['scenario_inputs.systems','scenario_inputs.systems']},
                {'set':{'scenario_inputs':{'systems':None}},'unset':['scenario_inputs.systems']}):
            with self.assertRaises(ValidationError): PersonalBuildingPatchV3.model_validate({'schema_version':3,**body})

    def test_outage_unrelated_reset_historical_reads_but_new_or_put_requires_pin(self):
        historic=document(systems({'kind':'selected','code':'retired'}))
        with patch('src.personal_hvac_contract.load_hvac_catalog',side_effect=HvacCatalogUnavailable):
            projection=project_v3(None,historic,building_key='synthetic')
            self.assertEqual(projection.systems_defaults.status,'pin_unavailable')
            for body in ({'display_name':'Changed'}, {'set':{'scenario_inputs':{'systems':systems({'kind':'selected','code':'retired'})}}},
                    {'set':{'scenario_inputs':{'systems':systems(None)}}}, {'set':{'scenario_inputs':{'systems':None}}},
                    {'unset':['scenario_inputs.systems']}):
                patch_v3(historic,PersonalBuildingPatchV3.model_validate({'schema_version':3,**body}),None)
            for action in (lambda:replace_v3(historic,None),lambda:patch_v3(historic,
                    PersonalBuildingPatchV3.model_validate({'schema_version':3,'set':{'scenario_inputs':{'systems':systems({'kind':'selected','code':'0'})}}}),None)):
                with self.assertRaises(HvacCatalogUnavailable): action()
        projection=project_v3(None,historic,building_key='synthetic')
        self.assertEqual(projection.field_provenance['scenario_inputs.systems.ventilation'].status,'unsupported')

    def test_deliberate_read_and_write_versions(self):
        for stored in (1,2,3):
            for view in (1,2,3):
                row=SimpleNamespace(schema_version=stored)
                if view==2 and stored==3:
                    with self.assertRaises(OwnerFailure): require_read_version(row,view)
                else: require_read_version(row,view)
                if view<stored:
                    with self.assertRaises(OwnerFailure): require_document_version(row,view)
                else: require_document_version(row,view)

    def test_each_actual_choice_validates_and_legacy_pin_still_reads(self):
        catalog = load_hvac_catalog()
        self.assertEqual(len(catalog.heating_choices), 16)
        self.assertEqual(len(catalog.cooling_choices), 14)
        for choice in catalog.heating_choices:
            replace_v3(document(systems() | {'heating': {'sources': [{'source_id': IDS[0], 'source_code': choice.code}]}}), None)
        for choice in catalog.cooling_choices:
            replace_v3(document(systems() | {'cooling': {'kind': 'selected', 'codes': [choice.code]}}), None)
        from src.hvac_catalog import load_legacy_hvac_catalog
        self.assertEqual(load_legacy_hvac_catalog().defaults.defaults_version, LEGACY_DEFAULTS_VERSION)
        old = document({'defaults_version': LEGACY_DEFAULTS_VERSION, 'ventilation': {'kind': 'selected', 'code': '0'}})
        self.assertEqual(personal_document(old)['scenario_inputs']['systems']['defaults_version'], LEGACY_DEFAULTS_VERSION)
        self.assertEqual(project_v3(None, old, building_key='synthetic').systems_defaults.defaults_version, LEGACY_DEFAULTS_VERSION)
        for c in catalog.equipment_choices:
            self.assertIn(c.code, EQUIPMENT_FIELDS)

    def test_permissive_number_transport_for_each_leaf(self):
        for value in (None, 0, -123.125, 101.75, 1e100, 2**100):
            for field in QUANTITY_FIELDS:
                candidate = document(systems() | {'heated_quantities': {field: value}})
                self.assertEqual(sparse(candidate.scenario_inputs.systems.heated_quantities), {field: value})
            candidate = full_systems(); candidate['heating']['sources'][0]['share_percent'] = value
            self.assertEqual(document(candidate).scenario_inputs.systems.heating.sources[0].share_percent, value)
        for value in (True, False, '0', float('nan'), float('inf'), -float('inf'), 10**400, 2**53 + 1):
            for field in QUANTITY_FIELDS:
                with self.subTest(field=field, value_type=type(value).__name__), self.assertRaises(ValidationError):
                    document(systems() | {'heated_quantities': {field: value}})
            candidate = full_systems(); candidate['heating']['sources'][0]['share_percent'] = value
            with self.assertRaises(ValidationError): document(candidate)

    def test_equipment_sparse_strict_and_independent(self):
        for field in EQUIPMENT_FIELDS:
            for value in (None, True, False):
                actual = document(systems() | {'equipment': {field: value}})
                self.assertEqual(sparse(actual.scenario_inputs.systems.equipment), {field: value})
            for value in (0, 1, 'false', [], {}):
                with self.assertRaises(ValidationError): document(systems() | {'equipment': {field: value}})
        for group in ('equipment', 'heated_quantities'):
            with self.assertRaises(ValidationError): document(systems() | {group: {'capacity': 10}})
        candidate = document(full_systems())
        self.assertIs(candidate.scenario_inputs.systems.equipment.geothermal_installed, False)
        self.assertIn('1101', candidate.scenario_inputs.systems.heating.sources[1].source_code)

    def test_collection_identity_order_primary_removal_readd_and_code_replacement(self):
        initial = document(full_systems())
        updated = full_systems(); updated['heating']['sources'].reverse(); updated['heating']['primary_source_id'] = IDS[1]
        def apply(value, previous):
            return patch_v3(previous, PersonalBuildingPatchV3.model_validate({'schema_version': 3, 'set': {'scenario_inputs': {'systems': value}}}), None)
        reordered = apply(updated, initial)
        self.assertEqual({s.source_id: s.share_percent for s in reordered.scenario_inputs.systems.heating.sources},
                         {s.source_id: s.share_percent for s in initial.scenario_inputs.systems.heating.sources})
        projection = project_v3(None, reordered, building_key='synthetic')
        self.assertEqual([p.source_id for p in projection.systems_source_provenance], list(reversed(IDS)))
        self.assertEqual([p.share_percent.status for p in projection.systems_source_provenance], ['user_unknown', 'available', 'available'])
        replaced = copy.deepcopy(updated); replaced['heating']['sources'][0]['source_code'] = 'ryhti:heating_energy_source:07'
        with self.assertRaises(ContractValidationError) as error: apply(replaced, reordered)
        self.assertEqual(error.exception.errors[0]['loc'], ['set', 'scenario_inputs', 'systems', 'heating', 'sources', 0, 'source_code'])
        removed = copy.deepcopy(updated); removed['heating']['sources'] = removed['heating']['sources'][1:]
        after = apply(removed, reordered)
        readd = copy.deepcopy(removed); readd['heating']['sources'].append({'source_id': '44444444-4444-4444-8444-444444444444', 'source_code': 'ryhti:heating_energy_source:04'})
        result = apply(readd, after)
        self.assertNotIn('share_percent', sparse(result.scenario_inputs.systems.heating.sources[-1]))
        replaced = copy.deepcopy(readd); replaced['heating']['sources'][-1] = {'source_id': '55555555-5555-4555-8555-555555555555', 'source_code': 'ryhti:heating_energy_source:07'}
        self.assertIsNone(apply(replaced, result).scenario_inputs.systems.heating.sources[-1].share_percent)

    def test_collection_structural_errors_and_none_unknown(self):
        for member, value, expected in (('source_id', IDS[0], 'duplicate_source_id'),
                                       ('source_code', 'ryhti:heating_energy_source:01', 'duplicate_source_code')):
            candidate = full_systems(); candidate['heating']['sources'][1][member] = value
            with self.assertRaises(ValidationError) as error: document(candidate)
            errors = validation_errors(error.exception)
            self.assertEqual(errors[0]['type'], expected)
            self.assertEqual(errors[0]['loc'][-3:], ['sources', 1, member])
        candidate = full_systems(); candidate['heating']['primary_source_id'] = '44444444-4444-4444-8444-444444444444'
        with self.assertRaises(ValidationError): document(candidate)
        for variant in ({'kind': 'selected', 'codes': []}, {'kind': 'none', 'codes': ['energy:cooling:district']},
                        {'kind': 'selected', 'codes': ['energy:cooling:district'] * 2}):
            with self.assertRaises(ValidationError): document(systems() | {'cooling': variant})
        for variant in ({'kind': 'multiple', 'codes': ['0']}, {'kind': 'multiple', 'codes': ['0', '0']}):
            with self.assertRaises(ValidationError): document(systems(variant))
        for answer in (None, {'kind': 'none'}, {'kind': 'selected', 'codes': ['energy:cooling:district']}):
            value = document(systems() | {'cooling': answer})
            self.assertEqual(sparse(value.scenario_inputs.systems)['cooling'], answer)
        for heating in (None, {'sources': []}, {'sources': [], 'primary_source_id': None},
                        {'sources': [{'source_id': IDS[0], 'source_code': 'ryhti:heating_energy_source:01'}]}):
            self.assertEqual(sparse(document(systems() | {'heating': heating}).scenario_inputs.systems)['heating'], heating)

    def test_lookups_only_for_changed_choices_and_retained_historical_values(self):
        original = full_systems(); original['heating']['sources'][1]['source_code'] = 'ryhti:heating_energy_source:retired'
        original['cooling']['codes'][0] = 'energy:cooling:retired'
        initial = document(original)
        changed = copy.deepcopy(original); changed['heating']['sources'][1]['share_percent'] = -5.25
        changed['heating']['sources'].reverse(); changed['ventilation']['codes'].reverse()
        with patch('src.personal_hvac_contract.load_hvac_catalog', side_effect=HvacCatalogUnavailable):
            patch_v3(initial, PersonalBuildingPatchV3.model_validate({'schema_version': 3, 'set': {'scenario_inputs': {'systems': changed}}}), None)
            for value in (systems(), systems() | {'heated_quantities': {'total_area_m2': -12}}, systems() | {'equipment': {'geothermal_installed': False}}):
                replace_v3(document(value), None)
            with self.assertRaises(HvacCatalogUnavailable): replace_v3(initial, None)
            projection = project_v3(None, initial, building_key='synthetic')
            self.assertEqual(projection.systems_source_provenance[1].source_code.status, 'source_unavailable')
            self.assertEqual(projection.systems_source_provenance[1].share_percent.status, 'available')
            self.assertEqual(projection.field_provenance['scenario_inputs.systems.heated_quantities.total_area_m2'].status, 'available')
        projected = project_v3(None, initial, building_key='synthetic')
        self.assertEqual(projected.systems_source_provenance[1].source_code.status, 'unsupported')
        deprecated = load_hvac_catalog().model_copy(deep=True)
        deprecated.heating_choices[0].selectable = False
        deprecated.heating_choices[0].lifecycle = 'deprecated'
        with patch('src.personal_hvac_contract.load_hvac_catalog', return_value=deprecated):
            with self.assertRaises(ContractValidationError): replace_v3(document(full_systems()), None)
        with patch('src.personal_hvac_contract.load_hvac_catalog', side_effect=AssertionError('unnecessary lookup')) as dependency:
            for value in (systems(), systems() | {'heated_quantities': {'total_area_m2': -12}}, systems() | {'equipment': {'geothermal_installed': False}}):
                project_v3(None, replace_v3(document(value), None), building_key='synthetic')
            dependency.assert_not_called()
        # Loader rejection if the integrity hash is stale, not merely missing fields.
        raw = json.loads(Path('src/data/hvac-catalog-v2.snapshot.json').read_text())
        raw['heating_choices'][0]['label_fi'] = 'synthetic modified label'
        with patch.object(Path, 'read_bytes', return_value=json.dumps(raw).encode()):
            with self.assertRaises(HvacCatalogUnavailable): load_hvac_catalog()

    def test_status_unchanged_policy_no_optional_requirements_or_source_aliases(self):
        for optional in (None, systems(), full_systems()):
            for area in (None, 0, 1):
                doc = PersonalBuildingPutV3.model_validate({'schema_version': 3,
                    'overrides': {'floor_area': area, 'heating_method': '01', 'main_purpose': '01', 'construction_year': 2000},
                    'scenario_inputs': {'systems': optional}})
                projection = project_v3(None, doc, building_key='synthetic')
                expected = 'incomplete' if area is None else 'complete'
                self.assertEqual(projection.input_status.systems.completeness, expected)
                self.assertEqual(projection.input_status.property.completeness, expected)
                self.assertEqual(projection.input_status.systems.unavailable_paths, [])
                self.assertEqual(projection.systems_defaults.coefficients.numeric_defaults, None)
                self.assertFalse(projection.systems_defaults.coefficients.editable)
        missing = project_v3(None, document(full_systems()), building_key='synthetic')
        self.assertEqual(missing.input_status.systems.completeness, 'unavailable')
        no_answers = project_v3(None, document(systems()), building_key='synthetic')
        for field in QUANTITY_FIELDS:
            self.assertEqual(no_answers.field_provenance['scenario_inputs.systems.heated_quantities.' + field].status, 'missing')
        inherited = project_v3(None, document(systems() | {'heated_quantities': None, 'equipment': None}), building_key='synthetic')
        self.assertEqual(inherited.field_provenance['scenario_inputs.systems.heated_quantities.total_area_m2'].source,
                         'scenario_inputs.systems.heated_quantities')


class HvacHttpTests(unittest.TestCase):
    setUp=http_fixture.OwnerHttpBoundaryTests.setUp
    check=http_fixture.OwnerHttpBoundaryTests.check

    def test_discovery_and_pin_loss_no_store(self):
        response=self.client.get('/energy-data/building-hvac-choices');self.check(response,200)
        self.assertEqual(len(response.json()['ventilation_choices']),9)
        self.check(self.client.get('/energy-data/building-hvac-choices?SECRET=value'),422)
        with patch('src.api.building_hvac.load_hvac_catalog',side_effect=HvacCatalogUnavailable):
            response=self.client.get('/energy-data/building-hvac-choices');self.check(response,503)
            self.assertEqual(response.json()['detail']['code'],'hvac_catalog_unavailable')

    def test_v3_union_errors_payload_limits_and_selectors_sanitized(self):
        path='/energy-data/user/buildings/synthetic?personal_schema_version=3'
        for body in ({'schema_version':3,'scenario_inputs':{'systems':{'defaults_version':DEFAULTS_VERSION,'heating':'SECRET'}}},
                {'schema_version':3,'scenario_inputs':{'systems':{'defaults_version':DEFAULTS_VERSION,'ventilation':{'kind':'selected','code':True}}}}):
            response=self.client.put(path,json=body,headers=self.auth);self.check(response,422)
            for item in response.json()['detail']:
                self.assertEqual(set(item),{'loc','type','msg'})
                self.assertFalse(any('V1' in str(x) or 'V2' in str(x) or 'V3' in str(x) for x in item['loc']))
        self.check(self.client.put(path,json={'schema_version':2},headers=self.auth),422)
        self.check(self.client.put(path,content=(' '*32769).encode(),headers=self.auth),413)
        for query in ('personal_schema_version=SECRET','personal_schema_version=3&personal_schema_version=3','personal_schema_version=3.0'):
            response=self.client.get('/readyz/state?'+query);self.check(response,422)
            self.assertTrue(all(set(e)=={'loc','type','msg'} for e in response.json()['detail']))

    def test_structured_errors_address_submitted_members_and_never_echo_tags(self):
        path = '/energy-data/user/buildings/synthetic?personal_schema_version=3'
        cases = []
        candidate = full_systems(); candidate['heating']['sources'][1]['source_id'] = IDS[0]
        cases.append((candidate, ['heating', 'sources', 1, 'source_id']))
        candidate = full_systems(); candidate['heating']['sources'][2]['share_percent'] = True
        cases.append((candidate, ['heating', 'sources', 2, 'share_percent']))
        candidate = full_systems(); candidate['ventilation'] = {'kind': 'multiple', 'codes': ['0', '0']}
        cases.append((candidate, ['ventilation', 'codes', 1]))
        candidate = full_systems(); candidate['cooling'] = {'kind': 'SECRET'}
        cases.append((candidate, ['cooling']))
        for candidate, suffix in cases:
            for method, body, prefix in (('put', {'schema_version': 3, 'scenario_inputs': {'systems': candidate}}, ['body']),
                ('patch', {'schema_version': 3, 'set': {'scenario_inputs': {'systems': candidate}}}, ['body', 'set'])):
                response = getattr(self.client, method)(path, json=body, headers=self.auth)
                self.check(response, 422)
                self.assertEqual(response.json()['detail'][0]['loc'], prefix + ['scenario_inputs', 'systems'] + suffix)
                self.assertTrue(all(set(e) == {'loc', 'type', 'msg'} for e in response.json()['detail']))

    def test_wire_nonfinite_boolean_and_precision_rejection(self):
        path = '/energy-data/user/buildings/synthetic?personal_schema_version=3'
        for raw in ('NaN', 'Infinity', '-Infinity', '1e9999', str(10**400), str(2**53 + 1), 'true', '"0"'):
            payload = '{"schema_version":3,"scenario_inputs":{"systems":{"defaults_version":"' + DEFAULTS_VERSION + '","heated_quantities":{"total_area_m2":' + raw + '}}}}'
            response = self.client.put(path, content=payload, headers=self.auth | {'Content-Type': 'application/json'})
            self.check(response, 422)
            self.assertEqual(response.json()['detail'][0]['loc'], ['body', 'scenario_inputs', 'systems', 'heated_quantities', 'total_area_m2'])


class HvacArtifactTests(unittest.TestCase):
    def test_schemas_examples_exports_and_historical_artifacts_match(self):
        import os
        import tempfile
        import jsonschema
        import subprocess
        from src.api.personal_hvac_examples import hvac_examples
        root=Path(__file__).resolve().parents[1]
        schema=json.loads((root/'src/api/personal-hvac-v3.schema.json').read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        for name,(model,value) in hvac_examples().items():
            model.model_validate_json(json.dumps(value))
            if name!='catalog':
                branch={'$ref':'#/$defs/'+model.__name__,'$defs':schema['$defs']}
                jsonschema.Draft202012Validator(branch).validate(value)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder);(path/'src/api').mkdir(parents=True)
            result=subprocess.run([__import__('sys').executable,'-m','src.api.export_building_contract'],
                cwd=path,env={**os.environ,'PYTHONPATH':str(root)+':'+str(root/'src')},capture_output=True,timeout=20)
            self.assertEqual(result.returncode,0,'temporary deterministic export failed')
            for generated in (path/'src/api').iterdir():
                self.assertEqual(generated.read_bytes(),(root/'src/api'/generated.name).read_bytes(),generated.name)
