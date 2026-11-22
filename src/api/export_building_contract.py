"""Regenerate versioned schema/examples without database/provider access.

Run python -m src.api.export_building_contract from the repository root.
"""
import json
from pathlib import Path

from pydantic.json_schema import models_json_schema

from src.building_contract import (
    BuildingSummaryV1, CanonicalBuildingV1, LastKnownBuildingSummaryV1,
)
from src.personal_utilities_contract import PersonalBuildingPatchV5, PersonalBuildingProjectionV5, PersonalBuildingPutV5
from src.building_share_contract import SharedBuildingV5, ShareCreatedV1
from src.user_building_contract import SavedBuildingV5, SavedBuildingSummaryV5, SavedBuildingPageV5
from src.api.building_examples import (
    public_example, saved_example, shared_example, share_created_example, saved_page_example,
)


def contract_examples():
    return {
        'public_detail': (CanonicalBuildingV1, public_example()),
        'owner_saved': (SavedBuildingV5, saved_example()),
        'owner_list': (SavedBuildingPageV5, saved_page_example()),
        'share_created': (ShareCreatedV1, share_created_example()),
        'shared_detail': (SharedBuildingV5, shared_example()),
        'first_save_request': (PersonalBuildingPutV5, {'schema_version': 5, 'display_name': 'Home',
            'overrides': {'floor_area': 0, 'total_area': None}, 'scenario_inputs': {'resident_count': 2}}),
        'full_replace_request': (PersonalBuildingPutV5, {'schema_version': 5, 'overrides': {'floor_area': 120}}),
        'patch_request': (PersonalBuildingPatchV5, {'schema_version': 5, 'set': {'overrides': {'floor_area': 150, 'total_area': None},
            'scenario_inputs': {'resident_count': 4}}, 'unset': ['overrides.heated_net_area']}),
        'reset_request': (PersonalBuildingPatchV5, {'schema_version': 5, 'display_name': None,
            'unset': ['overrides.floor_area', 'scenario_inputs.resident_count']}),
        'reset_all_request': (PersonalBuildingPutV5, {'schema_version': 5}),
    }


def export():
    response_models = [CanonicalBuildingV1, PersonalBuildingProjectionV5, BuildingSummaryV1,
        LastKnownBuildingSummaryV1, SavedBuildingV5, SavedBuildingSummaryV5, SavedBuildingPageV5,
        SharedBuildingV5, ShareCreatedV1]
    modes = [(model, 'serialization') for model in response_models] + [
        (model, 'validation') for model in (PersonalBuildingPutV5, PersonalBuildingPatchV5)]
    _, schema = models_json_schema(modes, title='EnergyMapBuildingContract')
    Path('src/api/building-contract.schema.json').write_text(json.dumps(schema, indent=2)+'\n')
    examples = {}
    for name, (model, value) in contract_examples().items():
        model.model_validate_json(json.dumps(value))
        examples[name] = value
    Path('src/api/building-api.examples.json').write_text(json.dumps(examples, indent=2)+'\n')
    from src.hvac_catalog import HvacCatalogV2
    from src.purpose_catalog import PurposeCatalogV1
    for name, model, title in [('purpose-catalog-v1', PurposeCatalogV1, 'EnergyMapPurposeCatalogV1'),
                               ('hvac-catalog-v2', HvacCatalogV2, 'EnergyMapHvacCatalogV2')]:
        _, catalog = models_json_schema([(model, 'serialization')], title=title)
        Path(f'src/api/{name}.schema.json').write_text(json.dumps(catalog, indent=2)+'\n')
    export_utilities()


def export_utilities():
    """Export the personal v5 schema and examples."""
    from src.personal_utilities_contract import PersonalBuildingPutV5, PersonalBuildingPatchV5, PersonalBuildingProjectionV5
    from src.user_building_contract import SavedBuildingV5, SavedBuildingPageV5, SavedBuildingSummaryV5
    from src.building_share_contract import SharedBuildingV5
    from src.api.personal_utilities_examples import utilities_examples
    modes = [(m, 'serialization') for m in (PersonalBuildingProjectionV5, SavedBuildingV5,
        SavedBuildingPageV5, SavedBuildingSummaryV5, SharedBuildingV5)] + [
        (m, 'validation') for m in (PersonalBuildingPutV5, PersonalBuildingPatchV5)]
    _, schema = models_json_schema(modes, title='EnergyMapPersonalUtilitiesV5Closed')
    Path('src/api/personal-utilities-v5.schema.json').write_text(json.dumps(schema, indent=2) + '\n')
    examples = {}
    for name, (model, value) in utilities_examples().items():
        model.model_validate_json(json.dumps(value))
        examples[name] = value
    Path('src/api/personal-utilities-v5.examples.json').write_text(json.dumps(examples, indent=2) + '\n')
    print(f'Closed utilities v5 schema and {len(examples)} typed examples exported.')


if __name__ == '__main__':
    import sys
    if sys.argv[1:] == ['--utilities-only']:
        export_utilities()
    elif not sys.argv[1:]:
        export()
    else:
        raise SystemExit('Usage: python -m src.api.export_building_contract [--utilities-only]')
