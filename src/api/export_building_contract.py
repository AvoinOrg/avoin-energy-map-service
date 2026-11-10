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
    print('Closed v1 schema and 10 complete typed examples validated and exported.')
    export_property()


def export_property():
    from src.personal_property_contract import PersonalBuildingPutV2, PersonalBuildingPatchV2, PersonalBuildingProjectionV2
    from src.user_building_contract import SavedBuildingV2, SavedBuildingPageV2, SavedBuildingSummaryV2
    from src.building_share_contract import SharedBuildingV2
    from src.purpose_catalog import PurposeCatalogV1
    from src.api.personal_property_examples import property_examples
    modes=[(model, 'serialization') for model in (PersonalBuildingProjectionV2, SavedBuildingV2,
        SavedBuildingPageV2, SavedBuildingSummaryV2, SharedBuildingV2)] + [
        (model, 'validation') for model in (PersonalBuildingPutV2, PersonalBuildingPatchV2)]
    _, schema=models_json_schema(modes, title='EnergyMapPersonalPropertyV2')
    Path('src/api/personal-property-v2.schema.json').write_text(json.dumps(schema,indent=2)+'\n')
    _, catalog=models_json_schema([(PurposeCatalogV1,'serialization')],title='EnergyMapPurposeCatalogV1')
    Path('src/api/purpose-catalog-v1.schema.json').write_text(json.dumps(catalog,indent=2)+'\n')
    examples={}
    for name, (model, value) in property_examples().items():
        model.model_validate_json(json.dumps(value))
        examples[name]=value
    Path('src/api/personal-property-v2.examples.json').write_text(json.dumps(examples,indent=2)+'\n')
    print(f'Personal v2 schema, {len(examples)} typed examples and purpose catalog schema exported.')




def export_hvac():
    """Export v3 independently; never regenerate/widen historical artifacts."""
    from src.personal_hvac_contract import PersonalBuildingPutV3, PersonalBuildingPatchV3, PersonalBuildingProjectionV3
    from src.user_building_contract import SavedBuildingV3, SavedBuildingPageV3, SavedBuildingSummaryV3
    from src.building_share_contract import SharedBuildingV3
    from src.hvac_catalog import HvacCatalogV2
    from src.api.personal_hvac_examples import hvac_examples
    modes=[(m,'serialization') for m in (PersonalBuildingProjectionV3,SavedBuildingV3,
        SavedBuildingPageV3,SavedBuildingSummaryV3,SharedBuildingV3)] + [
        (m,'validation') for m in (PersonalBuildingPutV3,PersonalBuildingPatchV3)]
    _,schema=models_json_schema(modes,title='EnergyMapPersonalHvacV3')
    Path('src/api/personal-hvac-v3.schema.json').write_text(json.dumps(schema,indent=2)+'\n')
    _,catalog=models_json_schema([(HvacCatalogV2,'serialization')],title='EnergyMapHvacCatalogV2')
    Path('src/api/hvac-catalog-v2.schema.json').write_text(json.dumps(catalog,indent=2)+'\n')
    examples={}
    for name,(model,value) in hvac_examples().items():
        model.model_validate_json(json.dumps(value))
        examples[name]=value
    Path('src/api/personal-hvac-v3.examples.json').write_text(json.dumps(examples,indent=2)+'\n')
    print(f'Structured HVAC v3 schemas and {len(examples)} typed examples exported.')


def export_renovations():
    """Export only the provisional v4 successor; historical artifacts stay frozen."""
    from src.personal_renovations_contract import PersonalBuildingPutV4, PersonalBuildingPatchV4, PersonalBuildingProjectionV4
    from src.user_building_contract import SavedBuildingV4, SavedBuildingPageV4, SavedBuildingSummaryV4
    from src.building_share_contract import SharedBuildingV4
    from src.api.personal_renovations_examples import renovations_examples
    modes = [(m, 'serialization') for m in (PersonalBuildingProjectionV4, SavedBuildingV4,
        SavedBuildingPageV4, SavedBuildingSummaryV4, SharedBuildingV4)] + [
        (m, 'validation') for m in (PersonalBuildingPutV4, PersonalBuildingPatchV4)]
    _, schema = models_json_schema(modes, title='EnergyMapPersonalRenovationsV4Candidate')
    Path('src/api/personal-renovations-v4.schema.json').write_text(json.dumps(schema, indent=2) + '\n')
    examples = {}
    for name, (model, value) in renovations_examples().items():
        model.model_validate_json(json.dumps(value))
        examples[name] = value
    Path('src/api/personal-renovations-v4.examples.json').write_text(json.dumps(examples, indent=2) + '\n')
    print(f'Candidate renovations v4 schema and {len(examples)} typed examples exported; added-layer thickness in mm supported.')

def export_utilities():
    """Export only the closed v5 successor; historical artifacts stay frozen."""
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
    elif sys.argv[1:] == ['--renovations-only']:
        export_renovations()
    elif sys.argv[1:] == ['--hvac-only']:
        export_hvac()
    elif not sys.argv[1:]:
        export()
        export_hvac()
    else:
        raise SystemExit('Usage: python -m src.api.export_building_contract [--hvac-only|--renovations-only|--utilities-only]')
