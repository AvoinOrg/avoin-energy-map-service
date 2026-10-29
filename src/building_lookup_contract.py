"""Closed lookup v1 DTOs. Discovery never confirms or saves a building."""
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, WithJsonSchema, field_validator, model_validator

from src.building_contract import (
    BuildingKey, ContractModel, UtcTime, VersionedV1, fail, plain_text,
)

REQUEST_BYTES = 16 * 1024
RESPONSE_BYTES = 512 * 1024
RAW_ROWS = 2048
CANDIDATE_KEYS = 512
CURSOR_SECONDS = 300
POLICY = 'lookup-v1-exact-utf8-C-1'
Mode = Literal['address', 'property_identifier', 'permanent_building_identifier']


def bounded_text(value, maximum):
    value = plain_text(value)
    if len(value.encode('utf-8')) > maximum:
        fail('text_bytes', 'Text exceeds the UTF-8 byte limit')
    return value


AddressText = Annotated[str, BeforeValidator(lambda v: bounded_text(v, 512)),
                        Field(max_length=512, json_schema_extra={'x-maxUtf8Bytes':512})]
PermanentId = Annotated[str, BeforeValidator(lambda v: bounded_text(v, 256)),
                        Field(max_length=256, json_schema_extra={'x-maxUtf8Bytes':256})]


def property_id(value):
    if (not isinstance(value, str) or len(value) != 14 or
            any(c not in '0123456789' for c in value) or value == '0' * 14):
        fail('property_identifier', 'Expected a nonzero 14 ASCII digit string')
    return value


PropertyId = Annotated[str, BeforeValidator(property_id), Field(min_length=14, max_length=14, pattern=r'^[0-9]{14}$',
                       json_schema_extra={'not': {'const': '00000000000000'}})]
PostalCode = Annotated[str, Field(min_length=5, max_length=5, pattern=r'^[0-9]{5}$')]
Municipality = Annotated[str, Field(min_length=3, max_length=3, pattern=r'^[0-9]{3}$')]


class AddressQueryV1(ContractModel):
    mode: Literal['address']
    address_fin: AddressText
    # None is the internal omission marker, never a valid supplied filter.
    postal_code: Annotated[PostalCode | None, WithJsonSchema(
        {'type':'string','minLength':5,'maxLength':5,'pattern':r'^[0-9]{5}$'})] = Field(
            default=None, json_schema_extra=lambda schema:schema.pop('default',None))
    municipality_number: Annotated[Municipality | None, WithJsonSchema(
        {'type':'string','minLength':3,'maxLength':3,'pattern':r'^[0-9]{3}$'})] = Field(
            default=None, json_schema_extra=lambda schema:schema.pop('default',None))

    @model_validator(mode='before')
    @classmethod
    def omitted_or_string(cls, value):
        if isinstance(value, dict) and any(k in value and value[k] is None for k in ('postal_code', 'municipality_number')):
            fail('string_type', 'Omit an unused address filter')
        return value


class PropertyQueryV1(ContractModel):
    mode: Literal['property_identifier']
    property_identifier: PropertyId


class PermanentQueryV1(ContractModel):
    mode: Literal['permanent_building_identifier']
    permanent_building_identifier: PermanentId


QueryV1 = Annotated[AddressQueryV1 | PropertyQueryV1 | PermanentQueryV1, Field(discriminator='mode')]


class BuildingLookupRequestV1(VersionedV1):
    schema_version: Literal[1] = Field(...)
    query: QueryV1
    limit: Annotated[int, Field(ge=1, le=50)] = 20
    cursor: Annotated[str, Field(min_length=1, max_length=2048, pattern=r'^[A-Za-z0-9_-]+$')] | None = None


class LookupAddressV1(ContractModel):
    address_fin: str | None
    postal_code: str | None
    postal_office_fin: str | None
    municipality_number: str | None
    source: Literal['main'] = 'main'
    source_language: Literal['fi'] = 'fi'


class LookupEvidenceV1(ContractModel):
    component: Literal['main'] = 'main'
    upstream_modified_at: UtcTime | None = None
    timestamp_status: Literal['unknown', 'missing', 'invalid', 'known'] = 'unknown'
    reason: Literal['not_projected'] = 'not_projected'


class LookupCandidateV1(ContractModel):
    building_key: BuildingKey
    requires_canonical_confirmation: Literal[True] = True
    permanent_building_identifier: PermanentId | None
    property_identifier: PropertyId | None
    permanent_identity_status: Literal['available', 'missing']
    property_identity_status: Literal['available', 'missing', 'invalid']
    identity_conflict: Literal[False] = False
    matched_mode: Literal['permanent_building_identifier'] = 'permanent_building_identifier'
    matched_source: Literal['main'] = 'main'
    representative_address: LookupAddressV1 | None
    address_variant_count: Annotated[int, Field(ge=0, le=RAW_ROWS)]
    matching_address: None = None
    evidence: list[LookupEvidenceV1]
    geometry_availability: Literal['present_unvalidated', 'missing_in_scope', 'unknown']
    location: None = None


class LookupComponentV1(ContractModel):
    component: Literal['main', 'buildings', 'addresses', 'polygons']
    status: Literal['included', 'absent', 'unreadable', 'unsupported']
    reason: Literal['indexed_permanent_equality', 'no_reviewed_bounded_path', 'not_in_main_scope']


class LookupCoverageV1(ContractModel):
    scope: Literal['main'] = 'main'
    enumeration_complete: bool
    scope_complete: Literal[False] = False
    national_completeness: Literal['unknown'] = 'unknown'
    components: list[LookupComponentV1]


def main_coverage():
    return LookupCoverageV1(enumeration_complete=True, components=[
        LookupComponentV1(component='main', status='included', reason='indexed_permanent_equality'),
        LookupComponentV1(component='buildings', status='unsupported', reason='no_reviewed_bounded_path'),
        LookupComponentV1(component='addresses', status='unsupported', reason='not_in_main_scope'),
        LookupComponentV1(component='polygons', status='unsupported', reason='not_in_main_scope'),
    ])


class LookupSourceVersionV1(ContractModel):
    dataset_version: None = None
    checksum: None = None
    refreshed_at: None = None
    status: Literal['unknown'] = 'unknown'
    reason: Literal['no_authoritative_generation_lineage'] = 'no_authoritative_generation_lineage'


class LookupPageInfoV1(ContractModel):
    total_candidates: Annotated[int, Field(ge=0, le=CANDIDATE_KEYS)]
    has_more: bool
    next_cursor: str | None
    order_policy: Literal['lookup-v1-exact-utf8-C-1'] = POLICY
    permanent_id_multiplicity: bool


class BuildingLookupPageV1(VersionedV1):
    query: QueryV1
    candidates: list[LookupCandidateV1] = Field(max_length=50)
    outcome: Literal['candidates', 'no_match', 'no_match_in_scope']
    coverage: LookupCoverageV1
    page: LookupPageInfoV1
    observed_at: UtcTime
    source_version: LookupSourceVersionV1


ErrorCode = Literal[
    'lookup_capability_unavailable', 'lookup_source_unavailable', 'lookup_source_contract_failure',
    'lookup_identity_conflict', 'lookup_page_invalidated', 'lookup_query_too_broad',
    'lookup_invalid_request', 'lookup_payload_too_large', 'lookup_unsupported_media_type',
    'lookup_internal_error',
]


class LookupErrorDetailV1(ContractModel):
    code: ErrorCode
    message: str
    mode: Mode | None = None
    component: Literal['main'] | None = None


class BuildingLookupErrorV1(ContractModel):
    detail: LookupErrorDetailV1


class LookupCursorV1(ContractModel):
    version: Annotated[int, Field(ge=1)]
    policy: str
    query_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    scope_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidates_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    last_key: BuildingKey
    limit: Annotated[int, Field(ge=1, le=50)]
    issued: Annotated[int, Field(ge=0)]
    expires: Annotated[int, Field(ge=0)]

    @field_validator('version', mode='before')
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            fail('cursor_version', 'Expected integer cursor version')
        return value
