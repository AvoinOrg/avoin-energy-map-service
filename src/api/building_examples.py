"""Synthetic schema-valid examples. No persisted capabilities or account data."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from uuid import UUID

from src.api.buildings import public_example
from src.building_contract import CanonicalBuildingV1
from src.building_share_contract import ShareCreatedV1, shared_projection
from src.user_buildings import capture_summary, detail


def example_row():
    base = CanonicalBuildingV1.model_validate_json(json.dumps(public_example()))
    now = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
    row = SimpleNamespace(saved_id=UUID('00000000-0000-0000-0000-000000000001'),
        building_key=base.building_key, display_name='Shared example home',
        schema_version=1, overrides={'floor_area': 0, 'total_area': None},
        scenario_inputs={'resident_count': 2}, revision=2, created_at=now,
        updated_at=now, share_enabled=True,
        last_known_summary=capture_summary(base, now).model_dump(mode='json'))
    return row, base


def saved_example():
    row, base = example_row()
    return detail(row, base).model_dump(mode='json')


def shared_example():
    row, base = example_row()
    return shared_projection(row, base, 'available').model_dump(mode='json')


def share_created_example():
    # Documentation spelling only; never created in a database.
    return ShareCreatedV1(share_token='e'*43,
        resolver_path='/energy-data/shared-buildings/'+'e'*43,
        revision=2, updated_at=datetime(2026,10,23,12,tzinfo=timezone.utc)).model_dump(mode='json')


def saved_page_example():
    saved = saved_example()
    summary = {key: saved[key] for key in ('schema_version', 'saved_id', 'building_key',
        'display_name', 'revision', 'created_at', 'updated_at', 'share_enabled',
        'base_status', 'base_unavailable_reason', 'last_known_summary')}
    summary['current_summary'] = {key: value for key, value in saved['last_known_summary'].items() if key != 'captured_at'}
    return {'schema_version': 1, 'items': [summary], 'next_cursor': None}
