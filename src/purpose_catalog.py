"""Pinned purpose-only registry snapshot; no runtime upstream or source writes."""
from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from src.building_contract import CodeCatalog, ContractModel, UtcTime, VersionedV1

SOURCE_URI = 'http://uri.suomi.fi/codelist/rytj/avoin_rakennusluokitus'
SNAPSHOT = Path(__file__).parent / 'data/purpose-catalog-v1.snapshot.json'


class PurposeCatalogUnavailable(Exception):
    """Missing/corrupt/unadopted snapshot; safe HTTP boundary is 503."""


class PurposeChoiceV1(ContractModel):
    code: str = Field(min_length=1, max_length=16)
    label_fi: str | None
    label_sv: str | None
    label_en: str | None
    lifecycle: Literal['active', 'deprecated', 'withdrawn', 'unknown']
    upstream_status: str
    valid_from: date | None
    valid_until: date | None
    selectable: bool

    @model_validator(mode='after')
    def selection(self):
        # Product support is independent of upstream DRAFT/unknown lifecycle.
        if self.selectable and self.lifecycle in ('deprecated', 'withdrawn'):
            raise ValueError('Deprecated or withdrawn choices cannot be selectable')
        return self


class PurposeCatalogV1(VersionedV1):
    catalog_id: Literal['rytj/avoin_rakennusluokitus']
    catalog_version: str = Field(pattern=r'^sha256:[0-9a-f]{64}$')
    source_uri: Literal['http://uri.suomi.fi/codelist/rytj/avoin_rakennusluokitus']
    provider: str
    scheme: Literal['avoin_rakennusluokitus']
    upstream_version: str | None
    upstream_modified_at: UtcTime | None
    retrieved_at: UtcTime
    freshness_status: Literal['pinned_snapshot']
    attribution: str
    product_support: Literal['pending', 'adopted']
    approval_reference: str | None = Field(default=None, min_length=1, max_length=500)
    scheme_status: str
    choices: list[PurposeChoiceV1] = Field(min_length=1, max_length=10000)

    @model_validator(mode='after')
    def ordering(self):
        codes = [c.code for c in self.choices]
        if codes != sorted(set(codes)):
            raise ValueError('Choices must have unique exact codes in deterministic order')
        if self.product_support == 'pending':
            if self.approval_reference is not None or any(c.selectable for c in self.choices):
                raise ValueError('Pending adoption cannot authorize choices')
        elif self.approval_reference is None or not self.approval_reference.strip() or not any(c.selectable for c in self.choices):
            raise ValueError('Adoption requires its approval reference and supported choices')
        return self


def content_version(document):
    content = {k: v for k, v in document.items() if k not in ('catalog_version', 'retrieved_at')}
    return 'sha256:' + hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def load_purpose_catalog():
    try:
        raw = SNAPSHOT.read_bytes()
        if len(raw) > 1_000_000:
            raise ValueError
        catalog = PurposeCatalogV1.model_validate_json(raw)
        if catalog.product_support != 'adopted' or content_version(json.loads(raw)) != catalog.catalog_version:
            raise ValueError
        if not any(c.selectable for c in catalog.choices):
            raise ValueError
        return catalog
    except (OSError, ValueError, TypeError):
        raise PurposeCatalogUnavailable from None


def purpose_validation_catalog():
    catalog = load_purpose_catalog()
    return CodeCatalog({'main_purpose': frozenset(c.code for c in catalog.choices if c.selectable)})
