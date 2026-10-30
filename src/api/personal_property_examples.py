"""Typed v2 examples for owner-decided fields and the adopted pinned catalog."""
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID

from src.api.building_examples import saved_example, public_example
from src.building_contract import CanonicalBuildingV1
from src.personal_property_contract import PersonalBuildingPutV2, PersonalBuildingPatchV2
from src.user_building_contract import SavedBuildingV2, SavedBuildingPageV2, SavedBuildingSummaryV2
from src.building_share_contract import SharedBuildingV2, shared_projection
from src.user_buildings import detail
import json


def property_examples():
    saved=saved_example()
    scenario={'resident_count':2,'personal_energy_class':{'grade':'C','scale':2018},
        'personal_energy_class_date':{'precision':'year','year':2020},
        'annual_maintenance_charge_eur_cents':123456,'apartment_studio_count':0,
        'apartment_two_room_count':None,'apartment_three_room_count':12,'apartment_four_plus_room_count':1}
    row=SimpleNamespace(**{k:saved[k] for k in ('display_name','revision','share_enabled','building_key','last_known_summary')},
        saved_id=UUID(saved['saved_id']),created_at=datetime.fromisoformat(saved['created_at'].replace('Z','+00:00')),
        updated_at=datetime.fromisoformat(saved['updated_at'].replace('Z','+00:00')),
        schema_version=2,overrides={},scenario_inputs=scenario)
    base=CanonicalBuildingV1.model_validate_json(json.dumps(public_example()))
    owner=detail(row,base,version=2).model_dump(mode='json')
    item=SavedBuildingSummaryV2.model_validate_json(json.dumps({k:owner[k] for k in (
        'schema_version','document_schema_version','saved_id','building_key','display_name','revision','created_at','updated_at',
        'share_enabled','base_status','base_unavailable_reason','last_known_summary')} | {'current_summary':None}))
    return {
        'full_property_request': (PersonalBuildingPutV2, {'schema_version':2,'scenario_inputs':scenario}),
        'partial_draft_request': (PersonalBuildingPutV2, {'schema_version':2,'scenario_inputs':{
            'personal_energy_class_date':{'precision':'year','year':2020},'annual_maintenance_charge_eur_cents':0}}),
        'personal_unknown_request': (PersonalBuildingPatchV2, {'schema_version':2,'set':{'scenario_inputs':{
            'personal_energy_class':None,'personal_energy_class_date':None,'annual_maintenance_charge_eur_cents':None}}}),
        'replace_class_request': (PersonalBuildingPatchV2, {'schema_version':2,'set':{'scenario_inputs':{
            'personal_energy_class':{'grade':'A','scale':2013}}}}),
        'reset_personal_estimates_request': (PersonalBuildingPatchV2, {'schema_version':2,'unset':[
            'scenario_inputs.personal_energy_class','scenario_inputs.personal_energy_class_date',
            'scenario_inputs.annual_maintenance_charge_eur_cents']}),
        'promote_preserving_values_request': (PersonalBuildingPatchV2, {'schema_version':2}),
        'patch_counts_request': (PersonalBuildingPatchV2, {'schema_version':2,'set':{'scenario_inputs':{'apartment_two_room_count':5}}}),
        'reset_counts_request': (PersonalBuildingPatchV2, {'schema_version':2,'unset':['scenario_inputs.apartment_studio_count','scenario_inputs.apartment_two_room_count']}),
        'full_reset_request': (PersonalBuildingPutV2, {'schema_version':2}),
        'owner_detail_v2': (SavedBuildingV2,owner),
        'owner_page_v2': (SavedBuildingPageV2,SavedBuildingPageV2(items=[item],next_cursor=None).model_dump(mode='json')),
        'shared_detail_v2': (SharedBuildingV2,shared_projection(row,base,'available',version=2).model_dump(mode='json')),
    }
