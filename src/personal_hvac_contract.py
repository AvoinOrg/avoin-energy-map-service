"""Closed personal HVAC inputs; source data and scientific defaults stay separate."""
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, ValidationError, field_validator, model_validator
from pydantic_core import PydanticCustomError

from src.building_contract import (
    ContractModel, ContractValidationError, FieldProvenanceV1, fail, issue, numeric,
)
from src.personal_property_contract import PropertyInputStatusV2, ScenarioInputsV2
from src.hvac_catalog import (CAPABILITY_VERSION, DeferredCoefficientsV2, HvacCatalogUnavailable, load_hvac_catalog)

SYSTEMS_PATH = 'scenario_inputs.systems'
QUANTITY_FIELDS = ('total_area_m2', 'total_volume_m3', 'residential_area_m2', 'residential_volume_m3')
EQUIPMENT_FIELDS = ('geothermal_installed', 'solar_panels_installed', 'energy_storage_ready')
SYSTEMS_SUFFIXES = ('', '.heating', '.heating.sources', '.heating.primary_source_id',
    '.heated_quantities', '.cooling', '.ventilation', '.equipment',
    *('.heated_quantities.' + k for k in QUANTITY_FIELDS), *('.equipment.' + k for k in EQUIPMENT_FIELDS))
SYSTEMS_PATHS = frozenset(SYSTEMS_PATH + k for k in SYSTEMS_SUFFIXES)
StatusPathV3 = Literal['details.floor_area', 'details.heating_method']
DefaultsVersionV3 = Literal['hvac-defaults-2026-10-30.1', 'hvac-inputs-2026-10-31.1']
ChoiceCodeV3 = Annotated[str, Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_:-]+$')]
SourceIdV3 = Annotated[str, Field(pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    json_schema_extra={'format': 'uuid'})]


def transport_number(value):
    numeric(value)
    converted = float(value)
    if type(value) is int and converted != value:
        fail('number_precision', 'Integer must be exactly representable as a binary64 JSON number')
    return converted


UserNumberV3 = Annotated[float, BeforeValidator(transport_number), Field(allow_inf_nan=False)]


def structural_errors(model, errors):
    if errors:
        raise ValidationError.from_exception_data(model, [
            {'type': PydanticCustomError(e['type'], e['msg']), 'loc': tuple(e['loc']), 'input': None}
            for e in errors])


def duplicate_errors(values, prefix, code='duplicate_choice'):
    seen, errors = set(), []
    for i, value in enumerate(values):
        if value in seen:
            errors.append(issue((*prefix, i), code, 'Collection members must be unique'))
        seen.add(value)
    return errors


class NoVentilationV3(ContractModel):
    kind: Literal['none']


class SelectedVentilationV3(ContractModel):
    kind: Literal['selected']
    # Structurally retain historical codes on read. Current input uses the pin.
    code: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')


class MultipleVentilationV3(ContractModel):
    kind: Literal['multiple']
    codes: list[ChoiceCodeV3] = Field(min_length=2, max_length=9)

    @model_validator(mode='after')
    def unique(self):
        structural_errors(type(self).__name__, duplicate_errors(self.codes, ('codes',)))
        return self


class NoCoolingV3(ContractModel):
    kind: Literal['none']


class SelectedCoolingV3(ContractModel):
    kind: Literal['selected']
    codes: list[ChoiceCodeV3] = Field(min_length=1, max_length=14)

    @model_validator(mode='after')
    def unique(self):
        structural_errors(type(self).__name__, duplicate_errors(self.codes, ('codes',)))
        return self


class HeatingSourceV3(ContractModel):
    source_id: SourceIdV3
    source_code: ChoiceCodeV3
    share_percent: UserNumberV3 | None = None


class HeatingV3(ContractModel):
    sources: list[HeatingSourceV3] = Field(max_length=16)
    primary_source_id: SourceIdV3 | None = None

    @model_validator(mode='after')
    def references(self):
        errors = []
        ids, codes = set(), set()
        for i, source in enumerate(self.sources):
            for key, seen, code in (('source_id', ids, 'duplicate_source_id'),
                                    ('source_code', codes, 'duplicate_source_code')):
                value = getattr(source, key)
                if value in seen:
                    errors.append(issue(('sources', i, key), code, 'Source identities and choices must be unique'))
                seen.add(value)
        if self.primary_source_id is not None and self.primary_source_id not in ids:
            errors.append(issue(('primary_source_id',), 'dangling_primary_source', 'Primary must refer to a present source'))
        structural_errors(type(self).__name__, errors)
        return self


class HeatedQuantitiesV3(ContractModel):
    total_area_m2: UserNumberV3 | None = None
    total_volume_m3: UserNumberV3 | None = None
    residential_area_m2: UserNumberV3 | None = None
    residential_volume_m3: UserNumberV3 | None = None


class EquipmentV3(ContractModel):
    geothermal_installed: bool | None = None
    solar_panels_installed: bool | None = None
    energy_storage_ready: bool | None = None


class SystemsV3(ContractModel):
    defaults_version: DefaultsVersionV3
    heating: HeatingV3 | None = None
    heated_quantities: HeatedQuantitiesV3 | None = None
    cooling: Annotated[NoCoolingV3 | SelectedCoolingV3, Field(discriminator='kind')] | None = None
    ventilation: Annotated[NoVentilationV3 | SelectedVentilationV3 | MultipleVentilationV3, Field(discriminator='kind')] | None = None
    equipment: EquipmentV3 | None = None


class ScenarioInputsV3(ScenarioInputsV2):
    systems: SystemsV3 | None = None


def selected_codes(systems):
    """Code validation locations are the submitted snapshot, never stored indexes."""
    if systems is None:
        return []
    result = []
    if systems.heating is not None:
        result.extend(('heating', s.source_id, s.source_code, ('heating', 'sources', i, 'source_code'))
                      for i, s in enumerate(systems.heating.sources))
    if isinstance(systems.cooling, SelectedCoolingV3):
        result.extend(('cooling', code, code, ('cooling', 'codes', i)) for i, code in enumerate(systems.cooling.codes))
    if isinstance(systems.ventilation, SelectedVentilationV3):
        result.append(('ventilation', systems.ventilation.code, systems.ventilation.code, ('ventilation', 'code')))
    elif isinstance(systems.ventilation, MultipleVentilationV3):
        result.extend(('ventilation', code, code, ('ventilation', 'codes', i)) for i, code in enumerate(systems.ventilation.codes))
    return result


def validate_source_identity(new, old=None, *, prefix=('scenario_inputs', 'systems')):
    if new is None or new.heating is None or old is None or old.heating is None:
        return
    previous = {s.source_id: s.source_code for s in old.heating.sources}
    errors = [issue((*prefix, 'heating', 'sources', i, 'source_code'), 'source_identity_reused',
                    'A replacement choice requires a new source identity')
              for i, s in enumerate(new.heating.sources) if s.source_id in previous and previous[s.source_id] != s.source_code]
    if errors:
        raise ContractValidationError(errors)


def validate_systems(new, old=None, *, prefix=('scenario_inputs', 'systems'), replacement=False):
    validate_source_identity(new, old, prefix=prefix)
    previous = {(group, identity, code) for group, identity, code, _ in selected_codes(old)}
    changed = [s for s in selected_codes(new) if replacement or s[:3] not in previous]
    if not changed:
        return  # Empty/numeric/equipment/unchanged-choice drafts require no catalog.
    catalog = load_hvac_catalog()
    choices = {name: {c.code for c in getattr(catalog, name + '_choices') if c.selectable}
               for name in ('heating', 'cooling', 'ventilation')}
    errors = [issue((*prefix, *location), 'unsupported_hvac_choice', 'Choose a supported HVAC code')
              for group, _, code, location in changed if code not in choices[group]]
    if errors:
        raise ContractValidationError(errors)


class SystemsInputStatusV3(ContractModel):
    policy_version: Literal['property-required-inputs-2026-10-29'] = 'property-required-inputs-2026-10-29'
    saveable: Literal[True] = True
    completeness: Literal['complete', 'incomplete', 'unavailable']
    missing_paths: list[StatusPathV3]
    unavailable_paths: list[StatusPathV3]


class InputStatusV3(ContractModel):
    property: PropertyInputStatusV2
    systems: SystemsInputStatusV3


class DefaultResolutionV3(ContractModel):
    defaults_version: DefaultsVersionV3 | None
    status: Literal['not_requested', 'pinned_no_scientific_defaults', 'pin_unavailable']
    capability_version: Literal['hvac-inputs-2026-10-31.1'] = CAPABILITY_VERSION
    coefficients: DeferredCoefficientsV2 = Field(default_factory=DeferredCoefficientsV2)


class HeatingSourceProvenanceV3(ContractModel):
    source_id: SourceIdV3
    source_code: FieldProvenanceV1
    share_percent: FieldProvenanceV1


def project_systems(systems, provenance, property_status):
    """Resolve selected HVAC choices and report the input status for this building."""
    default_status = 'not_requested'
    catalog = None
    if systems is not None:
        default_status = 'pinned_no_scientific_defaults'
        if selected_codes(systems):
            try:
                catalog = load_hvac_catalog()
            except HvacCatalogUnavailable:
                default_status = 'pin_unavailable'

    selected = selected_codes(systems)
    available = {name: {c.code for c in getattr(catalog, name + '_choices') if c.selectable}
                 for name in ('heating', 'cooling', 'ventilation')} if catalog else {}

    def choice_provenance(value, group, code):
        if catalog is None:
            return value.model_copy(update={'status': 'source_unavailable', 'reason': 'hvac_catalog_unavailable'})
        if code not in available[group]:
            return value.model_copy(update={'status': 'unsupported', 'reason': 'historical_hvac_choice'})
        return value

    for group, _, code, _ in selected:
        path = SYSTEMS_PATH + ('.heating.sources' if group == 'heating' else '.' + group)
        # Preserve the most informative failure if a multi-selection has several.
        if provenance[path].status == 'available':
            provenance[path] = choice_provenance(provenance[path], group, code)
    source_provenance = []
    if systems is not None and systems.heating is not None:
        for source in systems.heating.sources:
            # Container path is accompanied by stable source_id in the typed DTO;
            # no request index or arbitrary dynamic provenance key is persisted.
            path = SYSTEMS_PATH + '.heating.sources'
            code_p = FieldProvenanceV1(origin='user', source=path, status='available', is_override=True)
            share_p = FieldProvenanceV1(origin='user' if 'share_percent' in source.model_fields_set else 'unavailable',
                source=path if 'share_percent' in source.model_fields_set else None,
                status=('user_unknown' if source.share_percent is None else 'available') if 'share_percent' in source.model_fields_set else 'missing',
                reason=None if 'share_percent' in source.model_fields_set else 'no_personal_default',
                is_override='share_percent' in source.model_fields_set)
            source_provenance.append(HeatingSourceProvenanceV3(source_id=source.source_id,
                source_code=choice_provenance(code_p, 'heating', source.source_code), share_percent=share_p))
    mandatory = {'details.floor_area', 'details.heating_method'}
    missing = [p for p in property_status.missing_paths if p in mandatory]
    unavailable = [p for p in property_status.unavailable_paths if p in mandatory]
    statuses = InputStatusV3(property=property_status, systems=SystemsInputStatusV3(
        completeness='unavailable' if unavailable else 'incomplete' if missing else 'complete',
        missing_paths=missing, unavailable_paths=unavailable))
    defaults = DefaultResolutionV3(defaults_version=systems.defaults_version if systems else None, status=default_status)
    return statuses, defaults, source_provenance
