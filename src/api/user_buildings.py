"""Authenticated owner API. Source snapshots never use the state writer."""
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.routing import Match as RouteMatch
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from pydantic import ValidationError

from src.api.auth import AuthenticatedPrincipal, require_principal
from src.building_contract import (
    BuildingKey, CatalogUnavailable, CODE_FIELDS, ContractValidationError,
    PERSONAL_PAYLOAD_BYTES, PersonalBuildingPatchV1, PersonalBuildingPutV1,
    apply_personal_patch, replace_personal, sparse, validate_new_codes, validation_errors,
)
from src.building_resolver import BuildingSourceUnavailable, load_code_catalog, resolve_building, resolve_building_summaries
from src.db.session import SessionLocal
from src.db.state import StateUnavailable, StateSchemaUpgradeRequired, require_personal_schema, state_ready, state_session
from src.user_building_contract import (
    OwnerErrorV1, SavedBuildingPageV1, SavedBuildingSummaryV1, SavedBuildingV1,
    SavedBuildingPageV2, SavedBuildingSummaryV2, SavedBuildingV2,
    decode_cursor, encode_cursor,
)
from src import user_buildings as service
from src import building_shares as shares
from src.building_share_contract import ShareCreatedV1
from src.api.building_examples import saved_example, saved_page_example, share_created_example
from src.purpose_catalog import PurposeCatalogUnavailable, purpose_validation_catalog
from src.building_contract import CodeCatalog, ContractModel
from src.personal_property_contract import PersonalBuildingPutV2, PersonalBuildingPatchV2, replace_v2, patch_v2


class EmptyShareRequest(ContractModel):
    pass


def reject_share_query(request):
    if request.query_params:
        raise RequestValidationError([{'loc': ['query'], 'type': 'extra_forbidden', 'msg': 'Share management accepts no query parameters'}])


def error(status, code, message):
    return JSONResponse(status_code=status, content={'detail': {'code': code, 'message': message}}, headers={'Cache-Control': 'no-store'})


class PrivateBoundary:
    """Pre-parse bounded bodies and no-store even outside a route handler."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        private = path == '/energy-data/shared-buildings' or path.startswith('/energy-data/shared-buildings/') or path == '/energy-data/user/buildings' or path.startswith('/energy-data/user/buildings/') or path == '/readyz/state' or path == '/energy-data/building-purpose-choices'
        if scope['type'] != 'http' or not private:
            return await self.app(scope, receive, send)
        started = False

        async def private_send(message):
            nonlocal started
            if message['type'] == 'http.response.start':
                started = True
                message = {**message, 'headers': [(k, v) for k, v in message['headers'] if k.lower() != b'cache-control'] + [(b'cache-control', b'no-store')]}
            await send(message)

        try:
            if scope['method'] in ('PUT', 'PATCH', 'POST'):
                body = bytearray()
                while True:
                    message = await receive()
                    if message['type'] == 'http.disconnect':
                        return
                    chunk = message.get('body', b'')
                    if len(body) + len(chunk) > PERSONAL_PAYLOAD_BYTES:
                        return await error(413, 'payload_too_large', 'Personal payload exceeds 32 KiB UTF-8')(scope, receive, private_send)
                    body.extend(chunk)
                    if not message.get('more_body', False):
                        break
                delivered = False

                async def bounded_receive():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                    return await receive()

                await self.app(scope, bounded_receive, private_send)
            else:
                await self.app(scope, receive, private_send)
        except Exception:
            if started:
                raise
            await error(500, 'personal_api_error', 'Personal building request failed')(scope, receive, private_send)


class OwnerRoute(APIRoute):
    def matches(self, scope):
        # Starlette matches decoded paths; the literal operation suffix must
        # remain distinguishable from an encoded slash in an opaque key.
        if self.path.endswith('/share') and not scope.get('raw_path', b'').endswith(b'/share'):
            return RouteMatch.NONE, {}
        return super().matches(scope)

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except (RequestValidationError, ContractValidationError) as failure:
                issues = validation_errors(failure, () if isinstance(failure, RequestValidationError) else ('body',))
                # Untagged union preserves v1's omitted version. Expose only the
                # selected request model's field locations, never union branches.
                variant = 'V2' if request.query_params.get('personal_schema_version') == '2' else 'V1'
                names = ('function-after[bounded(), PersonalBuildingPutV1]', 'function-after[bounded(), PersonalBuildingPutV2]',
                         'function-after[operations(), PersonalBuildingPatchV1]', 'function-after[operations(), PersonalBuildingPatchV2]')
                selected_issues = []
                for issue in issues:
                    location = issue['loc']
                    if len(location) > 1 and location[1] in names:
                        if not location[1].endswith(variant + ']'):
                            continue
                        issue = dict(issue, loc=[location[0], *location[2:]])
                    selected_issues.append(issue)
                return JSONResponse(status_code=422, content={'detail': selected_issues})
            except PurposeCatalogUnavailable:
                return error(503, 'purpose_catalog_unavailable', 'Supported purpose choices are temporarily unavailable')
            except StateSchemaUpgradeRequired:
                return error(503, 'personal_schema_upgrade_required', 'Personal schema upgrade is required for version 2 writes')
            except service.OwnerFailure as failure:
                return error(failure.status, failure.code, failure.message)
            except (StateUnavailable, CatalogUnavailable, ValidationError):
                return error(503, 'personal_state_unavailable', 'Personal building state is temporarily unavailable')
            except HTTPException:
                raise
            except Exception:
                # Keep sanitized route failures inside CORS; the outer boundary
                # still protects framework/middleware failures and preflights.
                return error(500, 'personal_api_error', 'Personal building request failed')
        return handler


ERRORS = {status: {'model': OwnerErrorV1, 'description': description} for status, description in {
    400: 'Malformed or contradictory conditional headers', 401: 'Valid Bearer token required',
    403: 'Configured permission missing', 404: 'Owned record or new source not found',
    409: 'Source identity ambiguous or personal document version conflict', 412: 'Precondition does not match',
    413: 'Body exceeds 32 KiB', 428: 'Required conditional header missing',
    503: 'Identity, state or required source/catalog unavailable', 500: 'Sanitized unexpected failure',
}.items()}
router = APIRouter(prefix='/energy-data/user/buildings', tags=['Personal saved buildings'], route_class=OwnerRoute, responses=ERRORS)
readiness_router = APIRouter(tags=['Readiness'], route_class=OwnerRoute)
Principal = Annotated[AuthenticatedPrincipal, Depends(require_principal)]
Match = Annotated[str | None, Header(alias='If-Match', description='One strong saved-state ETag, including quotes')]
NoneMatch = Annotated[str | None, Header(alias='If-None-Match', description='Use * only for explicit initial creation')]


def select_personal_version(request: Request,
        personal_schema_version: Annotated[int, Query(ge=1, le=2)] = 1):
    allowed = {'personal_schema_version'}
    if request.scope.get('route') and request.scope['route'].path == '/energy-data/user/buildings':
        allowed |= {'limit', 'cursor'}
    if any(k not in allowed or len(request.query_params.getlist(k)) != 1 for k in request.query_params):
        raise RequestValidationError([{'loc': ['query'], 'type': 'extra_forbidden', 'msg': 'Unsupported or duplicate query parameter'}])
    raw = request.query_params.get('personal_schema_version', '1')
    if raw not in ('1', '2'):
        raise RequestValidationError([{'loc': ['query', 'personal_schema_version'], 'type': 'personal_schema_version', 'msg': 'Expected 1 or 2'}])
    return int(raw)


PersonalVersion = Annotated[int, Depends(select_personal_version)]
SavedResponse = SavedBuildingV1 | SavedBuildingV2
PageResponse = SavedBuildingPageV1 | SavedBuildingPageV2


def require_body_version(value, version):
    if value.schema_version != version:
        raise ContractValidationError([{'loc': ['schema_version'], 'type': 'schema_version', 'msg': 'Body version must agree with personal_schema_version'}])


def observe(key, supplied_codes=False):
    """Resolve source and, if needed, codes before acquiring any write lock."""
    base, reason, catalog = None, 'source_unavailable', None
    supplied_codes = set(supplied_codes) if supplied_codes else set()
    purpose = purpose_validation_catalog() if 'main_purpose' in supplied_codes else None
    supplied_codes.discard('main_purpose')
    try:
        with SessionLocal() as source:
            result = resolve_building(source, key)
            base, reason = result.value, result.status
            if supplied_codes:
                catalog = load_code_catalog(source)
    except (BuildingSourceUnavailable, SQLAlchemyError):
        # A source outage is tolerated for existing records. Codes still need
        # independent current validation if this input supplies any.
        if supplied_codes:
            try:
                with SessionLocal() as source:
                    source.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
                    source.execute(text("SET LOCAL statement_timeout = '10s'"))
                    source.execute(text("SET LOCAL lock_timeout = '2s'"))
                    catalog = load_code_catalog(source)
            except SQLAlchemyError:
                raise CatalogUnavailable from None
    if purpose is not None:
        catalog = CodeCatalog({**(catalog.codes if catalog else {}), **purpose.codes})
    return base, reason, catalog


def needs_catalog(overrides):
    return {k for k, v in sparse(overrides).items() if k in CODE_FIELDS and v is not None}


def rendered(row, base, reason, response, version=1):
    value = service.detail(row, base, reason, version)
    response.headers['ETag'] = service.etag(row)
    return value


@router.get('', response_model=PageResponse, responses={200: {'content': {'application/json': {'example': saved_page_example()}}}})
def list_buildings(request: Request, principal: Principal, version: PersonalVersion,
                   limit: Annotated[int, Query(ge=1, le=100)] = 20, cursor: str | None = None):
    try:
        boundary = decode_cursor(cursor)
    except ValueError:
        raise RequestValidationError([{'loc': ['query', 'cursor'], 'type': 'invalid_cursor', 'msg': 'Invalid cursor'}]) from None
    with state_session() as state:
        rows = state.execute(service.page_query(principal, limit, boundary)).all()
    more, rows = len(rows) > limit, rows[:limit]
    summaries = {}
    if rows:
        try:
            with SessionLocal() as source:
                summaries = resolve_building_summaries(source, [r.building_key for r in rows])
        except (BuildingSourceUnavailable, SQLAlchemyError):
            pass
    items = []
    for row in rows:
        if row.schema_version not in (1, 2):
            raise StateUnavailable
        result = summaries.get(row.building_key)
        current = result.value if result else None
        summary_model = SavedBuildingSummaryV1 if version == 1 else SavedBuildingSummaryV2
        version_fields = {} if version == 1 else {'document_schema_version': row.schema_version}
        items.append(summary_model(**version_fields, saved_id=row.saved_id, building_key=row.building_key,
            display_name=row.display_name, revision=row.revision, created_at=row.created_at, updated_at=row.updated_at,
            base_status='available' if current else 'unavailable',
            base_unavailable_reason=None if current else result.status if result else 'source_unavailable',
            current_summary=current, last_known_summary=service.stored_summary(row), share_enabled=row.share_enabled))
    return (SavedBuildingPageV1 if version == 1 else SavedBuildingPageV2)(items=items, next_cursor=encode_cursor(rows[-1].updated_at, rows[-1].saved_id) if more else None)


@router.post('/{building_key:path}/share', status_code=201, response_model=ShareCreatedV1, responses={201: {'content': {'application/json': {'example': share_created_example()}}}})
def create_share(building_key: BuildingKey, principal: Principal, request: Request,
                 response: Response, value: EmptyShareRequest | None = Body(default=None),
                 if_match: Match = None, if_none_match: NoneMatch = None):
    reject_share_query(request)
    with state_session() as state:
        row = service.locked_owned(state, principal, building_key, request.headers)
        token = shares.rotate(state, row)
        result = ShareCreatedV1(share_token=token, resolver_path=shares.RESOLVER_PREFIX + token,
                               revision=row.revision, updated_at=row.updated_at)
        tag = service.etag(row)
        state.commit()
    response.headers['ETag'] = tag
    return result


@router.delete('/{building_key:path}/share', status_code=204)
def delete_share(building_key: BuildingKey, principal: Principal, request: Request,
                 if_match: Match = None, if_none_match: NoneMatch = None):
    reject_share_query(request)
    with state_session() as state:
        row = service.locked_owned(state, principal, building_key, request.headers)
        shares.revoke(state, row)
        tag = service.etag(row)
        state.commit()
    return Response(status_code=204, headers={'ETag': tag})


@router.get('/{building_key:path}', response_model=SavedResponse, responses={200: {'content': {'application/json': {'example': saved_example()}}}})
def get_building(building_key: BuildingKey, principal: Principal, response: Response, version: PersonalVersion):
    with state_session() as state:
        row = service.load_owned(state, principal, building_key)
    base, reason, _ = observe(building_key)
    return rendered(row, base, reason, response, version)


@router.put('/{building_key:path}', response_model=SavedResponse, responses={status: {'model': SavedResponse, 'description': description, 'content': {'application/json': {'example': saved_example()}}} for status, description in ((200, 'Replaced'), (201, 'Explicitly created'))})
def put_building(building_key: BuildingKey, value: Annotated[PersonalBuildingPutV1 | PersonalBuildingPutV2, Body(openapi_examples={
    'save': {'summary': 'Explicit personal save', 'value': {'schema_version': 1, 'display_name': 'Home', 'overrides': {'floor_area': 0, 'total_area': None}, 'scenario_inputs': {'resident_count': 2}}},
    'reset': {'summary': 'Replace with an intentionally empty saved document', 'value': {}},
})], principal: Principal,
                 request: Request, response: Response, version: PersonalVersion, if_match: Match = None, if_none_match: NoneMatch = None):
    require_body_version(value, version)
    with state_session() as state:
        # Fast preflight avoids source work on invalid preconditions; repeat
        # under lock below because another writer may commit while resolving.
        row = service.load_owned(state, principal, building_key, required=False)
        service.preconditions(request.headers, row, create=True)
        service.require_document_version(row, version)
        require_personal_schema(state, version)
        base, reason, catalog = observe(building_key, needs_catalog(value.overrides))
        value = (replace_personal if version == 1 else replace_v2)(value, catalog)
        row = service.load_owned(state, principal, building_key, lock=True, required=False)
        service.preconditions(request.headers, row, create=True)
        service.require_document_version(row, version)
        require_personal_schema(state, version)
        if row is None:
            if base is None:
                status = {'missing': 404, 'ambiguous': 409, 'source_unavailable': 503}[reason]
                return error(status, 'building_' + reason, 'A current unambiguous source building is required for a new save')
            row = service.create_owned(state, principal, building_key, value, base)
            response.status_code = 201
        else:
            service.stored_document(row)
            service.stored_summary(row)
            service.bump_revision(row)
            service.store_document(row, value, base)
        result = rendered(row, base, reason, response, version)
        state.commit()
    return result


@router.patch('/{building_key:path}', response_model=SavedResponse, responses={200: {'content': {'application/json': {'example': saved_example()}}}})
def patch_building(building_key: BuildingKey, value: Annotated[PersonalBuildingPatchV1 | PersonalBuildingPatchV2, Body(openapi_examples={
    'edit': {'summary': 'Clear name, mark an area unknown, and restore a source fallback', 'value': {'display_name': None, 'set': {'overrides': {'heated_net_area': None}}, 'unset': ['overrides.floor_area']}},
})], principal: Principal,
                   request: Request, response: Response, version: PersonalVersion, if_match: Match = None, if_none_match: NoneMatch = None):
    require_body_version(value, version)
    with state_session() as state:
        row = service.load_owned(state, principal, building_key)
        service.preconditions(request.headers, row)
        service.require_document_version(row, version)
        require_personal_schema(state, version)
        base, reason, catalog = observe(building_key, needs_catalog(value.set.overrides))
        validate_new_codes(value.set.overrides, catalog, prefix=('set', 'overrides'))
        row = service.locked_owned(state, principal, building_key, request.headers)
        service.require_document_version(row, version)
        require_personal_schema(state, version)
        updated = (apply_personal_patch if version == 1 else patch_v2)(service.stored_document(row), value, catalog)
        service.stored_summary(row)
        service.bump_revision(row)
        service.store_document(row, updated, base)
        result = rendered(row, base, reason, response, version)
        state.commit()
    return result


@router.delete('/{building_key:path}', status_code=204)
def delete_building(building_key: BuildingKey, principal: Principal, request: Request, version: PersonalVersion,
                    if_match: Match = None, if_none_match: NoneMatch = None):
    with state_session() as state:
        row = service.locked_owned(state, principal, building_key, request.headers)
        service.require_document_version(row, version)
        require_personal_schema(state, version)
        service.bump_revision(row)
        tag = service.etag(row)
        state.delete(row)
        state.commit()
    return Response(status_code=204, headers={'ETag': tag})


@readiness_router.get('/readyz/state')
def readiness(version: PersonalVersion):
    available = state_ready(version)
    return JSONResponse(status_code=200 if available else 503,
                        content={'status': 'ready' if available else 'personal_state_unavailable'})


# Document validators even for bodyless responses. Collection summaries contain
# revisions, but a caller loads detail before a conditional mutation.
for route in router.routes:
    if route.endpoint is list_buildings:
        continue
    statuses = {route.status_code or 200} | ({201} if route.endpoint is put_building else set())
    for status in statuses:
        route.responses.setdefault(status, {}).setdefault('headers', {}).update({
            'ETag': {'description': 'Strong saved incarnation/revision validator (terminal receipt on owner DELETE)', 'schema': {'type': 'string'}},
            'Cache-Control': {'schema': {'type': 'string', 'const': 'no-store'}},
        })
