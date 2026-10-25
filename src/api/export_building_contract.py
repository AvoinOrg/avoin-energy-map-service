"""Regenerate versioned schema/examples without database/provider access.

Run python -m src.api.export_building_contract from the repository root.
"""
import json
from pathlib import Path

from pydantic.json_schema import models_json_schema

from src.building_contract import (
    BuildingSummaryV1, CanonicalBuildingV1, LastKnownBuildingSummaryV1,
    PersonalBuildingPatchV1, PersonalBuildingProjectionV1, PersonalBuildingPutV1,
)
from src.building_share_contract import SharedBuildingV1, ShareCreatedV1
from src.user_building_contract import SavedBuildingV1, SavedBuildingSummaryV1, SavedBuildingPageV1
from src.api.building_examples import (
    public_example, saved_example, shared_example, share_created_example, saved_page_example,
)


def contract_examples():
    return {
        'public_detail': (CanonicalBuildingV1, public_example()),
        'owner_saved': (SavedBuildingV1, saved_example()),
        'owner_list': (SavedBuildingPageV1, saved_page_example()),
        'share_created': (ShareCreatedV1, share_created_example()),
        'shared_detail': (SharedBuildingV1, shared_example()),
        'first_save_request': (PersonalBuildingPutV1, {'schema_version': 1, 'display_name': 'Home',
            'overrides': {'floor_area': 0, 'total_area': None}, 'scenario_inputs': {'resident_count': 2}}),
        'full_replace_request': (PersonalBuildingPutV1, {'schema_version': 1, 'overrides': {'floor_area': 120}}),
        'patch_request': (PersonalBuildingPatchV1, {'set': {'overrides': {'floor_area': 150, 'total_area': None},
            'scenario_inputs': {'resident_count': 4}}, 'unset': ['overrides.heated_net_area']}),
        'reset_request': (PersonalBuildingPatchV1, {'display_name': None,
            'unset': ['overrides.floor_area', 'scenario_inputs.resident_count']}),
        'reset_all_request': (PersonalBuildingPutV1, {}),
    }


def export():
    response_models = [CanonicalBuildingV1, PersonalBuildingProjectionV1, BuildingSummaryV1,
        LastKnownBuildingSummaryV1, SavedBuildingV1, SavedBuildingSummaryV1, SavedBuildingPageV1,
        SharedBuildingV1, ShareCreatedV1]
    modes = [(model, 'serialization') for model in response_models] + [
        (model, 'validation') for model in (PersonalBuildingPutV1, PersonalBuildingPatchV1)]
    _, schema = models_json_schema(modes, title='EnergyMapBuildingContractV1')
    Path('src/api/building-contract-v1.schema.json').write_text(json.dumps(schema, indent=2)+'\n')
    examples = {}
    for name, (model, value) in contract_examples().items():
        model.model_validate_json(json.dumps(value))
        examples[name] = value
    Path('src/api/building-api-v1.examples.json').write_text(json.dumps(examples, indent=2)+'\n')
    print('Closed schema and 10 complete typed examples validated and exported.')


if __name__ == '__main__':
    export()
