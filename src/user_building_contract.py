"""Closed owner-only metadata and bounded keyset cursor contract."""
import base64
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from src.building_contract import (
    BuildingKey, BuildingSummaryV1, ContractModel, DisplayName,
    LastKnownBuildingSummaryV1, PersonalBuildingProjectionV1, UtcTime, VersionedV1,
)


class SavedBuildingV1(PersonalBuildingProjectionV1):
    last_known_summary: LastKnownBuildingSummaryV1
    saved_id: UUID
    display_name: DisplayName | None
    revision: int = Field(ge=1)
    created_at: UtcTime
    updated_at: UtcTime
    share_enabled: bool = False


class SavedBuildingSummaryV1(VersionedV1):
    saved_id: UUID
    building_key: BuildingKey
    display_name: DisplayName | None
    revision: int = Field(ge=1)
    created_at: UtcTime
    updated_at: UtcTime
    share_enabled: bool = False
    base_status: Literal['available', 'unavailable']
    base_unavailable_reason: Literal['missing', 'ambiguous', 'source_unavailable'] | None
    current_summary: BuildingSummaryV1 | None
    last_known_summary: LastKnownBuildingSummaryV1


class SavedBuildingPageV1(VersionedV1):
    items: list[SavedBuildingSummaryV1]
    next_cursor: str | None


class OwnerErrorDetailV1(ContractModel):
    code: str
    message: str


class OwnerErrorV1(ContractModel):
    detail: OwnerErrorDetailV1


class CursorV1(VersionedV1):
    updated_at: UtcTime
    saved_id: UUID


def encode_cursor(updated_at: datetime, saved_id: UUID):
    raw = CursorV1(updated_at=updated_at, saved_id=saved_id).model_dump_json().encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def decode_cursor(value):
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        raise ValueError('Invalid cursor')
    try:
        raw = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
        result = CursorV1.model_validate_json(raw)
        if encode_cursor(result.updated_at, result.saved_id) != value:
            raise ValueError
        return result
    except Exception:
        raise ValueError('Invalid cursor') from None
