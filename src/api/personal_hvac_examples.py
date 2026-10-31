"""Structured typed examples; all building/owner answers are synthetic."""
from datetime import datetime, timezone
import json
from uuid import UUID

from src.api.building_examples import public_example
from src.building_contract import CanonicalBuildingV1
from src.personal_hvac_contract import PersonalBuildingPutV3, PersonalBuildingPatchV3, project_v3
from src.user_building_contract import SavedBuildingV3, SavedBuildingPageV3, SavedBuildingSummaryV3
from src.building_share_contract import SharedBuildingV3, shared_projection
from src.hvac_catalog import DEFAULTS_VERSION, HvacCatalogV2, load_hvac_catalog
from src import user_buildings as owner
from types import SimpleNamespace


def full_systems_example():
    primary = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
    return {'defaults_version': DEFAULTS_VERSION,
        'heating': {'sources': [
            {'source_id': primary, 'source_code': 'ryhti:heating_energy_source:01', 'share_percent': 65.5},
            {'source_id': 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', 'source_code': 'ryhti:heating_energy_source:1101', 'share_percent': 0},
            {'source_id': 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', 'source_code': 'ryhti:heating_energy_source:04', 'share_percent': None}],
            'primary_source_id': primary},
        'heated_quantities': {'total_area_m2': 0, 'total_volume_m3': None, 'residential_area_m2': 120.25, 'residential_volume_m3': 360.5},
        'cooling': {'kind': 'selected', 'codes': ['energy:cooling:district', 'energy:cooling:heat_pump']},
        'ventilation': {'kind': 'multiple', 'codes': ['0', '8']},
        'equipment': {'geothermal_installed': False, 'solar_panels_installed': True, 'energy_storage_ready': None}}


def hvac_examples():
    def s(value=...):
        answer={'defaults_version':DEFAULTS_VERSION}
        if value is not ...: answer['ventilation']=value
        return answer
    body={'schema_version':3,'overrides':{'ventilation_type_id':'0','ventilation_description':'Legacy personal note'},
          'scenario_inputs':{'apartment_studio_count':0,'apartment_two_room_count':None,
                            'systems':full_systems_example()}}
    body['overrides'].update(main_purpose='01', construction_year=2000, floor_area=0, heating_method='01')
    base=CanonicalBuildingV1.model_validate_json(json.dumps(public_example()))
    row=SimpleNamespace(saved_id=UUID('11111111-1111-4111-8111-111111111111'),building_key=base.building_key,
        schema_version=3,display_name=None,overrides=body['overrides'],scenario_inputs=body['scenario_inputs'],
        revision=1,created_at=datetime(2026,10,30,tzinfo=timezone.utc),updated_at=datetime(2026,10,30,tzinfo=timezone.utc),share_enabled=True)
    row.last_known_summary=owner.capture_summary(base,row.created_at).model_dump(mode='json')
    saved=owner.detail(row,base,version=3)
    shared=shared_projection(row,base,'missing',3)
    summary=SavedBuildingSummaryV3(saved_id=row.saved_id,building_key=row.building_key,display_name=None,
        document_schema_version=3,revision=1,created_at=row.created_at,updated_at=row.updated_at,
        share_enabled=True,base_status='unavailable',base_unavailable_reason='source_unavailable',
        current_summary=None,last_known_summary=owner.stored_summary(row))
    examples={
        'full_inputs_complete_existing_policy':(PersonalBuildingPutV3,body),
        'additional_only_partial':(PersonalBuildingPutV3,{'schema_version':3,'scenario_inputs':{'systems':{'defaults_version':DEFAULTS_VERSION,'heating':{'sources':[{'source_id':'dddddddd-dddd-4ddd-8ddd-dddddddddddd','source_code':'ryhti:heating_energy_source:07'}]}}}}),
        'numeric_equipment_only_partial':(PersonalBuildingPutV3,{'schema_version':3,'scenario_inputs':{'systems':{'defaults_version':DEFAULTS_VERSION,'heated_quantities':{'total_area_m2':-1.25},'equipment':{'geothermal_installed':False}}}}),
        'partial_initial_save':(PersonalBuildingPutV3,{'schema_version':3,'scenario_inputs':{'systems':s()}}),
        'explicit_systems_unknown':(PersonalBuildingPutV3,{'schema_version':3,'scenario_inputs':{'systems':None}}),
        'ventilation_unknown':(PersonalBuildingPutV3,{'schema_version':3,'scenario_inputs':{'systems':s(None)}}),
        'ventilation_none':(PersonalBuildingPutV3,{'schema_version':3,'scenario_inputs':{'systems':s({'kind':'none'})}}),
        'independent_ventilation_and_property_zero_unknown':(PersonalBuildingPutV3,body),
        'atomic_selection_change':(PersonalBuildingPatchV3,{'schema_version':3,'set':{'scenario_inputs':{'systems':s({'kind':'selected','code':'0'})}}}),
        'reset_ventilation':(PersonalBuildingPatchV3,{'schema_version':3,'set':{'scenario_inputs':{'systems':s()}}}),
        'unset_systems':(PersonalBuildingPatchV3,{'schema_version':3,'unset':['scenario_inputs.systems']}),
        'promote_preserving_siblings':(PersonalBuildingPatchV3,{'schema_version':3}),
        'owner_read':(SavedBuildingV3,saved.model_dump(mode='json')),
        'shared_read':(SharedBuildingV3,shared.model_dump(mode='json')),
        'owner_page':(SavedBuildingPageV3,SavedBuildingPageV3(items=[summary],next_cursor=None).model_dump(mode='json')),
        'catalog':(HvacCatalogV2,load_hvac_catalog().model_dump(mode='json')),
    }
    return examples
