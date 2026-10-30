"""Closed personal v2 document; canonical and v1 DTOs remain unchanged."""
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import Field, ValidationInfo, field_validator

from src.building_contract import (
    AvailabilityV1, BuildingOverridesV1, ContractModel, EffectiveV1,
    DetailsV1, EnergyClass, FieldProvenanceV1, PersonalBuildingPatchV1, PersonalBuildingProjectionV1,
    PersonalBuildingPutV1, ScenarioInputsV1, UNSET_PATHS, check_payload_size,
    fail, personal_document, project_building, sparse, validate_new_codes,
)

APARTMENT_FIELDS = ('apartment_studio_count', 'apartment_two_room_count',
                    'apartment_three_room_count', 'apartment_four_plus_room_count')
PROPERTY_FIELDS = (*APARTMENT_FIELDS, 'personal_energy_class', 'personal_energy_class_date',
                   'annual_maintenance_charge_eur_cents')
NEW_PATHS = frozenset('scenario_inputs.' + name for name in PROPERTY_FIELDS)
PERSONAL_READ_PATHS = NEW_PATHS | {'display_name', 'scenario_inputs.resident_count'} | {f'details.{k}' for k in DetailsV1.model_fields} | {f'identity.{k}' for k in ('permanent_building_identifier', 'property_identifier', 'address_key')}
ReadPathV2 = Literal[tuple(sorted(PERSONAL_READ_PATHS))]
AvailabilityPathV2 = Literal[tuple(sorted(PERSONAL_READ_PATHS | {'geometry', 'source_observed_at'}))]


class VersionedV2(ContractModel):
    schema_version: Literal[2] = 2

    @field_validator('schema_version', mode='before')
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != 2:
            fail('schema_version', 'Expected schema version integer 2')
        return value


Count = Annotated[int, Field(ge=0, le=1_000_000)]


class PersonalEnergyClassV2(ContractModel):
    grade: EnergyClass
    scale: Literal[2013, 2018]

    @field_validator('scale', mode='before')
    @classmethod
    def exact_scale(cls, value):
        if type(value) is not int or value not in (2013, 2018):
            fail('personal_class_scale', 'Expected assessment scale integer 2013 or 2018')
        return value


class PersonalEstimateYearV2(ContractModel):
    precision: Literal['year']
    year: Annotated[int, Field(ge=1, le=9999)]

    @field_validator('year')
    @classmethod
    def new_year_bound(cls, value, info: ValidationInfo):
        context = info.context or {}
        # A current-input proposal must not invalidate saved historical answers.
        if not context.get('stored_document'):
            year = context.get('current_year', datetime.now(timezone.utc).year)
            if value > year:
                fail('personal_estimate_year', 'Estimate year exceeds the current UTC year')
        return value


class ScenarioInputsV2(ScenarioInputsV1):
    personal_energy_class: PersonalEnergyClassV2 | None = None
    personal_energy_class_date: PersonalEstimateYearV2 | None = None
    annual_maintenance_charge_eur_cents: Annotated[int, Field(ge=0, le=99_999_999_999)] | None = None
    apartment_studio_count: Count | None = None
    apartment_two_room_count: Count | None = None
    apartment_three_room_count: Count | None = None
    apartment_four_plus_room_count: Count | None = None


class PersonalBuildingPutV2(PersonalBuildingPutV1):
    schema_version: Literal[2]
    scenario_inputs: ScenarioInputsV2 = Field(default_factory=ScenarioInputsV2)
    @field_validator('schema_version', mode='before')
    @classmethod
    def exact_version(cls, value):
        return VersionedV2.exact_version(value)


class PatchSetV2(ContractModel):
    overrides: BuildingOverridesV1 = Field(default_factory=BuildingOverridesV1)
    scenario_inputs: ScenarioInputsV2 = Field(default_factory=ScenarioInputsV2)


class PersonalBuildingPatchV2(PersonalBuildingPatchV1):
    schema_version: Literal[2]
    set: PatchSetV2 = Field(default_factory=PatchSetV2)
    unset: list[Literal[tuple(sorted(UNSET_PATHS | NEW_PATHS))]] = Field(
        default_factory=list, max_length=len(UNSET_PATHS | NEW_PATHS))
    @field_validator('schema_version', mode='before')
    @classmethod
    def exact_version(cls, value):
        return VersionedV2.exact_version(value)


REQUIRED_PROPERTY_PATHS = ('details.main_purpose', 'details.construction_year',
                           'details.floor_area', 'details.heating_method')
RequiredPropertyPathV2 = Literal[REQUIRED_PROPERTY_PATHS]


class PropertyInputStatusV2(ContractModel):
    policy_version: Literal['property-required-inputs-2026-10-29'] = 'property-required-inputs-2026-10-29'
    saveable: Literal[True] = True
    completeness: Literal['complete', 'incomplete', 'unavailable']
    missing_paths: list[RequiredPropertyPathV2]
    unavailable_paths: list[RequiredPropertyPathV2]


def property_input_status(projection):
    from src.purpose_catalog import PurposeCatalogUnavailable, load_purpose_catalog

    missing, unavailable = [], []
    for path in REQUIRED_PROPERTY_PATHS:
        provenance = projection.field_provenance.get(path)
        value = getattr(projection.effective.details, path.split('.')[1])
        if provenance is None or provenance.status in ('source_unavailable', 'not_projected', 'invalid', 'unsupported'):
            unavailable.append(path)
        elif value is None:
            missing.append(path)
        elif path == 'details.main_purpose':
            try:
                catalog = load_purpose_catalog()
                if value not in {c.code for c in catalog.choices if c.selectable}:
                    unavailable.append(path)
            except PurposeCatalogUnavailable:
                unavailable.append(path)
    return PropertyInputStatusV2(completeness='unavailable' if unavailable else 'incomplete' if missing else 'complete',
                                 missing_paths=missing, unavailable_paths=unavailable)


class InputStatusV2(ContractModel):
    property: PropertyInputStatusV2


class EffectiveV2(EffectiveV1):
    scenario_inputs: ScenarioInputsV2


class PersonalBuildingProjectionV2(PersonalBuildingProjectionV1):
    schema_version: Literal[2] = 2
    document_schema_version: Literal[1, 2]
    scenario_inputs: ScenarioInputsV2
    effective: EffectiveV2
    field_provenance: dict[ReadPathV2, FieldProvenanceV1]
    source_availability: dict[AvailabilityPathV2, AvailabilityV1]
    input_status: InputStatusV2
    @field_validator('schema_version', mode='before')
    @classmethod
    def exact_version(cls, value):
        return VersionedV2.exact_version(value)


def v1_document(value):
    """Lossy read-only projection, never a write/downgrade primitive."""
    return PersonalBuildingPutV1.model_validate({
        'display_name': value.display_name, 'overrides': sparse(value.overrides),
        'scenario_inputs': {k: v for k, v in sparse(value.scenario_inputs).items()
                            if k in ScenarioInputsV1.model_fields}})


def replace_v2(value, catalog):
    validate_new_codes(value.overrides, catalog)
    return PersonalBuildingPutV2.model_validate(personal_document(value))


def patch_v2(existing, patch, catalog):
    validate_new_codes(patch.set.overrides, catalog, prefix=('set', 'overrides'))
    document = personal_document(existing)
    document['schema_version'] = 2
    for namespace in ('overrides', 'scenario_inputs'):
        document[namespace].update(sparse(getattr(patch.set, namespace)))
    for path in patch.unset:
        namespace, key = path.split('.')
        document[namespace].pop(key, None)
    if 'display_name' in patch.model_fields_set:
        document['display_name'] = patch.display_name
    check_payload_size(document)
    return PersonalBuildingPutV2.model_validate(document, context={'stored_document': True})


def project_v2(base, document, **kwargs):
    old = v1_document(document)
    projection = project_building(base, old.overrides, old.scenario_inputs, **kwargs)
    provenance = dict(projection.field_provenance)
    for path in sorted(NEW_PATHS | {'display_name'}):
        name = path.split('.')[-1]
        present = document.display_name is not None if path == 'display_name' else name in document.scenario_inputs.model_fields_set
        value = document.display_name if path == 'display_name' else getattr(document.scenario_inputs, name, None)
        provenance[path] = FieldProvenanceV1(origin='user' if present else 'unavailable',
            source=path if present else None, status=('user_unknown' if value is None else 'available') if present else 'missing',
            reason=None if present else 'no_personal_default', is_override=present)
    availability = dict(projection.source_availability)
    availability.update({k: AvailabilityV1(status=v.status, reason=v.reason) for k, v in provenance.items()})
    scenario = ScenarioInputsV2.model_validate(sparse(document.scenario_inputs), context={'stored_document': True})
    # Pass model instances to preserve strict date/UUID handling and sparse sets.
    values = {k: getattr(projection, k) for k in type(projection).model_fields}
    values.update(schema_version=2, document_schema_version=document.schema_version,
                  scenario_inputs=scenario, field_provenance=provenance, source_availability=availability,
                  input_status=InputStatusV2(property=property_input_status(projection)),
                  effective=EffectiveV2(**{k: scenario if k == 'scenario_inputs' else getattr(projection.effective, k)
                                         for k in EffectiveV1.model_fields}))
    return PersonalBuildingProjectionV2(**values)
