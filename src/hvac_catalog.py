"""Bounded bundled HVAC capability discovery; never implies full HVAC coverage."""
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from src.building_contract import ContractModel, VersionedV1

SNAPSHOT = Path(__file__).parent / 'data/hvac-catalog-v1.snapshot.json'
LEGACY_DEFAULTS_VERSION = 'hvac-defaults-2026-10-30.1'
DEFAULTS_VERSION = 'hvac-inputs-2026-10-31.1'
CAPABILITY_VERSION = DEFAULTS_VERSION
CATALOG_V2_SNAPSHOT = SNAPSHOT.with_name('hvac-catalog-v2.snapshot.json')
UNRESOLVED = ('heating_sources_and_shares', 'heated_quantities', 'cooling',
              'equipment', 'coefficients')
Capability = Literal['heating_sources_and_shares', 'heated_quantities', 'cooling', 'equipment', 'coefficients']


class HvacCatalogUnavailable(Exception):
    """A required bundled pin is missing/corrupt, not a source-data outage."""


class VentilationChoiceV1(ContractModel):
    code: str = Field(min_length=1, max_length=64)
    label_fi: str
    label_sv: str
    label_en: None = None
    lifecycle: Literal['unknown'] = 'unknown'
    upstream_status: Literal['not_supplied'] = 'not_supplied'
    selectable: bool
    applicability_note: str | None


class HvacDefaultsV1(ContractModel):
    defaults_version: Literal['hvac-defaults-2026-10-30.1']
    behavior: Literal['pinned_no_scientific_defaults']
    unavailable_capabilities: list[Capability]
    reason: Literal['domain_contract_unsettled']


class HvacCatalogV1(VersionedV1):
    personal_schema_version: Literal[3] = 3
    catalog_id: Literal['energy-personal-hvac-supported-subset']
    catalog_version: str = Field(pattern=r'^sha256:[0-9a-f]{64}$')
    coverage: Literal['partial']
    ventilation_namespace: Literal['energy_certificate/ilmanvaihtotyyppi']
    ventilation_multiplicity: Literal['single_detailed_code']
    source_uri: Literal['https://www.energiatodistusrekisteri.fi/api/public/energiatodistukset/ilmanvaihtotyyppi']
    evidence_verified_on: Literal['2026-09-15']
    upstream_version: None = None
    freshness: Literal['pinned_snapshot_not_live']
    attribution: str
    product_support: Literal['supported_single_code_subset']
    support_reference: str
    ventilation_choices: list[VentilationChoiceV1] = Field(min_length=1, max_length=100)
    unavailable_capabilities: list[Capability]
    defaults: HvacDefaultsV1

    @model_validator(mode='after')
    def integrity(self):
        codes = [c.code for c in self.ventilation_choices]
        if codes != sorted(set(codes)) or not any(c.selectable for c in self.ventilation_choices):
            raise ValueError('Choices must be unique, ordered and supported')
        if tuple(self.unavailable_capabilities) != UNRESOLVED or tuple(self.defaults.unavailable_capabilities) != UNRESOLVED:
            raise ValueError('Unresolved capability inventory must be explicit')
        return self


def content_version(document):
    content = {k: v for k, v in document.items() if k != 'catalog_version'}
    return 'sha256:' + hashlib.sha256(json.dumps(content, ensure_ascii=False,
        sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def load_legacy_hvac_catalog():
    try:
        raw = SNAPSHOT.read_bytes()
        if len(raw) > 100_000:
            raise ValueError
        result = HvacCatalogV1.model_validate_json(raw)
        if content_version(json.loads(raw)) != result.catalog_version:
            raise ValueError
        return result
    except (OSError, ValueError, TypeError):
        raise HvacCatalogUnavailable from None


class HvacChoiceV2(ContractModel):
    code: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_:-]+$')
    namespace: str
    label_fi: str | None
    label_sv: str | None
    label_en: str | None
    source_uri: str
    upstream_code: str | None
    upstream_status: Literal['VALID', 'unknown']
    lifecycle: Literal['current', 'deprecated', 'unknown']
    selectable: bool
    applicability_note: str | None


class EquipmentChoiceV2(ContractModel):
    code: Literal['geothermal_installed', 'solar_panels_installed', 'energy_storage_ready']
    label_fi: str
    label_sv: None = None
    label_en: None = None
    source_uri: str
    meaning: Literal['self_reported_installation', 'self_reported_readiness']


class DeferredCoefficientsV2(ContractModel):
    status: Literal['deferred'] = 'deferred'
    reason: Literal['owner_deferred'] = 'owner_deferred'
    editable: Literal[False] = False
    numeric_defaults: None = None
    attribution_editable: Literal[False] = False
    reset_available: Literal[False] = False
    required_for_save: Literal[False] = False
    required_for_completion: Literal[False] = False


class HvacCatalogV2(ContractModel):
    schema_version: Literal[2]
    personal_schema_version: Literal[3]
    capability_version: Literal['hvac-inputs-2026-10-31.1']
    catalog_id: Literal['energy-personal-hvac']
    catalog_version: str = Field(pattern=r'^sha256:[0-9a-f]{64}$')
    coverage: Literal['product_supported_choices_not_national_coverage']
    freshness: Literal['pinned_snapshot_not_live']
    evidence_verified_on: Literal['2026-10-31']
    upstream_version: None
    attribution: str
    defaults_version: Literal['hvac-inputs-2026-10-31.1']
    accepted_defaults_versions: list[Literal['hvac-inputs-2026-10-31.1', 'hvac-defaults-2026-10-30.1']] = Field(min_length=2, max_length=2)
    heating_namespace: Literal['ryhti:heating_energy_source']
    heating_multiplicity: Literal['unique_choices_optional_primary']
    heating_share_basis: Literal['annual_useful_space_heat_excluding_domestic_hot_water']
    heating_choices: list[HvacChoiceV2] = Field(min_length=1, max_length=16)
    cooling_namespace: Literal['energy:cooling']
    cooling_multiplicity: Literal['multiple_or_none']
    cooling_choices: list[HvacChoiceV2] = Field(min_length=1, max_length=14)
    ventilation_namespace: Literal['energy_certificate/ilmanvaihtotyyppi']
    ventilation_multiplicity: Literal['single_multiple_or_none']
    ventilation_choices: list[VentilationChoiceV1] = Field(min_length=1, max_length=9)
    equipment_choices: list[EquipmentChoiceV2] = Field(min_length=3, max_length=3)
    coefficients: DeferredCoefficientsV2
    unavailable_capabilities: list[Literal['coefficients']] = Field(min_length=1, max_length=1)

    @model_validator(mode='after')
    def integrity(self):
        for choices in (self.heating_choices, self.cooling_choices, self.ventilation_choices, self.equipment_choices):
            codes = [c.code for c in choices]
            if codes != sorted(set(codes)):
                raise ValueError('Choices must be unique and ordered')
        if set(self.accepted_defaults_versions) != {DEFAULTS_VERSION, LEGACY_DEFAULTS_VERSION}:
            raise ValueError('Technical pin compatibility must be explicit')
        if self.unavailable_capabilities != ['coefficients']:
            raise ValueError('Only coefficients are deferred')
        for choices, namespace in ((self.heating_choices, self.heating_namespace), (self.cooling_choices, self.cooling_namespace)):
            if any(c.namespace != namespace or not c.code.startswith(namespace + ':') or
                   (c.selectable and c.lifecycle == 'deprecated') for c in choices):
                raise ValueError('Choice namespace or lifecycle mismatch')
        return self


def load_hvac_catalog():
    """Current supported inputs; legacy snapshot/pin is never repurposed."""
    try:
        raw = CATALOG_V2_SNAPSHOT.read_bytes()
        if len(raw) > 100_000:
            raise ValueError
        result = HvacCatalogV2.model_validate_json(raw)
        if content_version(json.loads(raw)) != result.catalog_version:
            raise ValueError
        return result
    except (OSError, ValueError, TypeError):
        raise HvacCatalogUnavailable from None
