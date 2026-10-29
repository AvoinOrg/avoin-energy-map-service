"""Anonymous JSON discovery, separate from the canonical opaque-key namespace."""
from functools import lru_cache
import json
import logging

from fastapi import APIRouter, Depends
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.building_lookup import LookupFailure, lookup
from src.building_lookup_contract import (
    BuildingLookupErrorV1, BuildingLookupPageV1, BuildingLookupRequestV1,
    LookupErrorDetailV1, REQUEST_BYTES,
)
from src.db.session import local_db_url

PREFIX = '/energy-data/building-lookup'


def error_response(failure):
    body = BuildingLookupErrorV1(detail=LookupErrorDetailV1(code=failure.code,
        message=failure.message, mode=failure.mode, component=failure.component))
    return JSONResponse(status_code=failure.status, content=body.model_dump(mode='json'),
                        headers={'Cache-Control': 'no-store'})


class LookupBoundary:
    """Own the body cap and final no-store, including CORS/framework responses."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        if scope['type'] != 'http' or not (path == PREFIX or path.startswith(PREFIX + '/')):
            return await self.app(scope, receive, send)
        started = False

        async def no_store(message):
            nonlocal started
            if message['type'] == 'http.response.start':
                started = True
                message = {**message, 'headers': [(k, v) for k, v in message.get('headers', [])
                           if k.lower() != b'cache-control'] + [(b'cache-control', b'no-store')]}
            await send(message)

        try:
            if scope['method'] == 'POST':
                headers = {k.lower(): v for k, v in scope.get('headers', [])}
                media = headers.get(b'content-type', b'').split(b';', 1)[0].strip().lower()
                if media != b'application/json' or headers.get(b'content-encoding', b'identity') != b'identity':
                    return await error_response(LookupFailure('lookup_unsupported_media_type'))(scope, receive, no_store)
                if scope.get('query_string'):
                    return await error_response(LookupFailure('lookup_invalid_request'))(scope, receive, no_store)
                length = headers.get(b'content-length')
                if length and (not length.isdigit() or len(length) > 10 or int(length) > REQUEST_BYTES):
                    return await error_response(LookupFailure('lookup_payload_too_large'))(scope, receive, no_store)
                body = bytearray()
                while True:
                    message = await receive()
                    if message['type'] == 'http.disconnect':
                        return
                    chunk = message.get('body', b'')
                    if len(body) + len(chunk) > REQUEST_BYTES:
                        return await error_response(LookupFailure('lookup_payload_too_large'))(scope, receive, no_store)
                    body.extend(chunk)
                    if not message.get('more_body', False):
                        break
                try:
                    # Reject malformed UTF-8/JSON before the framework can turn
                    # a decoding failure into a different or unsanitized error.
                    def invalid_constant(_value):
                        raise ValueError()
                    json.loads(body.decode('utf-8'), parse_constant=invalid_constant)
                except (ValueError, UnicodeError, RecursionError):
                    return await error_response(LookupFailure('lookup_invalid_request'))(scope, receive, no_store)
                delivered = False

                async def bounded_receive():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                    return await receive()

                await self.app(scope, bounded_receive, no_store)
            else:
                await self.app(scope, receive, no_store)
        except Exception:
            if started:
                raise
            await error_response(LookupFailure('lookup_internal_error'))(scope, receive, no_store)


class LookupRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except RequestValidationError:
                return error_response(LookupFailure('lookup_invalid_request'))
            except LookupFailure as failure:
                return error_response(failure)
            except Exception:
                return error_response(LookupFailure('lookup_internal_error'))
        return handler


@lru_cache(maxsize=1)
def source_sessions():
    # Same analytical configuration, isolated connection/time/log policy. This
    # dependency returns a factory without connecting before DTO validation.
    logging.getLogger('sqlalchemy.engine.Engine.building_lookup').disabled = True
    engine = create_engine(local_db_url(), echo=False, hide_parameters=True,
        logging_name='building_lookup', pool_timeout=1, pool_pre_ping=False,
        connect_args={'connect_timeout': 2,
                      'options': '-c default_transaction_read_only=on -c statement_timeout=3000 -c lock_timeout=1000'})
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


router = APIRouter(prefix=PREFIX, tags=['Building lookup v1'], route_class=LookupRoute)


@router.post('', response_model=BuildingLookupPageV1,
    summary='Discover partial Main-scope permanent-ID candidates',
    description='Read-only anonymous JSON lookup. Address/property inputs validate but currently return '
                'lookup_capability_unavailable. Canonical exact-key GET independently confirms each selection.', responses={
    status: {'model': BuildingLookupErrorV1, 'description': description} for status, description in {
        409: 'Identity conflict or invalidated page', 413: 'Body exceeds 16 KiB',
        415: 'JSON media type required', 422: 'Invalid request or bounded query too broad',
        503: 'Unsupported source capability, outage, timeout or source contract failure',
        500: 'Sanitized unexpected failure',
    }.items()
})
def post_lookup(request: BuildingLookupRequestV1, sessions=Depends(source_sessions)):
    result = lookup(request, sessions)
    return Response(content=result.model_dump_json(exclude_none=False), media_type='application/json',
                    headers={'Cache-Control': 'no-store'})
