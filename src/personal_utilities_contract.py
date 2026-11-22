"""Closed personal utilities v5; no source or calculation effects."""
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, ValidationInfo, field_validator

from src.building_contract import (
    AvailabilityV1, BuildingOverridesV1, ContractModel, EffectiveV1, FieldProvenanceV1,
    PersonalBuildingPatchV1, PersonalBuildingProjectionV1, PersonalBuildingPutV1, UNSET_PATHS,
    check_payload_size, fail, personal_document, project_building, sparse, validate_new_codes,
)
from src.personal_property_contract import NEW_PATHS, PERSONAL_READ_PATHS, property_input_status
from src.personal_hvac_contract import (
    DefaultResolutionV3, HeatingSourceProvenanceV3, InputStatusV3, SYSTEMS_PATH, SYSTEMS_PATHS,
    project_systems, transport_number, validate_systems,
)
from src.personal_renovations_contract import RENOVATIONS_PATH, RENOVATIONS_PATHS, ScenarioInputsV4

UTILITIES_PATH = 'scenario_inputs.utilities'
UTILITIES_PATHS = frozenset({UTILITIES_PATH, UTILITIES_PATH + '.electricity',
    UTILITIES_PATH + '.water', UTILITIES_PATH + '.electricity.grid_import_kwh',
    UTILITIES_PATH + '.electricity.calendar_year', UTILITIES_PATH + '.water.supplied_volume_m3',
    UTILITIES_PATH + '.water.calendar_year'})
READ_PATHS = PERSONAL_READ_PATHS | SYSTEMS_PATHS | RENOVATIONS_PATHS | UTILITIES_PATHS
ReadPathV5 = Literal[tuple(sorted(READ_PATHS))]
AvailabilityPathV5 = Literal[tuple(sorted(READ_PATHS | {'geometry', 'source_observed_at'}))]
UNSET_PATHS_V5 = UNSET_PATHS | NEW_PATHS | {SYSTEMS_PATH, RENOVATIONS_PATH, UTILITIES_PATH}


class VersionedV5(ContractModel):
    schema_version: Literal[5] = 5

    @field_validator('schema_version', mode='before')
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != 5:
            fail('schema_version', 'Expected schema version integer 5')
        return value


class AnnualObservation(ContractModel):
    calendar_year: int | None = Field(default=None, ge=1, le=9999,
        description='Complete January–December calendar year; newly submitted non-null years must precede the current UTC year. Optional, independent of amount; no inferred year.')

    @field_validator('calendar_year')
    @classmethod
    def completed_year(cls, value, info: ValidationInfo):
        year = (info.context or {}).get('current_year', datetime.now(timezone.utc).year)
        if not (info.context or {}).get('stored_document') and value is not None and value >= year:
            fail('calendar_year', 'Observation year must precede the current UTC year')
        return value


Quantity = Annotated[float, Field(ge=0, allow_inf_nan=False), BeforeValidator(transport_number)]


class ElectricityV5(AnnualObservation):
    grid_import_kwh: Quantity | None = Field(default=None,
        description='Self-reported measured grid import for the whole building over one complete calendar year, kWh; all supplied uses, excluding direct self-generation and exports. Zero retained, null unknown; no source prefill.')


class WaterV5(AnnualObservation):
    supplied_volume_m3: Quantity | None = Field(default=None,
        description='Self-reported measured total supplied consumption for the whole building over one complete calendar year, m³ counted once before hot/cold distribution; excludes recirculation, wastewater and heating energy. Zero retained, null unknown; no source prefill.')


class UtilitiesV5(ContractModel):
    electricity: ElectricityV5 | None = None
    water: WaterV5 | None = None


class ScenarioInputsV5(ScenarioInputsV4):
    utilities: UtilitiesV5 | None = None


class PersonalBuildingPutV5(PersonalBuildingPutV1):
    schema_version: Literal[5]
    scenario_inputs: ScenarioInputsV5 = Field(default_factory=ScenarioInputsV5)

    @field_validator('schema_version', mode='before')
    @classmethod
    def exact_version(cls, value):
        return VersionedV5.exact_version(value)


class PatchSetV5(ContractModel):
    overrides: BuildingOverridesV1 = Field(default_factory=BuildingOverridesV1)
    scenario_inputs: ScenarioInputsV5 = Field(default_factory=ScenarioInputsV5)


class PersonalBuildingPatchV5(PersonalBuildingPatchV1):
    schema_version: Literal[5]
    set: PatchSetV5 = Field(default_factory=PatchSetV5)
    unset: list[Literal[tuple(sorted(UNSET_PATHS_V5))]] = Field(
        default_factory=list, max_length=len(UNSET_PATHS_V5))

    @field_validator('schema_version', mode='before')
    @classmethod
    def exact_version(cls, value):
        return VersionedV5.exact_version(value)


def replace_v5(value, catalog):
    validate_new_codes(value.overrides, catalog)
    validate_systems(value.scenario_inputs.systems, replacement=True)
    return PersonalBuildingPutV5.model_validate(personal_document(value), context={'stored_document': True})


def patch_v5(existing, patch, catalog):
    validate_new_codes(patch.set.overrides, catalog, prefix=('set', 'overrides'))
    if 'systems' in patch.set.scenario_inputs.model_fields_set:
        validate_systems(patch.set.scenario_inputs.systems, getattr(existing.scenario_inputs, 'systems', None),
                         prefix=('set', 'scenario_inputs', 'systems'))
    document = personal_document(existing)
    for namespace in ('overrides', 'scenario_inputs'):
        document[namespace].update(sparse(getattr(patch.set, namespace)))
    for path in patch.unset:
        namespace, key = path.split('.')
        document[namespace].pop(key, None)
    if 'display_name' in patch.model_fields_set:
        document['display_name'] = patch.display_name
    check_payload_size(document)
    return PersonalBuildingPutV5.model_validate(document, context={'stored_document': True})


class EffectiveV5(EffectiveV1):
    scenario_inputs: ScenarioInputsV5


class PersonalBuildingProjectionV5(PersonalBuildingProjectionV1):
    schema_version: Literal[5] = 5
    document_schema_version: Literal[5]
    scenario_inputs: ScenarioInputsV5
    effective: EffectiveV5
    input_status: InputStatusV3
    systems_defaults: DefaultResolutionV3
    systems_source_provenance: list[HeatingSourceProvenanceV3] = Field(max_length=16)
    field_provenance: dict[ReadPathV5, FieldProvenanceV1]
    source_availability: dict[AvailabilityPathV5, AvailabilityV1]

    @field_validator('schema_version', mode='before')
    @classmethod
    def exact_version(cls, value):
        return VersionedV5.exact_version(value)


def input_provenance(scenario, path):
    current = scenario
    parts = path.split('.')[1:]
    for index, part in enumerate(parts):
        if part not in current.model_fields_set:
            return FieldProvenanceV1(origin='unavailable', status='missing', reason='no_personal_default')
        current = getattr(current, part)
        if current is None:
            source = 'scenario_inputs.' + '.'.join(parts[:index + 1])
            return FieldProvenanceV1(origin='user', source=source, status='user_unknown',
                is_override=True, dependencies=[source] if source != path else [])
    return FieldProvenanceV1(origin='user', source=path, status='available', is_override=True)


def project_v5(base, document: PersonalBuildingPutV5, **kwargs):
    scenario = document.scenario_inputs
    projection = project_building(base, document.overrides, scenario, **kwargs)
    provenance = dict(projection.field_provenance)
    provenance.update({path: input_provenance(scenario, path)
        for path in sorted(NEW_PATHS | SYSTEMS_PATHS | RENOVATIONS_PATHS | UTILITIES_PATHS)})
    provenance['display_name'] = (
        FieldProvenanceV1(origin='user', source='display_name', status='available', is_override=True)
        if document.display_name is not None else
        FieldProvenanceV1(origin='unavailable', status='missing', reason='no_personal_default')
    )
    statuses, defaults, sources = project_systems(scenario.systems, provenance, property_input_status(projection))
    availability = dict(projection.source_availability)
    availability.update({path: AvailabilityV1(status=value.status, reason=value.reason)
                         for path, value in provenance.items()})
    return PersonalBuildingProjectionV5(
        building_key=projection.building_key, base_status=projection.base_status,
        base_unavailable_reason=projection.base_unavailable_reason, base=projection.base,
        overrides=projection.overrides, scenario_inputs=scenario, document_schema_version=document.schema_version,
        effective=EffectiveV5(**{name: getattr(projection.effective, name) for name in EffectiveV1.model_fields}),
        source_observed_at=projection.source_observed_at, last_known_summary=projection.last_known_summary,
        field_provenance=provenance, source_availability=availability,
        input_status=statuses, systems_defaults=defaults, systems_source_provenance=sources,
    )
