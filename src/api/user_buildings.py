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
from src.db.state import StateUnavailable, state_ready, state_session
from src.user_building_contract import (
    OwnerErrorV1, SavedBuildingPageV1, SavedBuildingSummaryV1, SavedBuildingV1,
    decode_cursor, encode_cursor,
)
from src import user_buildings as service
from src import building_shares as shares
from src.building_share_contract import ShareCreatedV1
from src.api.building_examples import saved_example, saved_page_example, share_created_example
from src.building_contract import ContractModel


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
        private = path == '/energy-data/shared-buildings' or path.startswith('/energy-data/shared-buildings/') or path == '/energy-data/user/buildings' or path.startswith('/energy-data/user/buildings/') or path == '/readyz/state'
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
                return JSONResponse(status_code=422, content={'detail': validation_errors(failure, () if isinstance(failure, RequestValidationError) else ('body',))})
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
    409: 'New source identity ambiguous', 412: 'Precondition does not match',
    413: 'Body exceeds 32 KiB', 428: 'Required conditional header missing',
    503: 'Identity, state or required source/catalog unavailable', 500: 'Sanitized unexpected failure',
}.items()}
router = APIRouter(prefix='/energy-data/user/buildings', tags=['Personal saved buildings'], route_class=OwnerRoute, responses=ERRORS)
Principal = Annotated[AuthenticatedPrincipal, Depends(require_principal)]
Match = Annotated[str | None, Header(alias='If-Match', description='One strong saved-state ETag, including quotes')]
NoneMatch = Annotated[str | None, Header(alias='If-None-Match', description='Use * only for explicit initial creation')]


def observe(key, supplied_codes=False):
    """Resolve source and, if needed, codes before acquiring any write lock."""
    base, reason, catalog = None, 'source_unavailable', None
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
    return base, reason, catalog


def needs_catalog(overrides):
    return any(k in CODE_FIELDS and v is not None for k, v in sparse(overrides).items())


def rendered(row, base, reason, response):
    value = service.detail(row, base, reason)
    response.headers['ETag'] = service.etag(row)
    return value


@router.get('', response_model=SavedBuildingPageV1, responses={200: {'content': {'application/json': {'example': saved_page_example()}}}})
def list_buildings(request: Request, principal: Principal,
                   limit: Annotated[int, Query(ge=1, le=100)] = 20, cursor: str | None = None):
    if any(k not in ('limit', 'cursor') for k in request.query_params) or any(len(request.query_params.getlist(k)) > 1 for k in request.query_params):
        raise RequestValidationError([{'loc': ['query'], 'type': 'extra_forbidden', 'msg': 'Only one limit and cursor are accepted'}])
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
        if row.schema_version != 1:
            raise StateUnavailable
        result = summaries.get(row.building_key)
        current = result.value if result else None
        items.append(SavedBuildingSummaryV1(saved_id=row.saved_id, building_key=row.building_key,
            display_name=row.display_name, revision=row.revision, created_at=row.created_at, updated_at=row.updated_at,
            base_status='available' if current else 'unavailable',
            base_unavailable_reason=None if current else result.status if result else 'source_unavailable',
            current_summary=current, last_known_summary=service.stored_summary(row), share_enabled=row.share_enabled))
    return SavedBuildingPageV1(items=items, next_cursor=encode_cursor(rows[-1].updated_at, rows[-1].saved_id) if more else None)


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


@router.get('/{building_key:path}', response_model=SavedBuildingV1, responses={200: {'content': {'application/json': {'example': saved_example()}}}})
def get_building(building_key: BuildingKey, principal: Principal, response: Response):
    with state_session() as state:
        row = service.load_owned(state, principal, building_key)
    base, reason, _ = observe(building_key)
    return rendered(row, base, reason, response)


@router.put('/{building_key:path}', response_model=SavedBuildingV1, responses={status: {'model': SavedBuildingV1, 'description': description, 'content': {'application/json': {'example': saved_example()}}} for status, description in ((200, 'Replaced'), (201, 'Explicitly created'))})
def put_building(building_key: BuildingKey, value: Annotated[PersonalBuildingPutV1, Body(openapi_examples={
    'save': {'summary': 'Explicit personal save', 'value': {'schema_version': 1, 'display_name': 'Home', 'overrides': {'floor_area': 0, 'total_area': None}, 'scenario_inputs': {'resident_count': 2}}},
    'reset': {'summary': 'Replace with an intentionally empty saved document', 'value': {}},
})], principal: Principal,
                 request: Request, response: Response, if_match: Match = None, if_none_match: NoneMatch = None):
    with state_session() as state:
        # Fast preflight avoids source work on invalid preconditions; repeat
        # under lock below because another writer may commit while resolving.
        row = service.load_owned(state, principal, building_key, required=False)
        service.preconditions(request.headers, row, create=True)
        base, reason, catalog = observe(building_key, needs_catalog(value.overrides))
        value = replace_personal(value, catalog)
        row = service.load_owned(state, principal, building_key, lock=True, required=False)
        service.preconditions(request.headers, row, create=True)
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
        result = rendered(row, base, reason, response)
        state.commit()
    return result


@router.patch('/{building_key:path}', response_model=SavedBuildingV1, responses={200: {'content': {'application/json': {'example': saved_example()}}}})
def patch_building(building_key: BuildingKey, value: Annotated[PersonalBuildingPatchV1, Body(openapi_examples={
    'edit': {'summary': 'Clear name, mark an area unknown, and restore a source fallback', 'value': {'display_name': None, 'set': {'overrides': {'heated_net_area': None}}, 'unset': ['overrides.floor_area']}},
})], principal: Principal,
                   request: Request, response: Response, if_match: Match = None, if_none_match: NoneMatch = None):
    with state_session() as state:
        row = service.load_owned(state, principal, building_key)
        service.preconditions(request.headers, row)
        base, reason, catalog = observe(building_key, needs_catalog(value.set.overrides))
        validate_new_codes(value.set.overrides, catalog, prefix=('set', 'overrides'))
        row = service.locked_owned(state, principal, building_key, request.headers)
        updated = apply_personal_patch(service.stored_document(row), value, catalog)
        service.stored_summary(row)
        service.bump_revision(row)
        service.store_document(row, updated, base)
        result = rendered(row, base, reason, response)
        state.commit()
    return result


@router.delete('/{building_key:path}', status_code=204)
def delete_building(building_key: BuildingKey, principal: Principal, request: Request,
                    if_match: Match = None, if_none_match: NoneMatch = None):
    with state_session() as state:
        row = service.locked_owned(state, principal, building_key, request.headers)
        service.bump_revision(row)
        tag = service.etag(row)
        state.delete(row)
        state.commit()
    return Response(status_code=204, headers={'ETag': tag})


def readiness():
    available = state_ready()
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
