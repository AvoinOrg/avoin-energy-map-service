"""Explicit public serialization boundary, independent of owner metadata."""
from typing import Literal

from pydantic import Field, field_serializer, field_validator

from src.building_contract import (
    AvailabilityV1, BuildingKey, BuildingOverridesV1, CertificateV1, CodeLabelsV1,
    ContractModel, DetailsV1, DisplayName, EffectiveV1, FieldProvenanceV1,
    GeometryV1, LastKnownBuildingSummaryV1, LocationV1, ModelV1,
    PublishedCertificateV1, ScenarioInputsV1, SelectionV1, SourcesV1,
    UtcTime, VersionedV1, project_building, sparse,
)
from src import user_buildings as owner
from src.personal_renovations_contract import (ScenarioInputsV4, EffectiveV4, VersionedV4, ReadPathV4, AvailabilityPathV4, RENOVATIONS_PATHS, project_v4)
from src.personal_hvac_contract import (HeatingSourceProvenanceV3, ScenarioInputsV3, EffectiveV3, InputStatusV3, DefaultResolutionV3, VersionedV3, ReadPathV3, AvailabilityPathV3, SYSTEMS_PATHS, project_v3)
from src.personal_property_contract import (ScenarioInputsV2, EffectiveV2, InputStatusV2, VersionedV2, NEW_PATHS, ReadPathV2, AvailabilityPathV2, v1_document, project_v2)


class ShareCreatedV1(VersionedV1):
    share_token: str = Field(pattern=r'^[A-Za-z0-9_-]{43}$')
    resolver_path: str = Field(pattern=r'^/energy-data/shared-buildings/[A-Za-z0-9_-]{43}$')
    revision: int = Field(ge=1)
    updated_at: UtcTime


class SharedIdentityV1(ContractModel):
    permanent_building_identifier: str | None
    property_identifier: str | None


class SharedBaseV1(VersionedV1):
    building_key: BuildingKey
    base_status: Literal['available']
    identity: SharedIdentityV1
    details: DetailsV1
    geometry: GeometryV1 | None
    location: LocationV1 | None
    selection: SelectionV1
    code_labels: CodeLabelsV1
    certificate: CertificateV1 | None
    published_certificate: PublishedCertificateV1 | None
    model: ModelV1
    sources: SourcesV1
    source_observed_at: UtcTime | None
    timestamp_status: Literal['unknown']
    resolved_at: UtcTime
    field_provenance: dict[str, FieldProvenanceV1]
    source_availability: dict[str, AvailabilityV1]


class SharedBuildingV1(VersionedV1):
    building_key: BuildingKey
    display_name: DisplayName | None
    revision: int = Field(ge=1)
    created_at: UtcTime
    updated_at: UtcTime
    base_status: Literal['available', 'unavailable']
    base_unavailable_reason: Literal['missing', 'ambiguous', 'source_unavailable'] | None
    base: SharedBaseV1 | None
    overrides: BuildingOverridesV1
    scenario_inputs: ScenarioInputsV1
    effective: EffectiveV1
    field_provenance: dict[str, FieldProvenanceV1]
    source_availability: dict[str, AvailabilityV1]
    source_observed_at: UtcTime | None
    last_known_summary: LastKnownBuildingSummaryV1

    @field_serializer('overrides', 'scenario_inputs')
    def sparse_inputs(self, value):
        return sparse(value)


class SharedBuildingV2(SharedBuildingV1):
    schema_version: Literal[2] = 2
    document_schema_version: Literal[1, 2]
    scenario_inputs: ScenarioInputsV2
    effective: EffectiveV2
    input_status: InputStatusV2
    field_provenance: dict[ReadPathV2, FieldProvenanceV1]
    source_availability: dict[AvailabilityPathV2, AvailabilityV1]

    @field_validator('schema_version', mode='before')
    @classmethod
    def exact_version(cls, value):
        return VersionedV2.exact_version(value)


class SharedBuildingV3(SharedBuildingV2):
    schema_version: Literal[3] = 3
    document_schema_version: Literal[1, 2, 3]
    scenario_inputs: ScenarioInputsV3
    effective: EffectiveV3
    input_status: InputStatusV3
    systems_source_provenance: list[HeatingSourceProvenanceV3] = Field(max_length=16)
    systems_defaults: DefaultResolutionV3
    field_provenance: dict[ReadPathV3, FieldProvenanceV1]
    source_availability: dict[AvailabilityPathV3, AvailabilityV1]

    @field_validator('schema_version', mode='before')
    @classmethod
    def exact_version(cls, value):
        return VersionedV3.exact_version(value)


class SharedBuildingV4(SharedBuildingV3):
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


# Map keys/dependencies cannot smuggle internal identifiers into the safe DTO.
PUBLIC_PROVENANCE = frozenset({f'details.{k}' for k in DetailsV1.model_fields} | {
    'identity.permanent_building_identifier', 'identity.property_identifier',
    'scenario_inputs.resident_count'})
PUBLIC_AVAILABILITY = PUBLIC_PROVENANCE | {'geometry', 'source_observed_at'}
SUMMARY_AVAILABILITY = frozenset({'address_fin', 'postal_code', 'postal_office_fin', 'location', 'main', 'source_observed_at'})


def public_provenance(values, allowed=PUBLIC_PROVENANCE):
    return {key: value.model_copy(update={'dependencies': [p for p in value.dependencies if p in allowed]})
            for key, value in values.items() if key in allowed}


def public_map(values, allowed):
    return {key: value for key, value in values.items() if key in allowed}


def public_component(value):
    if value is None:
        return None
    # Component properties are closed DTOs; availability keys must also belong
    # to their component, rather than arbitrary source metadata.
    return value.model_copy(update={'field_availability': public_map(value.field_availability,
        set(type(value).model_fields) - {'field_availability'})})


def shared_projection(row, base, reason, version=1):
    owner.require_read_version(row, version)
    document = owner.stored_document(row)
    summary = owner.stored_summary(row)
    summary = summary.model_copy(update={'source_availability': public_map(summary.source_availability, SUMMARY_AVAILABILITY)})
    if version == 1:
        document = v1_document(document)
        projection = project_building(base, document.overrides, document.scenario_inputs,
            building_key=row.building_key, last_known_summary=summary, base_unavailable_reason=reason)
    else:
        projection = {2: project_v2, 3: project_v3, 4: project_v4}[version](base, document, building_key=row.building_key,
            last_known_summary=summary, base_unavailable_reason=reason)
    allowed = PUBLIC_PROVENANCE if version == 1 else PUBLIC_PROVENANCE | NEW_PATHS | {'display_name'}
    if version in (3, 4):
        allowed |= SYSTEMS_PATHS
    if version == 4:
        allowed |= RENOVATIONS_PATHS
    safe_base = None
    if base is not None:
        safe_base = SharedBaseV1(
            building_key=base.building_key, base_status=base.base_status,
            identity=SharedIdentityV1(permanent_building_identifier=base.identity.permanent_building_identifier,
                                     property_identifier=base.identity.property_identifier),
            details=base.details, geometry=base.geometry, location=base.location,
            selection=base.selection, code_labels=base.code_labels,
            certificate=public_component(base.certificate),
            published_certificate=public_component(base.published_certificate), model=public_component(base.model),
            sources=base.sources, source_observed_at=base.source_observed_at,
            timestamp_status=base.timestamp_status, resolved_at=base.resolved_at,
            field_provenance=public_provenance(base.field_provenance),
            source_availability=public_map(base.source_availability, PUBLIC_AVAILABILITY))
    effective = projection.effective.model_copy(update={
        'certificate': public_component(projection.effective.certificate),
        'published_certificate': public_component(projection.effective.published_certificate),
        'model': public_component(projection.effective.model)})
    model = {1: SharedBuildingV1, 2: SharedBuildingV2, 3: SharedBuildingV3, 4: SharedBuildingV4}[version]
    fields = {} if version == 1 else {'document_schema_version': row.schema_version, 'input_status': projection.input_status}
    if version in (3, 4):
        fields['systems_defaults'] = projection.systems_defaults
        fields['systems_source_provenance'] = projection.systems_source_provenance
    return model(**fields, building_key=row.building_key, display_name=document.display_name,
        revision=row.revision, created_at=row.created_at, updated_at=row.updated_at,
        base_status=projection.base_status, base_unavailable_reason=projection.base_unavailable_reason,
        base=safe_base, overrides=document.overrides, scenario_inputs=projection.scenario_inputs,
        effective=effective, field_provenance=public_provenance(projection.field_provenance, allowed),
        source_availability=public_map(projection.source_availability, allowed | {'geometry', 'source_observed_at'}),
        source_observed_at=projection.source_observed_at, last_known_summary=summary)
