"""Synthetic completed-work v4 examples; thickness is the added layer in mm."""
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID
import json

from src.api.building_examples import public_example
from src.api.personal_hvac_examples import full_systems_example
from src.building_contract import CanonicalBuildingV1
from src.personal_renovations_contract import PersonalBuildingPutV4, PersonalBuildingPatchV4
from src.user_building_contract import SavedBuildingV4, SavedBuildingPageV4, SavedBuildingSummaryV4
from src.building_share_contract import SharedBuildingV4, shared_projection
from src import user_buildings as owner


def completed_works_example():
    return {'facade': {'completed': False}, 'windows': None,
        'plumbing': {'completed': True}, 'roof': {},
        'top_floor_insulation': {'completed': True, 'execution_year': 2025, 'added_insulation_thickness_mm': 230.5},
        'drainage': {'completed': None}, 'energy_smart_solutions': {'completed': True},
        'energy_storage': {'completed': False}}


def renovations_examples():
    body = {'schema_version': 4, 'overrides': {'floor_area': 0, 'total_area': None},
        'scenario_inputs': {'resident_count': 2, 'apartment_studio_count': 0,
            'apartment_two_room_count': None, 'systems': full_systems_example(),
            'renovations': completed_works_example()}}
    base = CanonicalBuildingV1.model_validate_json(json.dumps(public_example()))
    row = SimpleNamespace(saved_id=UUID('11111111-1111-4111-8111-111111111111'), building_key=base.building_key,
        schema_version=4, display_name=None, overrides=body['overrides'], scenario_inputs=body['scenario_inputs'],
        revision=1, created_at=datetime(2026, 11, 9, tzinfo=timezone.utc),
        updated_at=datetime(2026, 11, 9, tzinfo=timezone.utc), share_enabled=True)
    row.last_known_summary = owner.capture_summary(base, row.created_at).model_dump(mode='json')
    summary = SavedBuildingSummaryV4(saved_id=row.saved_id, building_key=row.building_key, display_name=None,
        document_schema_version=4, revision=1, created_at=row.created_at, updated_at=row.updated_at,
        share_enabled=True, base_status='unavailable', base_unavailable_reason='source_unavailable',
        current_summary=None, last_known_summary=owner.stored_summary(row))
    return {
        'minimal_draft': (PersonalBuildingPutV4, {'schema_version': 4}),
        'partial_affirmative': (PersonalBuildingPutV4, {'schema_version': 4, 'scenario_inputs': {'renovations': {'top_floor_insulation': {'completed': True}}}}),
        'full_siblings_and_all_categories': (PersonalBuildingPutV4, body),
        'explicit_zero_thickness': (PersonalBuildingPutV4, {'schema_version': 4, 'scenario_inputs': {'renovations': {'top_floor_insulation': {'completed': True, 'added_insulation_thickness_mm': 0}}}}),
        'section_unknown': (PersonalBuildingPutV4, {'schema_version': 4, 'scenario_inputs': {'renovations': None}}),
        'detail_unknown': (PersonalBuildingPutV4, {'schema_version': 4, 'scenario_inputs': {'renovations': {'top_floor_insulation': {'completed': True, 'execution_year': None, 'added_insulation_thickness_mm': None}}}}),
        'atomic_section_replacement': (PersonalBuildingPatchV4, {'schema_version': 4, 'set': {'scenario_inputs': {'renovations': completed_works_example()}}}),
        'clear_detail_no_work': (PersonalBuildingPatchV4, {'schema_version': 4, 'set': {'scenario_inputs': {'renovations': {'top_floor_insulation': {'completed': False}}}}}),
        'reset_section': (PersonalBuildingPatchV4, {'schema_version': 4, 'unset': ['scenario_inputs.renovations']}),
        'promote_preserving_siblings': (PersonalBuildingPatchV4, {'schema_version': 4}),
        'owner_read': (SavedBuildingV4, owner.detail(row, base, version=4).model_dump(mode='json')),
        'shared_read': (SharedBuildingV4, shared_projection(row, base, 'missing', 4).model_dump(mode='json')),
        'summary_page': (SavedBuildingPageV4, SavedBuildingPageV4(items=[summary], next_cursor=None).model_dump(mode='json')),
    }
