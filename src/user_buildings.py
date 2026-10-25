"""Owner-scoped transaction primitives; caller owns commit (also for sharing)."""
from datetime import datetime, timedelta, timezone
import json
import re
from uuid import uuid4

from sqlalchemy import func, select, tuple_
from sqlalchemy.exc import IntegrityError
from pydantic import ValidationError

from src.building_contract import (
    AvailabilityV1, LastKnownBuildingSummaryV1, PersonalBuildingPutV1,
    personal_document, project_building,
)
from src.db.state import StateUnavailable
from src.db.user_building_models import UserBuilding as Row, UserBuildingShare as Share
from src.user_building_contract import SavedBuildingV1


class OwnerFailure(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def fail(status):
    code, message = {
        400: ('invalid_precondition', 'Use one exact strong If-Match tag or creation If-None-Match: *'),
        404: ('saved_building_not_found', 'Saved building not found'),
        412: ('precondition_failed', 'Saved building changed; reload before saving'),
        428: ('precondition_required', 'A conditional write header is required'),
    }[status]
    raise OwnerFailure(status, code, message)


def owner_scope(principal):
    # Hash expressions bound index size; exact predicates always authorize.
    return (func.md5(Row.owner_issuer) == func.md5(principal.issuer),
            func.md5(Row.owner_subject) == func.md5(principal.subject),
            Row.owner_issuer == principal.issuer, Row.owner_subject == principal.subject)


def owned_query(principal, key):
    return select(Row).where(*owner_scope(principal), Row.building_key == key)


def load_owned(session, principal, key, *, lock=False, required=True):
    query = owned_query(principal, key)
    row = session.execute(query.with_for_update().execution_options(populate_existing=True) if lock else query).scalar_one_or_none()
    if row is not None and lock:
        # A waiter may have selected before the preceding share commit. Re-read
        # the EXISTS after acquiring its saved-row lock under READ COMMITTED.
        row.share_enabled = session.scalar(select(select(Share.saved_id).where(Share.saved_id == row.saved_id).exists()))
    if row is None and required:
        fail(404)
    return row


def etag(row):
    return f'"ub1:{row.saved_id}:{row.revision}"'


def preconditions(headers, row, *, create=False):
    match, none = headers.getlist('if-match'), headers.getlist('if-none-match')
    if len(match) > 1 or len(none) > 1 or (match and none):
        fail(400)
    if none and none != ['*']:
        fail(400)
    if match and (len(match[0]) > 128 or not re.fullmatch(r'"[!#-~]+"', match[0])):
        fail(400)
    if row is None:
        if not create:
            fail(404)
        if match:
            fail(412)
        if not none:
            fail(428)
    else:
        if none:
            fail(412)
        if not match:
            fail(428)
        if match[0] != etag(row):
            fail(412)


def locked_owned(session, principal, key, headers):
    """Reusable lock + exact incarnation/revision check for future share writes."""
    row = load_owned(session, principal, key, lock=True)
    preconditions(headers, row)
    return row


def stored_document(row):
    try:
        return PersonalBuildingPutV1.model_validate_json(json.dumps({
            'schema_version': row.schema_version, 'display_name': row.display_name,
            'overrides': row.overrides, 'scenario_inputs': row.scenario_inputs}))
    except (ValidationError, ValueError, TypeError):
        raise StateUnavailable from None


def stored_summary(row):
    try:
        result = LastKnownBuildingSummaryV1.model_validate_json(json.dumps(row.last_known_summary))
        if result.building_key != row.building_key:
            raise ValueError
        return result
    except (ValidationError, ValueError, TypeError):
        raise StateUnavailable from None


def capture_summary(base, now):
    """Small adapter matching the canonical batch's address/location contract."""
    availability = {}
    for key in ('address_fin', 'postal_code', 'postal_office_fin'):
        item = base.field_provenance[f'details.{key}']
        availability[key] = AvailabilityV1(status='missing' if item.status == 'not_projected' else item.status, reason=item.reason)
    # Summary location remains usable even when full geometry serialization is
    # unsupported (e.g. a valid source surface with extra coordinate axes).
    availability['location'] = AvailabilityV1(status='available') if base.location else base.selection.geometry_status
    availability['main'] = AvailabilityV1(status=base.sources.main.status)
    availability['source_observed_at'] = base.source_availability['source_observed_at']
    return LastKnownBuildingSummaryV1(building_key=base.building_key,
        address_fin=base.details.address_fin, postal_code=base.details.postal_code,
        postal_office_fin=base.details.postal_office_fin, location=base.location,
        source_observed_at=base.source_observed_at, source_availability=availability, captured_at=now)


def bump_revision(row):
    row.revision += 1
    row.updated_at = max(datetime.now(timezone.utc), row.updated_at + timedelta(microseconds=1))


def store_document(row, value, base):
    document = personal_document(value)
    for key in ('schema_version', 'display_name', 'overrides', 'scenario_inputs'):
        setattr(row, key, document[key])
    if base is not None:
        row.last_known_summary = capture_summary(base, row.updated_at).model_dump(mode='json')


def create_owned(session, principal, key, value, base):
    now = datetime.now(timezone.utc)
    row = Row(saved_id=uuid4(), owner_issuer=principal.issuer, owner_subject=principal.subject,
              building_key=key, revision=1, created_at=now, updated_at=now)
    store_document(row, value, base)
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as error:
        if getattr(error.orig.diag, 'constraint_name', None) != 'uq_user_buildings_owner_key':
            raise
        # Exact owner comparison checks the hash collision case. Never translate
        # an unrelated/colliding identity conflict into an owned-resource result.
        if load_owned(session, principal, key, required=False) is not None:
            fail(412)
        raise StateUnavailable from None
    return row


def detail(row, base, reason='missing'):
    document = stored_document(row)
    projection = project_building(base, document.overrides, document.scenario_inputs,
        building_key=row.building_key, last_known_summary=stored_summary(row), base_unavailable_reason=reason)
    return SavedBuildingV1(**{name: getattr(projection, name) for name in type(projection).model_fields},
        saved_id=row.saved_id, display_name=document.display_name, revision=row.revision,
        created_at=row.created_at, updated_at=row.updated_at, share_enabled=bool(row.share_enabled))


SUMMARY_COLUMNS = (Row.saved_id, Row.building_key, Row.display_name, Row.schema_version,
                   Row.revision, Row.created_at, Row.updated_at, Row.last_known_summary, Row.share_enabled.label("share_enabled"))


def page_query(principal, limit, cursor):
    query = select(*SUMMARY_COLUMNS).where(*owner_scope(principal))
    if cursor:
        query = query.where(tuple_(Row.updated_at, Row.saved_id) < tuple_(cursor.updated_at, cursor.saved_id))
    return query.order_by(Row.updated_at.desc(), Row.saved_id.desc()).limit(limit + 1)
