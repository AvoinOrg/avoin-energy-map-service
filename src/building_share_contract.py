"""Explicit public serialization boundary, independent of owner metadata."""
from typing import Literal

from pydantic import Field, field_serializer

from src.building_contract import (
    AvailabilityV1, BuildingKey, BuildingOverridesV1, CertificateV1, CodeLabelsV1,
    ContractModel, DetailsV1, DisplayName, EffectiveV1, FieldProvenanceV1,
    GeometryV1, LastKnownBuildingSummaryV1, LocationV1, ModelV1,
    PublishedCertificateV1, ScenarioInputsV1, SelectionV1, SourcesV1,
    UtcTime, VersionedV1, project_building, sparse,
)
from src import user_buildings as owner


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


# Map keys/dependencies cannot smuggle internal identifiers into the safe DTO.
PUBLIC_PROVENANCE = frozenset({f'details.{k}' for k in DetailsV1.model_fields} | {
    'identity.permanent_building_identifier', 'identity.property_identifier',
    'scenario_inputs.resident_count'})
PUBLIC_AVAILABILITY = PUBLIC_PROVENANCE | {'geometry', 'source_observed_at'}
SUMMARY_AVAILABILITY = frozenset({'address_fin', 'postal_code', 'postal_office_fin', 'location', 'main', 'source_observed_at'})


def public_provenance(values):
    return {key: value.model_copy(update={'dependencies': [p for p in value.dependencies if p in PUBLIC_PROVENANCE]})
            for key, value in values.items() if key in PUBLIC_PROVENANCE}


def public_map(values, allowed):
    return {key: value for key, value in values.items() if key in allowed}


def public_component(value):
    if value is None:
        return None
    # Component properties are closed DTOs; availability keys must also belong
    # to their component, rather than arbitrary source metadata.
    return value.model_copy(update={'field_availability': public_map(value.field_availability,
        set(type(value).model_fields) - {'field_availability'})})


def shared_projection(row, base, reason):
    document = owner.stored_document(row)
    summary = owner.stored_summary(row)
    summary = summary.model_copy(update={'source_availability': public_map(summary.source_availability, SUMMARY_AVAILABILITY)})
    projection = project_building(base, document.overrides, document.scenario_inputs,
        building_key=row.building_key, last_known_summary=summary, base_unavailable_reason=reason)
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
    return SharedBuildingV1(building_key=row.building_key, display_name=document.display_name,
        revision=row.revision, created_at=row.created_at, updated_at=row.updated_at,
        base_status=projection.base_status, base_unavailable_reason=projection.base_unavailable_reason,
        base=safe_base, overrides=document.overrides, scenario_inputs=document.scenario_inputs,
        effective=effective, field_provenance=public_provenance(projection.field_provenance),
        source_availability=public_map(projection.source_availability, PUBLIC_AVAILABILITY),
        source_observed_at=projection.source_observed_at, last_known_summary=summary)
