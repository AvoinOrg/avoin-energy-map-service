import copy
from datetime import datetime, timezone
import unittest

from pydantic import ValidationError

from src.building_contract import *
from src.api.buildings import public_example


CATALOG = CodeCatalog({'main_purpose': frozenset({'05', '06', '99'}),
                       'heating_method': frozenset({'01', '03'}),
                       'heating_energy_source': frozenset({'01', '04', '09'})})


class BuildingContractTests(unittest.TestCase):
    def base(self):
        base = CanonicalBuildingV1.model_validate_json(__import__('json').dumps(public_example()))
        base.details = DetailsV1(floor_area=80, heated_net_area=70, main_purpose='05',
                                ventilation_type_id='0', ventilation_category='gravity')
        base.field_provenance = {f'details.{key}': FieldProvenanceV1(
            origin='certificate' if key == 'heated_net_area' else 'ryhti',
            source='fixture.' + key, status='available' if value is not None else 'missing')
            for key, value in base.details.model_dump().items()}
        base.certificate = CertificateV1(certificate_id='001', heated_net_area=70, energy_class='B')
        base.model = ModelV1(energy_class='B', is_energy_class_modeled=False)
        return base

    def test_all_editable_leaves_omission_null_and_good_values(self):
        valid = {'address_fin':'Street 1', 'postal_code':'00100', 'postal_office_fin':'City',
                 'construction_year':2026, 'main_purpose':'05', 'heating_method':'01',
                 'heating_energy_source':'04', 'number_of_storeys':0, 'apartment_count':0,
                 'floor_area':0, 'total_area':1.25, 'gross_floor_area':1, 'heated_net_area':None,
                 'volume':0, 'ventilation_type_id':'0', 'ventilation_description':'Note\n\tcontinued'}
        self.assertEqual(set(valid), set(EDITABLE_FIELDS))
        for leaf, value in valid.items():
            with self.subTest(leaf=leaf):
                good = PersonalBuildingPutV1(overrides={leaf:value})
                self.assertEqual(personal_document(good)['overrides'], {leaf:value})
                null = PersonalBuildingPutV1(overrides={leaf:None})
                self.assertEqual(personal_document(null)['overrides'], {leaf:None})
        self.assertEqual(personal_document(PersonalBuildingPutV1())['overrides'], {})

    def test_numeric_strictness_and_limits_for_every_numeric_field(self):
        for leaf, definition in EDITABLE_FIELDS.items():
            if definition.kind not in ('int', 'year', 'number'):
                continue
            for value in (True, False, '1', -1, float('nan'), float('inf'), -float('inf'), 10**1000):
                with self.subTest(leaf=leaf, value=str(value)[:20]):
                    with self.assertRaises(ValidationError):
                        PersonalBuildingPutV1(overrides={leaf:value})
            if definition.kind in ('int', 'year'):
                with self.assertRaises(ValidationError):
                    PersonalBuildingPutV1(overrides={leaf:1.0})
            elif definition.kind == 'number':
                with self.assertRaises(ValidationError):
                    PersonalBuildingPutV1(overrides={leaf:definition.maximum+1})
        for value in (0, True, 1.0, '1', 10001, -1):
            with self.assertRaises(ValidationError):
                ScenarioInputsV1(resident_count=value)
        for value in (1, 10000, None):
            self.assertEqual(ScenarioInputsV1(resident_count=value).resident_count, value)

    def test_year_uses_injected_utc_clock(self):
        BuildingOverridesV1.model_validate({'construction_year':2031}, context={'current_year':2026})
        for value in (2032, 0, '2026-01-01'):
            with self.assertRaises(ValidationError):
                BuildingOverridesV1.model_validate({'construction_year':value}, context={'current_year':2026})

    def test_text_code_and_namespace_bounds(self):
        for leaf, definition in EDITABLE_FIELDS.items():
            if definition.kind not in ('text', 'note', 'code', 'postal', 'ventilation'):
                continue
            for value in ('', '  ', '\x00', 'x'*(definition.maximum+1), 0, True):
                with self.subTest(leaf=leaf):
                    with self.assertRaises(ValidationError):
                        BuildingOverridesV1.model_validate({leaf:value})
        for doc in ({'overrides':{'postal_code':'00100\n'}}, {'schema_version':True}, {'schema_version':1.0}, {'schema_version':'1'}, {'schema_version':2},
                    {'overrides':None}, {'scenario_inputs':None}, {'owner':'forged'},
                    {'overrides':{'energy_class':'A'}}, {'scenario_inputs':{'price':1}},
                    {'overrides':{'completion_date':'2026-02-30'}}):
            with self.assertRaises(ValidationError):
                PersonalBuildingPutV1.model_validate(doc)
        for name in ('', ' ', 'x'*121, 'a\nb', 'a\tb', '\x7f'):
            with self.assertRaises(ValidationError):
                PersonalBuildingPutV1(display_name=name)
        plain = '<script>alert(1)</script>'
        self.assertEqual(PersonalBuildingPutV1(display_name=plain).display_name, plain)
        with self.assertRaises(Exception):
            check_payload_size({'text':'😀'*9000})

    def test_code_catalog_membership_and_unavailability(self):
        doc = PersonalBuildingPutV1(overrides={'main_purpose':'05','heating_method':'01','ventilation_type_id':'0','postal_code':'00100'})
        self.assertEqual(personal_document(replace_personal(doc, CATALOG)), personal_document(doc))
        with self.assertRaises(CatalogUnavailable):
            replace_personal(doc, None)
        with self.assertRaises(ContractValidationError) as caught:
            replace_personal(PersonalBuildingPutV1(overrides={'main_purpose':'5'}), CATALOG)
        self.assertEqual(validation_errors(caught.exception)[0]['loc'], ['body','overrides','main_purpose'])
        with self.assertRaises(ValidationError):
            PersonalBuildingPutV1(overrides={'main_purpose':5})

    def test_put_replacement_and_patch_leaf_semantics(self):
        old = PersonalBuildingPutV1(display_name='Old',overrides={'floor_area':80,'main_purpose':'05'},scenario_inputs={'resident_count':3})
        before = personal_document(old)
        patch = PersonalBuildingPatchV1(set={'overrides':{'floor_area':0,'heated_net_area':None}})
        changed = apply_personal_patch(old,patch,None)
        self.assertEqual(changed.display_name,'Old')
        self.assertEqual(sparse(changed.overrides), {'floor_area':0,'heated_net_area':None,'main_purpose':'05'})
        changed = apply_personal_patch(changed,PersonalBuildingPatchV1(display_name=None,unset=['overrides.floor_area','scenario_inputs.resident_count']),None)
        self.assertIsNone(changed.display_name)
        self.assertEqual(sparse(changed.scenario_inputs),{})
        self.assertNotIn('floor_area', sparse(changed.overrides))
        self.assertEqual(personal_document(old),before)
        self.assertEqual(personal_document(replace_personal(PersonalBuildingPutV1(),None)),
                         {'schema_version':1,'display_name':None,'overrides':{},'scenario_inputs':{}})
        unchanged = apply_personal_patch(changed,PersonalBuildingPatchV1(unset=['overrides.floor_area']),None)
        self.assertEqual(personal_document(unchanged),personal_document(changed))

    def test_patch_conflicts_report_both_paths_and_do_not_mutate(self):
        with self.assertRaises(ValidationError) as caught:
            PersonalBuildingPatchV1(set={'scenario_inputs':{'resident_count':4}},unset=['scenario_inputs.resident_count'])
        errors=validation_errors(caught.exception)
        self.assertEqual({tuple(e['loc']) for e in errors}, {('body','unset',0),('body','set','scenario_inputs','resident_count')})
        for doc in ({'unset':['overrides.floor_area']*2}, {'unset':['overrides']}, {'unset':['overrides.*']},
                    {'unset':['/overrides/floor_area']}, {'unset':['display_name']}, {'unset':None}, {'set':None},
                    {'set':{'overrides':None}}, {'set':{'scenario_inputs':{'resident_count':True}}}):
            with self.assertRaises(ValidationError):
                PersonalBuildingPatchV1.model_validate(doc)
        old = PersonalBuildingPutV1(overrides={'main_purpose':'05'})
        before = personal_document(old)
        with self.assertRaises(ContractValidationError):
            apply_personal_patch(old,PersonalBuildingPatchV1(set={'overrides':{'main_purpose':'00'}}),CATALOG)
        self.assertEqual(before,personal_document(old))

    def test_catalog_refresh_does_not_erase_historical_valid_codes_on_reset(self):
        old = PersonalBuildingPutV1(overrides={'main_purpose':'deprecated','floor_area':5})
        changed = apply_personal_patch(old,PersonalBuildingPatchV1(unset=['overrides.floor_area']),CATALOG)
        self.assertEqual(changed.overrides.main_purpose,'deprecated')
        with self.assertRaises(ContractValidationError):
            apply_personal_patch(old,PersonalBuildingPatchV1(set={'overrides':{'main_purpose':'deprecated'}}),CATALOG)
        apply_personal_patch(old,PersonalBuildingPatchV1(unset=['overrides.main_purpose']),None)

    def test_overlay_source_user_null_zero_refresh_and_reset(self):
        base=self.base(); before=base.model_dump(mode='json')
        personal=BuildingOverridesV1(floor_area=0,heated_net_area=None,ventilation_type_id='1')
        projected=project_building(base,personal,ScenarioInputsV1(resident_count=None))
        self.assertEqual(projected.effective.details.floor_area,0)
        self.assertIsNone(projected.effective.details.heated_net_area)
        self.assertEqual(projected.effective.certificate.heated_net_area,70)
        self.assertEqual(projected.effective.model.energy_class,'B')
        self.assertEqual(projected.effective.details.ventilation_category,'mechanical')
        self.assertTrue(projected.field_provenance['details.ventilation_category'].is_override)
        self.assertEqual(projected.field_provenance['details.heated_net_area'].status,'user_unknown')
        self.assertEqual(projected.field_provenance['scenario_inputs.resident_count'].status,'user_unknown')
        self.assertEqual(base.model_dump(mode='json'),before)
        base.details.floor_area=99
        reset=project_building(base,BuildingOverridesV1(),ScenarioInputsV1())
        self.assertEqual(reset.effective.details.floor_area,99)
        self.assertFalse(reset.field_provenance['details.floor_area'].is_override)
        self.assertEqual(reset.model_dump()['overrides'],{})
        self.assertEqual(projected.model_dump()['scenario_inputs'],{'resident_count':None})
        self.assertEqual(projected.overrides.floor_area,0)

    def test_historical_summary_is_not_current_fallback(self):
        summary=LastKnownBuildingSummaryV1(building_key='00-key',address_fin='Historical',source_availability={},captured_at=datetime.now(timezone.utc))
        for reason in ('missing','ambiguous','source_unavailable'):
            projected=project_building(None,BuildingOverridesV1(floor_area=20),ScenarioInputsV1(resident_count=3),last_known_summary=summary,base_unavailable_reason=reason)
            self.assertIsNone(projected.base)
            self.assertEqual(projected.base_status,'unavailable')
            self.assertIsNone(projected.effective.details.address_fin)
            self.assertIsNone(projected.effective.details.ventilation_category)
            self.assertEqual(projected.source_availability['geometry'].status,'source_unavailable')
            self.assertEqual(projected.effective.details.floor_area,20)
            self.assertEqual(projected.last_known_summary.address_fin,'Historical')
        with self.assertRaises(ValueError):
            project_building(self.base(),BuildingOverridesV1(),ScenarioInputsV1(),last_known_summary=summary)
        with self.assertRaises(ValidationError):
            LastKnownBuildingSummaryV1(**{**summary.model_dump(),'owner':'secret'})

    def test_consumption_typed_totals_and_carrier_identity(self):
        data={'schema_version':1,'basis':'standardized_calculation','unit':'kWh/year','building_identifier':'0001',
              'certificate_id':'0002','certificate_version':2018,'signed_at':None,'valid_until':None,
              'heated_net_area_m2':10,'completeness':'complete','total_kwh_year':0,
              'components':[{'carrier':c,'annual_kwh':0} for c in ('electricity','district_heat','district_cooling','fossil_fuel','renewable_fuel')]}
        self.assertEqual(CertificateConsumptionV1.model_validate(data).total_kwh_year,0)
        for change in ({'total_kwh_year':1},{'basis':'metered'},{'unit':'MWh/year'},
                       {'components':[{'carrier':'electricity','annual_kwh':1}]*2},
                       {'completeness':'partial','total_kwh_year':0}):
            with self.assertRaises(ValidationError):
                CertificateConsumptionV1.model_validate({**data,**change})

    def test_keys_are_opaque_and_bounded_in_bytes(self):
        for key in ('00123','Not-a-UUID',' key ','ä'*128):
            self.assertEqual(building_key(key),key)
        for key in ('', 'ä'*129,'a\x00b', 123):
            with self.assertRaises(Exception): building_key(key)
