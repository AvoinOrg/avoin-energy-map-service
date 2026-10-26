"""Version 1 building/public and personal-input contract.

No persistence or calculations live here. Sparse model field sets distinguish
omitted overrides from explicit null; callers must use personal_document().
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import math
import unicodedata
from typing import Annotated, Literal, Mapping

from pydantic import (
    AfterValidator, AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field,
    ValidationError, ValidationInfo, create_model, field_serializer,
    field_validator, model_validator,
)
from pydantic_core import PydanticCustomError

from src.geoserver.ventilation import VENTILATION_TYPES

PERSONAL_PAYLOAD_BYTES = 32 * 1024
SOURCE_TEXT_CHARS = 16_384
GEOMETRY_BYTES = 1024 * 1024
PREFIXES = ('awhp', 'delec', 'distr', 'elecb', 'gshp', 'oil', 'wood')
MEASURES = ('default', 'aahp', 'solar', 'windows')
QUANTITIES = ('total', 'heat', 'elec')
SCENARIO_COLUMNS = tuple(f'{p}_{m}_{q}' for p in PREFIXES for m in MEASURES for q in QUANTITIES)
CODE_FIELDS = ('main_purpose', 'heating_method', 'heating_energy_source')


def fail(code, message):
    raise PydanticCustomError(code, message)


def numeric(value):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        fail('finite_number', 'Expected a finite JSON number (booleans and strings are not numbers)')
    return value


def plain_text(value, *, multiline=False):
    if not isinstance(value, str):
        fail('string_type', 'Expected a string')
    if not value.strip():
        fail('blank_text', 'Text must not be blank; use null for unknown')
    if any(unicodedata.category(c) in ('Cc', 'Cs') and
           not (multiline and c in '\n\t') for c in value):
        fail('text_control', 'Text contains unsupported control characters')
    return value


def building_key(value):
    if not isinstance(value, str):
        fail('building_key', 'Expected an opaque building key')
    if not 1 <= len(value.encode('utf-8', errors='replace')) <= 256 or any(
        unicodedata.category(c) in ('Cc', 'Cs') for c in value
    ):
        fail('building_key', 'Building key must be 1–256 UTF-8 bytes without control characters')
    return value


BuildingKey = Annotated[str, BeforeValidator(building_key)]
UtcTime = Annotated[AwareDatetime, AfterValidator(lambda v: v.astimezone(timezone.utc))]
Quantity = Annotated[float, BeforeValidator(numeric), Field(ge=0, allow_inf_nan=False)]
EnergyClass = Literal['A', 'B', 'C', 'D', 'E', 'F', 'G']
Availability = Literal['available', 'missing', 'invalid', 'unsupported', 'not_projected',
                       'source_unavailable', 'user_unknown']
Origin = Literal['ryhti', 'certificate', 'model', 'user', 'derived', 'unavailable']


class ContractModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, validate_default=True)


class VersionedV1(ContractModel):
    schema_version: Literal[1] = 1

    @field_validator('schema_version', mode='before')
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != 1:
            fail('schema_version', 'Expected schema version integer 1')
        return value


@dataclass(frozen=True)
class EditableField:
    source: str
    kind: str
    maximum: int
    unit: str | None = None
    minimum: int = 0


# Central allowlist drives structural validation, reset paths and overlay.
EDITABLE_FIELDS = {
    'address_fin': EditableField('address_fin', 'text', 300),
    'postal_code': EditableField('postal_code', 'postal', 5),
    'postal_office_fin': EditableField('postal_office_fin', 'text', 150),
    'construction_year': EditableField('completion_date', 'year', 9999, 'year', 1),
    'main_purpose': EditableField('main_purpose', 'code', 16),
    'heating_method': EditableField('heating_method', 'code', 16),
    'heating_energy_source': EditableField('heating_energy_source', 'code', 16),
    'number_of_storeys': EditableField('number_of_storeys', 'int', 1_000, 'storeys'),
    'apartment_count': EditableField('apartment_count', 'int', 1_000_000, 'apartments'),
    'floor_area': EditableField('floor_area', 'number', 10_000_000, 'm²'),
    'total_area': EditableField('total_area', 'number', 10_000_000, 'm²'),
    'gross_floor_area': EditableField('gross_floor_area', 'number', 10_000_000, 'm²'),
    'heated_net_area': EditableField('heated_net_area', 'number', 10_000_000, 'm²'),
    'volume': EditableField('volume', 'number', 1_000_000_000, 'm³'),
    'ventilation_type_id': EditableField('ventilation_type_id', 'ventilation', 1),
    'ventilation_description': EditableField('ventilation_description', 'note', 4_000),
}


def input_type(definition):
    if definition.kind in ('int', 'year'):
        return Annotated[int, Field(ge=definition.minimum, le=definition.maximum)]
    if definition.kind == 'number':
        return Annotated[float, BeforeValidator(numeric), Field(ge=0, le=definition.maximum)]
    if definition.kind == 'ventilation':
        return Literal['0', '1', '2', '3', '4', '5', '6', '7', '8']
    if definition.kind == 'postal':
        return Annotated[str, Field(pattern=r'^[0-9]{5}$')]
    return Annotated[str, BeforeValidator(
        lambda v: plain_text(v, multiline=definition.kind == 'note')
    ), Field(max_length=definition.maximum)]


class OverrideRulesV1(ContractModel):
    @field_validator('construction_year', check_fields=False)
    @classmethod
    def year_bound(cls, value, info: ValidationInfo):
        year = (info.context or {}).get('current_year', datetime.now(timezone.utc).year)
        if value is not None and value > year + 5:
            fail('construction_year', 'Construction year exceeds the current UTC year plus five')
        return value


BuildingOverridesV1 = create_model('BuildingOverridesV1', __base__=OverrideRulesV1,
    **{k: (input_type(v) | None, None) for k, v in EDITABLE_FIELDS.items()})


class ScenarioInputsV1(ContractModel):
    resident_count: Annotated[int, Field(ge=1, le=10_000)] | None = None


DisplayName = Annotated[str, BeforeValidator(plain_text), Field(max_length=120)]
UNSET_PATHS = frozenset([f'overrides.{k}' for k in EDITABLE_FIELDS] + ['scenario_inputs.resident_count'])


def sparse(model):
    return model.model_dump(exclude_unset=True)


def check_payload_size(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    except (ValueError, UnicodeError, TypeError):
        fail('payload_encoding', 'Payload must be finite UTF-8 JSON')
    if len(encoded) > PERSONAL_PAYLOAD_BYTES:
        fail('payload_too_large', 'Personal payload exceeds 32 KiB UTF-8')


class PersonalBuildingPutV1(VersionedV1):
    display_name: DisplayName | None = None
    overrides: BuildingOverridesV1 = Field(default_factory=BuildingOverridesV1)
    scenario_inputs: ScenarioInputsV1 = Field(default_factory=ScenarioInputsV1)

    @model_validator(mode='after')
    def bounded(self):
        check_payload_size(personal_document(self))
        return self


class PatchSetV1(ContractModel):
    overrides: BuildingOverridesV1 = Field(default_factory=BuildingOverridesV1)
    scenario_inputs: ScenarioInputsV1 = Field(default_factory=ScenarioInputsV1)


class PersonalBuildingPatchV1(VersionedV1):
    display_name: DisplayName | None = None
    set: PatchSetV1 = Field(default_factory=PatchSetV1)
    unset: list[Literal[tuple(sorted(UNSET_PATHS))]] = Field(default_factory=list, max_length=len(UNSET_PATHS))

    @model_validator(mode='after')
    def operations(self):
        issues = []
        seen = set()
        assigned = {f'{ns}.{k}' for ns in ('overrides', 'scenario_inputs')
                    for k in sparse(getattr(self.set, ns))}
        for i, path in enumerate(self.unset):
            if path in seen:
                issues.append(issue(('unset', i), 'duplicate_unset', 'Unset paths must be unique'))
            seen.add(path)
            if path in assigned:
                issues.extend([
                    issue(('unset', i), 'set_unset_conflict', 'A field cannot be both set and unset'),
                    issue(('set', *path.split('.')), 'set_unset_conflict', 'A field cannot be both set and unset'),
                ])
        if issues:
            raise ValidationError.from_exception_data(type(self).__name__, [
                {'type': PydanticCustomError(e['type'], e['msg']), 'loc': tuple(e['loc']), 'input': None}
                for e in issues
            ])
        check_payload_size(self.model_dump(exclude_unset=True))
        return self


def personal_document(value: PersonalBuildingPutV1):
    """Persistence-ready full replacement; explicit null leaves are retained."""
    return {'schema_version': 1, 'display_name': value.display_name,
            'overrides': sparse(value.overrides), 'scenario_inputs': sparse(value.scenario_inputs)}


@dataclass(frozen=True)
class CodeCatalog:
    codes: Mapping[str, frozenset[str]]
    labels: Mapping[str, Mapping[str, 'CodeLabelV1']] | None = None


class CatalogUnavailable(Exception):
    """Required semantic-validation dependency; later HTTP routes map to 503."""


class ContractValidationError(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__('Invalid personal building contract')


def issue(loc, code, message):
    return {'loc': list(loc), 'type': code, 'msg': message}


def validation_errors(error, prefix=('body',)):
    """Sanitized FastAPI-style details; never includes input, ctx or DB errors."""
    if isinstance(error, ContractValidationError):
        entries = error.errors
    else:
        entries = error.errors(include_url=False, include_context=False, include_input=False) if isinstance(error, ValidationError) else error.errors()
    return [issue((*prefix, *e['loc']), e['type'],
                  # Do not echo custom exception text embedded in Pydantic value errors.
                  'Invalid value' if e['type'] == 'value_error' else e['msg']) for e in entries]


def validate_new_codes(overrides, catalog: CodeCatalog | None, *, prefix=('overrides',)):
    supplied = {k: v for k, v in sparse(overrides).items() if k in CODE_FIELDS and v is not None}
    if not supplied:
        return
    if catalog is None or any(k not in catalog.codes or not catalog.codes[k] for k in supplied):
        raise CatalogUnavailable('Code catalog temporarily unavailable')
    errors = [issue((*prefix, k), 'unsupported_code', 'Code is not in the current supported catalog')
              for k, v in supplied.items() if v not in catalog.codes[k]]
    if errors:
        raise ContractValidationError(errors)


def replace_personal(value: PersonalBuildingPutV1, catalog: CodeCatalog | None):
    validate_new_codes(value.overrides, catalog)
    return PersonalBuildingPutV1.model_validate(personal_document(value))


def apply_personal_patch(existing: PersonalBuildingPutV1, patch: PersonalBuildingPatchV1,
                         catalog: CodeCatalog | None):
    """Atomic in-memory update. Only newly set codes need current membership."""
    validate_new_codes(patch.set.overrides, catalog, prefix=('set', 'overrides'))
    document = personal_document(existing)
    for namespace in ('overrides', 'scenario_inputs'):
        document[namespace].update(sparse(getattr(patch.set, namespace)))
    for path in patch.unset:
        namespace, key = path.split('.')
        document[namespace].pop(key, None)
    if 'display_name' in patch.model_fields_set:
        document['display_name'] = patch.display_name
    return PersonalBuildingPutV1.model_validate(document)


class AvailabilityV1(ContractModel):
    status: Availability
    reason: str | None = None


class FieldProvenanceV1(AvailabilityV1):
    origin: Origin
    source: str | None = None
    source_observed_at: UtcTime | None = None
    upstream_modified_at: UtcTime | None = None
    is_override: bool = False
    dependencies: list[str] = Field(default_factory=list)
    source_language: Literal['fi', 'sv'] | None = None


class SourceV1(AvailabilityV1):
    upstream_modified_at: UtcTime | None = None
    timestamp_status: Literal['known', 'missing', 'invalid', 'unknown'] = 'unknown'
    imported_at: UtcTime | None = None


class SourcesV1(ContractModel):
    main: SourceV1
    ryhti_building: SourceV1
    ryhti_address: SourceV1
    ryhti_polygon: SourceV1
    certificate: SourceV1
    code_catalog: SourceV1


class IdentityV1(ContractModel):
    permanent_building_identifier: str | None = None
    property_identifier: str | None = None
    address_key: str | None = None


# Source data is normalized independently from personal abuse/accident bounds.
DetailsV1 = create_model('DetailsV1', __base__=ContractModel,
    completion_date=(date | None, None),
    ventilation_category=(Literal['mechanical', 'gravity', 'other', 'no_info'] | None, 'no_info'),
    **{k: ((int if d.kind in ('int', 'year') else Quantity if d.kind == 'number' else str) | None, None)
       for k, d in EDITABLE_FIELDS.items()})


class LocationV1(ContractModel):
    longitude: Annotated[float, Field(ge=-180, le=180, allow_inf_nan=False)]
    latitude: Annotated[float, Field(ge=-90, le=90, allow_inf_nan=False)]
    srid: Literal[4326] = 4326


Coordinate = Annotated[list[Annotated[float, Field(allow_inf_nan=False)]], Field(min_length=2, max_length=3)]


class PointV1(ContractModel):
    type: Literal['Point']
    coordinates: Coordinate


class PolygonV1(ContractModel):
    type: Literal['Polygon']
    coordinates: list[list[Coordinate]]


class MultiPolygonV1(ContractModel):
    type: Literal['MultiPolygon']
    coordinates: list[list[list[Coordinate]]]


class GeometryV1(ContractModel):
    srid: Literal[3857, 4326]
    source: Literal['main.polygon', 'main.point', 'polygons.polygon', 'buildings.point']
    geometry: Annotated[PointV1 | PolygonV1 | MultiPolygonV1, Field(discriminator='type')]


class SelectionV1(ContractModel):
    address_variants: int = 0
    geometry_variants: int = 0
    geometry_source: str | None = None
    geometry_status: AvailabilityV1
    address_policy: Literal['usable_then_C_lexical'] = 'usable_then_C_lexical'
    geometry_policy: Literal['polygon_then_point_EWKB'] = 'polygon_then_point_EWKB'


class ConsumptionComponentV1(ContractModel):
    carrier: Literal['electricity', 'district_heat', 'district_cooling', 'fossil_fuel', 'renewable_fuel', 'other']
    annual_kwh: Quantity
    source_slot: Annotated[int, Field(ge=0, le=2)] | None = None
    name: Annotated[str, Field(max_length=SOURCE_TEXT_CHARS)] | None = None


class CertificateConsumptionV1(VersionedV1):
    basis: Literal['standardized_calculation']
    unit: Literal['kWh/year']
    building_identifier: Annotated[str, Field(min_length=1, max_length=SOURCE_TEXT_CHARS)]
    certificate_id: Annotated[str, Field(min_length=1, max_length=SOURCE_TEXT_CHARS)]
    certificate_version: Literal[2013, 2018]
    signed_at: date | None
    valid_until: date | None
    heated_net_area_m2: Annotated[Quantity, Field(gt=0)] | None
    components: Annotated[list[ConsumptionComponentV1], Field(min_length=1, max_length=8)]
    completeness: Literal['complete', 'partial']
    total_kwh_year: Quantity | None

    @model_validator(mode='after')
    def coherent(self):
        keys = [(c.carrier, c.source_slot) for c in self.components]
        if len(set(keys)) != len(keys) or any(
            (c.carrier == 'other') != (c.source_slot is not None) or
            (c.carrier != 'other' and c.name is not None) or
            (c.name is not None and '\x00' in c.name) for c in self.components
        ):
            fail('consumption_components', 'Invalid consumption component identities')
        standard = {c.carrier for c in self.components if c.carrier != 'other'}
        if self.completeness == 'complete':
            if len(standard) != 5 or self.total_kwh_year is None or not math.isclose(
                self.total_kwh_year, sum(c.annual_kwh for c in self.components), rel_tol=1e-12, abs_tol=1e-9
            ):
                fail('consumption_total', 'Complete consumption requires all carriers and their sum')
        elif self.total_kwh_year is not None:
            fail('consumption_total', 'Partial consumption cannot claim a complete total')
        return self


class CertificateV1(ContractModel):
    certificate_id: str | None = None
    version: int | None = None
    building_identifier: str | None = None
    signed_at: date | None = None
    valid_until: date | None = None
    inspection_date: date | None = None
    imported_at: UtcTime | None = None
    energy_class: EnergyClass | None = None
    previous_class: EnergyClass | None = None
    class_scale_year: Literal[2013, 2018] | None = None
    history_count: int | None = None
    heated_net_area: Quantity | None = None
    ventilation_type_id: str | None = None
    ventilation_type_label_fi: str | None = None
    ventilation_type_label_sv: str | None = None
    ventilation_description_fi: str | None = None
    ventilation_description_sv: str | None = None
    recommendations_fi: str | None = None
    recommendations_sv: str | None = None
    consumption: CertificateConsumptionV1 | None = None
    publication_comparison: Literal['agrees', 'differs', 'unknown'] = 'unknown'
    field_availability: dict[str, AvailabilityV1] = Field(default_factory=dict)


class ScenarioQuantityV1(AvailabilityV1):
    value: Quantity | None = None
    unit: Literal['kWh/m²/year'] = 'kWh/m²/year'


class ModelV1(ContractModel):
    modeled_energy_class: EnergyClass | None = None
    modeled_energy_e_value: Quantity | None = None
    reason: str | None = None
    area_source: str | None = None
    energy_class: EnergyClass | None = None
    is_energy_class_modeled: bool | None = None
    energy_class_year: int | None = None
    consumption_scenarios: dict[Literal[tuple(PREFIXES)], dict[Literal[tuple(MEASURES)], dict[Literal[tuple(QUANTITIES)], ScenarioQuantityV1]]] = Field(default_factory=dict)
    field_availability: dict[str, AvailabilityV1] = Field(default_factory=dict)
    context: Literal['source_based_not_recalculated'] = 'source_based_not_recalculated'
    generated_at: UtcTime | None = None


class CodeLabelV1(ContractModel):
    label_fi: str | None = None
    label_sv: str | None = None
    label_en: str | None = None


class CodeLabelsV1(ContractModel):
    main_purpose: CodeLabelV1 | None = None
    heating_method: CodeLabelV1 | None = None
    heating_energy_source: CodeLabelV1 | None = None


class PublishedCertificateV1(ContractModel):
    """Main snapshot facts; never attributed to a current selected certificate ID."""
    source: Literal['main'] = 'main'
    energy_class: EnergyClass | None = None
    previous_class: EnergyClass | None = None
    class_scale_year: Literal[2013, 2018] | None = None
    valid_until: date | None = None
    heated_net_area: Quantity | None = None
    ventilation_type_id: str | None = None
    ventilation_description_fi: str | None = None
    ventilation_description_sv: str | None = None
    recommendations_fi: str | None = None
    recommendations_sv: str | None = None
    field_availability: dict[str, AvailabilityV1] = Field(default_factory=dict)


class CanonicalBuildingV1(VersionedV1):
    building_key: BuildingKey
    base_status: Literal['available'] = 'available'
    identity: IdentityV1
    code_labels: CodeLabelsV1 = Field(default_factory=CodeLabelsV1)
    published_certificate: PublishedCertificateV1 | None = None
    details: DetailsV1
    geometry: GeometryV1 | None = None
    location: LocationV1 | None = None
    selection: SelectionV1
    certificate: CertificateV1 | None = None
    model: ModelV1
    sources: SourcesV1
    source_observed_at: UtcTime | None = None
    timestamp_status: Literal['unknown'] = 'unknown'
    resolved_at: UtcTime
    field_provenance: dict[str, FieldProvenanceV1]
    source_availability: dict[str, AvailabilityV1]


class BuildingSummaryV1(VersionedV1):
    building_key: BuildingKey
    name: None = None
    address_fin: str | None = None
    postal_code: str | None = None
    postal_office_fin: str | None = None
    location: LocationV1 | None = None
    source_observed_at: UtcTime | None = None
    source_availability: dict[str, AvailabilityV1]


class LastKnownBuildingSummaryV1(BuildingSummaryV1):
    captured_at: UtcTime


class EffectiveV1(ContractModel):
    published_certificate: PublishedCertificateV1 | None = None
    details: DetailsV1
    scenario_inputs: ScenarioInputsV1
    certificate: CertificateV1 | None = None
    model: ModelV1 | None = None
    model_input_divergence: list[str] = Field(default_factory=list)
    scenario_prefix: Literal[tuple(PREFIXES)] | None = None
    resident_count_applicability: Literal['residential', 'unsupported', 'unknown']

    @field_serializer('scenario_inputs')
    def sparse_scenario(self, value):
        return sparse(value)


class PersonalBuildingProjectionV1(VersionedV1):
    building_key: BuildingKey
    base_status: Literal['available', 'unavailable']
    base_unavailable_reason: Literal['missing', 'ambiguous', 'source_unavailable'] | None = None
    base: CanonicalBuildingV1 | None
    overrides: BuildingOverridesV1
    scenario_inputs: ScenarioInputsV1
    effective: EffectiveV1
    field_provenance: dict[str, FieldProvenanceV1]
    source_availability: dict[str, AvailabilityV1]
    source_observed_at: UtcTime | None
    last_known_summary: LastKnownBuildingSummaryV1 | None = None

    @field_serializer('overrides', 'scenario_inputs')
    def sparse_inputs(self, value):
        return sparse(value)


def ventilation_category(code, description):
    if code in VENTILATION_TYPES:
        return VENTILATION_TYPES[code][0]
    return 'other' if code or description else 'no_info'


def scenario_prefix(details):
    source = details.heating_energy_source
    if source == '04':
        return {'01': 'elecb', '03': 'delec'}.get(details.heating_method)
    return {'01': 'distr', '02': 'oil', '07': 'wood', '09': 'gshp'}.get(source)


def project_building(base: CanonicalBuildingV1 | None, overrides: BuildingOverridesV1,
                     scenario_inputs: ScenarioInputsV1, *,
                     last_known_summary: LastKnownBuildingSummaryV1 | None = None,
                     base_unavailable_reason: Literal['missing', 'ambiguous', 'source_unavailable'] = 'missing',
                     building_key: str | None = None) -> PersonalBuildingProjectionV1:
    """Pure allowlisted overlay shared by future owner/share reads.

    Missing source never falls back to historical snapshot facts. A key must be
    supplied explicitly or by the base/last-known summary and must agree.
    """
    candidates = [k for k in (building_key, base.building_key if base else None,
                             last_known_summary.building_key if last_known_summary else None) if k is not None]
    if not candidates or len(set(candidates)) != 1:
        raise ValueError('Projection requires one consistent building key')
    values = base.details.model_dump() if base else DetailsV1().model_dump()
    provenance = {k: v.model_copy(deep=True) for k, v in base.field_provenance.items()} if base else {
        f'details.{k}': FieldProvenanceV1(origin='unavailable', status='source_unavailable', reason=base_unavailable_reason)
        for k in DetailsV1.model_fields
    }
    for key, value in sparse(overrides).items():
        values[key] = value
        provenance[f'details.{key}'] = FieldProvenanceV1(origin='user', source=f'overrides.{key}',
            status='user_unknown' if value is None else 'available', is_override=True)
    vent_keys = ('ventilation_type_id', 'ventilation_description')
    changed_vent = any(k in overrides.model_fields_set for k in vent_keys)
    values['ventilation_category'] = ventilation_category(values['ventilation_type_id'], values['ventilation_description'])
    if base is None and not any(values[k] is not None for k in vent_keys):
        values['ventilation_category'] = None
    provenance['details.ventilation_category'] = FieldProvenanceV1(origin='derived',
        source='ventilation_policy_v1', status='available' if values['ventilation_category'] is not None else 'user_unknown' if changed_vent else 'source_unavailable', is_override=changed_vent,
        dependencies=[f'details.{k}' for k in vent_keys])
    resident_present = 'resident_count' in scenario_inputs.model_fields_set
    provenance['scenario_inputs.resident_count'] = FieldProvenanceV1(
        origin='user' if resident_present else 'unavailable', source='scenario_inputs.resident_count' if resident_present else 'frontend_default',
        status=('user_unknown' if scenario_inputs.resident_count is None else 'available') if resident_present else 'missing',
        reason=None if resident_present else 'frontend_default_not_computed', is_override=resident_present)
    details = DetailsV1.model_validate(values)
    availability = {k: v.model_copy(deep=True) for k, v in base.source_availability.items()} if base else {
        'geometry': AvailabilityV1(status='source_unavailable', reason=base_unavailable_reason),
        'source_observed_at': AvailabilityV1(status='missing', reason='unknown_source_freshness'),
    }
    availability.update({k: AvailabilityV1(status=v.status, reason=v.reason) for k, v in provenance.items()})
    return PersonalBuildingProjectionV1(
        building_key=candidates[0], base_status='available' if base else 'unavailable',
        base_unavailable_reason=None if base else base_unavailable_reason,
        base=base.model_copy(deep=True) if base else None, overrides=overrides.model_copy(deep=True),
        scenario_inputs=scenario_inputs.model_copy(deep=True),
        effective=EffectiveV1(published_certificate=base.published_certificate.model_copy(deep=True) if base and base.published_certificate else None, details=details, scenario_inputs=scenario_inputs.model_copy(deep=True),
            certificate=base.certificate.model_copy(deep=True) if base and base.certificate else None,
            model=base.model.model_copy(deep=True) if base else None,
            model_input_divergence=[k for k in ('construction_year', 'main_purpose', 'heating_method', 'heating_energy_source', 'floor_area', 'total_area', 'gross_floor_area', 'number_of_storeys')
                                    if k in overrides.model_fields_set and (not base or getattr(base.details, k) != getattr(overrides, k))],
            scenario_prefix=scenario_prefix(details),
            resident_count_applicability='residential' if details.main_purpose in ('05', '06') else 'unknown' if details.main_purpose is None else 'unsupported'),
        field_provenance=provenance, source_availability=availability,
        source_observed_at=base.source_observed_at if base else None,
        last_known_summary=last_known_summary.model_copy(deep=True) if last_known_summary else None)
