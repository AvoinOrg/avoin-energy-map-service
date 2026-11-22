"""Synthetic utilities v5 examples; values are illustrative, never defaults."""
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID
import json

from src.api.building_examples import public_example
from src.api.personal_hvac_examples import full_systems_example
from src.building_contract import CanonicalBuildingV1
from src.personal_utilities_contract import PersonalBuildingPutV5, PersonalBuildingPatchV5
from src.user_building_contract import SavedBuildingV5, SavedBuildingPageV5, SavedBuildingSummaryV5
from src.building_share_contract import SharedBuildingV5, shared_projection
from src import user_buildings as owner
from src.api.personal_renovations_examples import completed_works_example


def utilities_example():
    return {'electricity': {'grid_import_kwh': 12345.5, 'calendar_year': 2025},
        'water': {'supplied_volume_m3': 456.25, 'calendar_year': 2024}}


def utilities_examples():
    body = {'schema_version': 5, 'overrides': {'floor_area': 0, 'total_area': None},
        'scenario_inputs': {'resident_count': 2, 'apartment_studio_count': 0,
            'apartment_two_room_count': None, 'systems': full_systems_example(),
            'renovations': completed_works_example(), 'utilities': utilities_example()}}
    base = CanonicalBuildingV1.model_validate_json(json.dumps(public_example()))
    row = SimpleNamespace(saved_id=UUID('11111111-1111-4111-8111-111111111111'), building_key=base.building_key,
        schema_version=5, display_name=None, overrides=body['overrides'], scenario_inputs=body['scenario_inputs'],
        revision=1, created_at=datetime(2026, 11, 9, tzinfo=timezone.utc),
        updated_at=datetime(2026, 11, 9, tzinfo=timezone.utc), share_enabled=True)
    row.last_known_summary = owner.capture_summary(base, row.created_at).model_dump(mode='json')
    summary = SavedBuildingSummaryV5(saved_id=row.saved_id, building_key=row.building_key, display_name=None,
        document_schema_version=5, revision=1, created_at=row.created_at, updated_at=row.updated_at,
        share_enabled=True, base_status='unavailable', base_unavailable_reason='source_unavailable',
        current_summary=None, last_known_summary=owner.stored_summary(row))
    return {
        'whole_document_request': (PersonalBuildingPutV5, body),
        'empty_draft': (PersonalBuildingPutV5, {'schema_version': 5}),
        'year_only_draft': (PersonalBuildingPutV5, {'schema_version': 5, 'scenario_inputs': {
            'utilities': {'water': {'calendar_year': 2024}}}}),
        'unknown_section': (PersonalBuildingPatchV5, {'schema_version': 5, 'set': {
            'scenario_inputs': {'utilities': None}}}),
        'empty_group': (PersonalBuildingPatchV5, {'schema_version': 5, 'set': {
            'scenario_inputs': {'utilities': {}}}}),
        'zero_unknown_and_omitted_year': (PersonalBuildingPatchV5, {'schema_version': 5,
            'set': {'scenario_inputs': {'utilities': {'electricity': {'grid_import_kwh': 0},
                'water': {'supplied_volume_m3': None, 'calendar_year': None}}}}}),
        'retain_water_replace_electricity': (PersonalBuildingPatchV5, {'schema_version': 5,
            'set': {'scenario_inputs': {'utilities': utilities_example()}}}),
        'reset_section': (PersonalBuildingPatchV5, {'schema_version': 5, 'unset': ['scenario_inputs.utilities']}),
        'owner_read': (SavedBuildingV5, owner.detail(row, base).model_dump(mode='json')),
        'shared_read': (SharedBuildingV5, shared_projection(row, base, 'missing').model_dump(mode='json')),
        'summary_page': (SavedBuildingPageV5, SavedBuildingPageV5(items=[summary], next_cursor=None).model_dump(mode='json')),
    }
