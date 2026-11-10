"""Closed personal utilities v5; no source or calculation effects."""
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, ValidationInfo, field_validator

from src.building_contract import (AvailabilityV1, BuildingOverridesV1, ContractModel,
    FieldProvenanceV1, PersonalBuildingPatchV1, check_payload_size,
    fail, personal_document, sparse, validate_new_codes)
from src.personal_renovations_contract import (ScenarioInputsV4, PersonalBuildingPutV4,
    PersonalBuildingProjectionV4, EffectiveV4, READ_PATHS as V4_READ_PATHS,
    UNSET_PATHS_V4, project_v4)
from src.personal_hvac_contract import validate_systems, transport_number

UTILITIES_PATH = 'scenario_inputs.utilities'
UTILITIES_PATHS = frozenset({UTILITIES_PATH, UTILITIES_PATH + '.electricity',
    UTILITIES_PATH + '.water', UTILITIES_PATH + '.electricity.grid_import_kwh',
    UTILITIES_PATH + '.electricity.calendar_year', UTILITIES_PATH + '.water.supplied_volume_m3',
    UTILITIES_PATH + '.water.calendar_year'})
READ_PATHS = V4_READ_PATHS | UTILITIES_PATHS
ReadPathV5 = Literal[tuple(sorted(READ_PATHS))]
AvailabilityPathV5 = Literal[tuple(sorted(READ_PATHS | {'geometry', 'source_observed_at'}))]
UNSET_PATHS_V5 = UNSET_PATHS_V4 | {UTILITIES_PATH}


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


class PersonalBuildingPutV5(PersonalBuildingPutV4):
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
    document['schema_version'] = 5
    for namespace in ('overrides', 'scenario_inputs'):
        document[namespace].update(sparse(getattr(patch.set, namespace)))
    for path in patch.unset:
        namespace, key = path.split('.')
        document[namespace].pop(key, None)
    if 'display_name' in patch.model_fields_set:
        document['display_name'] = patch.display_name
    check_payload_size(document)
    return PersonalBuildingPutV5.model_validate(document, context={'stored_document': True})


class EffectiveV5(EffectiveV4):
    scenario_inputs: ScenarioInputsV5


class PersonalBuildingProjectionV5(PersonalBuildingProjectionV4):
    schema_version: Literal[5] = 5
    document_schema_version: Literal[1, 2, 3, 4, 5]
    scenario_inputs: ScenarioInputsV5
    effective: EffectiveV5
    field_provenance: dict[ReadPathV5, FieldProvenanceV1]
    source_availability: dict[AvailabilityPathV5, AvailabilityV1]

    @field_validator('schema_version', mode='before')
    @classmethod
    def exact_version(cls, value):
        return VersionedV5.exact_version(value)


def project_v5(base, document, **kwargs):
    # Temporary read projection only: a closed v4 model must never see U.
    old = personal_document(document)
    old['schema_version'] = 4
    old['scenario_inputs'].pop('utilities', None)
    legacy = PersonalBuildingPutV4.model_validate(old, context={'stored_document': True})
    projection = project_v4(base, legacy, **kwargs)
    scenario = ScenarioInputsV5.model_validate(sparse(document.scenario_inputs), context={'stored_document': True})
    provenance = dict(projection.field_provenance)
    for path in sorted(UTILITIES_PATHS):
        current = scenario
        parts = path.split('.')[1:]
        value = FieldProvenanceV1(origin='unavailable', status='missing', reason='no_personal_default')
        for index, part in enumerate(parts):
            if part not in current.model_fields_set:
                break
            current = getattr(current, part)
            if current is None:
                ancestor = 'scenario_inputs.' + '.'.join(parts[:index + 1])
                value = FieldProvenanceV1(origin='user', source=ancestor, status='user_unknown',
                    is_override=True, dependencies=[ancestor] if ancestor != path else [])
                break
        else:
            value = FieldProvenanceV1(origin='user', source=path, status='available', is_override=True)
        provenance[path] = value
    availability = dict(projection.source_availability)
    availability.update({p: AvailabilityV1(status=provenance[p].status, reason=provenance[p].reason)
                         for p in sorted(UTILITIES_PATHS)})
    values = {k: getattr(projection, k) for k in type(projection).model_fields}
    values.update(schema_version=5, document_schema_version=document.schema_version,
        scenario_inputs=scenario, field_provenance=provenance, source_availability=availability,
        effective=EffectiveV5(**{k: scenario if k == 'scenario_inputs' else getattr(projection.effective, k)
                                 for k in EffectiveV4.model_fields}))
    return PersonalBuildingProjectionV5(**values)
