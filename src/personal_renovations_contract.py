"""Completed renovation inputs for saved buildings."""
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, ValidationInfo, field_validator, model_validator

from src.building_contract import ContractModel, fail, issue
from src.personal_hvac_contract import ScenarioInputsV3, structural_errors, transport_number

RENOVATIONS_PATH = 'scenario_inputs.renovations'
CATEGORIES = ('facade', 'windows', 'plumbing', 'roof', 'top_floor_insulation',
              'drainage', 'energy_smart_solutions', 'energy_storage')
RENOVATIONS_PATHS = frozenset({RENOVATIONS_PATH,
    *(RENOVATIONS_PATH + '.' + k for k in CATEGORIES),
    *(RENOVATIONS_PATH + '.' + k + '.completed' for k in CATEGORIES),
    RENOVATIONS_PATH + '.top_floor_insulation.execution_year',
    RENOVATIONS_PATH + '.top_floor_insulation.added_insulation_thickness_mm'})
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
