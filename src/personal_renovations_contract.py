"""Closed personal completed-work v4; no source or calculation effects."""
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, ValidationInfo, field_validator, model_validator

from src.building_contract import (AvailabilityV1, BuildingOverridesV1, ContractModel,
    FieldProvenanceV1, PersonalBuildingPatchV1, UNSET_PATHS, check_payload_size,
    fail, issue, personal_document, sparse, validate_new_codes)
from src.personal_property_contract import NEW_PATHS, PERSONAL_READ_PATHS
from src.personal_hvac_contract import (ScenarioInputsV3, PersonalBuildingPutV3,
    PersonalBuildingProjectionV3, EffectiveV3, SYSTEMS_PATH, SYSTEMS_PATHS,
    project_v3, structural_errors, validate_systems, transport_number)

RENOVATIONS_PATH = 'scenario_inputs.renovations'
CATEGORIES = ('facade', 'windows', 'plumbing', 'roof', 'top_floor_insulation',
              'drainage', 'energy_smart_solutions', 'energy_storage')
RENOVATIONS_PATHS = frozenset({RENOVATIONS_PATH,
    *(RENOVATIONS_PATH + '.' + k for k in CATEGORIES),
    *(RENOVATIONS_PATH + '.' + k + '.completed' for k in CATEGORIES),
    RENOVATIONS_PATH + '.top_floor_insulation.execution_year',
    RENOVATIONS_PATH + '.top_floor_insulation.added_insulation_thickness_mm'})
READ_PATHS = PERSONAL_READ_PATHS | SYSTEMS_PATHS | RENOVATIONS_PATHS
ReadPathV4 = Literal[tuple(sorted(READ_PATHS))]
AvailabilityPathV4 = Literal[tuple(sorted(READ_PATHS | {'geometry', 'source_observed_at'}))]
UNSET_PATHS_V4 = UNSET_PATHS | NEW_PATHS | {SYSTEMS_PATH, RENOVATIONS_PATH}


class VersionedV4(ContractModel):
    schema_version: Literal[4] = 4

    @field_validator('schema_version', mode='before')
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != 4:
            fail('schema_version', 'Expected schema version integer 4')
        return value


class CompletedWorkV4(ContractModel):
    completed: bool | None = None


class TopFloorInsulationV4(CompletedWorkV4):
    model_config = {'json_schema_extra': {
        'if': {'anyOf': [{'required': ['execution_year']},
                          {'required': ['added_insulation_thickness_mm']}]},
        'then': {'required': ['completed'], 'properties': {'completed': {'const': True}}}}}
    execution_year: int | None = Field(default=None, ge=1, le=9999,
        description='Completed calendar year; newly submitted values must not exceed the current UTC year.')
    added_insulation_thickness_mm: Annotated[float, Field(ge=0, allow_inf_nan=False),
        BeforeValidator(transport_number)] | None = Field(default=None,
        description='Insulation layer added by this completed renovation, in mm; zero retained, null unknown.')

    @field_validator('execution_year')
    @classmethod
    def completed_year(cls, value, info: ValidationInfo):
        year = (info.context or {}).get('current_year', datetime.now(timezone.utc).year)
        if not (info.context or {}).get('stored_document') and value is not None and value > year:
            fail('execution_year', 'Completed work year exceeds the current UTC year')
        return value

    @model_validator(mode='after')
    def applicable(self):
        structural_errors(type(self).__name__, [issue((field,), 'renovation_detail_not_applicable',
            'Details require an affirmative completed answer')
            for field in ('execution_year', 'added_insulation_thickness_mm')
            if self.completed is not True and field in self.model_fields_set])
        return self


class RenovationsV4(ContractModel):
    facade: CompletedWorkV4 | None = None
    windows: CompletedWorkV4 | None = None
    plumbing: CompletedWorkV4 | None = None
    roof: CompletedWorkV4 | None = None
    top_floor_insulation: TopFloorInsulationV4 | None = None
    drainage: CompletedWorkV4 | None = None
    energy_smart_solutions: CompletedWorkV4 | None = None
    energy_storage: CompletedWorkV4 | None = None


class ScenarioInputsV4(ScenarioInputsV3):
    renovations: RenovationsV4 | None = None


class PersonalBuildingPutV4(PersonalBuildingPutV3):
    schema_version: Literal[4]
    scenario_inputs: ScenarioInputsV4 = Field(default_factory=ScenarioInputsV4)

    @field_validator('schema_version', mode='before')
    @classmethod
    def exact_version(cls, value):
        return VersionedV4.exact_version(value)


class PatchSetV4(ContractModel):
    overrides: BuildingOverridesV1 = Field(default_factory=BuildingOverridesV1)
    scenario_inputs: ScenarioInputsV4 = Field(default_factory=ScenarioInputsV4)


class PersonalBuildingPatchV4(PersonalBuildingPatchV1):
    schema_version: Literal[4]
    set: PatchSetV4 = Field(default_factory=PatchSetV4)
    unset: list[Literal[tuple(sorted(UNSET_PATHS_V4))]] = Field(
        default_factory=list, max_length=len(UNSET_PATHS_V4))

    @field_validator('schema_version', mode='before')
    @classmethod
    def exact_version(cls, value):
        return VersionedV4.exact_version(value)


def replace_v4(value, catalog):
    validate_new_codes(value.overrides, catalog)
    validate_systems(value.scenario_inputs.systems, replacement=True)
    return PersonalBuildingPutV4.model_validate(personal_document(value), context={'stored_document': True})


def patch_v4(existing, patch, catalog):
    validate_new_codes(patch.set.overrides, catalog, prefix=('set', 'overrides'))
    if 'systems' in patch.set.scenario_inputs.model_fields_set:
        validate_systems(patch.set.scenario_inputs.systems, getattr(existing.scenario_inputs, 'systems', None),
                         prefix=('set', 'scenario_inputs', 'systems'))
    document = personal_document(existing)
    document['schema_version'] = 4
    for namespace in ('overrides', 'scenario_inputs'):
        document[namespace].update(sparse(getattr(patch.set, namespace)))
    for path in patch.unset:
        namespace, key = path.split('.')
        document[namespace].pop(key, None)
    if 'display_name' in patch.model_fields_set:
        document['display_name'] = patch.display_name
    check_payload_size(document)
    return PersonalBuildingPutV4.model_validate(document, context={'stored_document': True})


class EffectiveV4(EffectiveV3):
    scenario_inputs: ScenarioInputsV4


class PersonalBuildingProjectionV4(PersonalBuildingProjectionV3):
    schema_version: Literal[4] = 4
    document_schema_version: Literal[1, 2, 3, 4]
    scenario_inputs: ScenarioInputsV4
    effective: EffectiveV4
    field_provenance: dict[ReadPathV4, FieldProvenanceV1]
    source_availability: dict[AvailabilityPathV4, AvailabilityV1]

    @field_validator('schema_version', mode='before')
    @classmethod
    def exact_version(cls, value):
        return VersionedV4.exact_version(value)


def project_v4(base, document, **kwargs):
    # Temporary read projection only: a closed v3 model must never see R.
    old = personal_document(document)
    old['schema_version'] = 3
    old['scenario_inputs'].pop('renovations', None)
    legacy = PersonalBuildingPutV3.model_validate(old, context={'stored_document': True})
    projection = project_v3(base, legacy, **kwargs)
    scenario = ScenarioInputsV4.model_validate(sparse(document.scenario_inputs), context={'stored_document': True})
    provenance = dict(projection.field_provenance)
    for path in sorted(RENOVATIONS_PATHS):
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
                         for p in sorted(RENOVATIONS_PATHS)})
    values = {k: getattr(projection, k) for k in type(projection).model_fields}
    values.update(schema_version=4, document_schema_version=document.schema_version,
        scenario_inputs=scenario, field_provenance=provenance, source_availability=availability,
        effective=EffectiveV4(**{k: scenario if k == 'scenario_inputs' else getattr(projection.effective, k)
                                 for k in EffectiveV3.model_fields}))
    return PersonalBuildingProjectionV4(**values)
