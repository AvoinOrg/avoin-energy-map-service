"""Personal property fields and completeness for saved buildings."""
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import Field, ValidationInfo, field_validator

from src.building_contract import ContractModel, DetailsV1, EnergyClass, ScenarioInputsV1, fail

APARTMENT_FIELDS = ('apartment_studio_count', 'apartment_two_room_count',
                    'apartment_three_room_count', 'apartment_four_plus_room_count')
PROPERTY_FIELDS = (*APARTMENT_FIELDS, 'personal_energy_class', 'personal_energy_class_date',
                   'annual_maintenance_charge_eur_cents')
NEW_PATHS = frozenset('scenario_inputs.' + name for name in PROPERTY_FIELDS)
PERSONAL_READ_PATHS = NEW_PATHS | {'display_name', 'scenario_inputs.resident_count'} | {f'details.{k}' for k in DetailsV1.model_fields} | {f'identity.{k}' for k in ('permanent_building_identifier', 'property_identifier', 'address_key')}
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
