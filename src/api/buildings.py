"""Public building details only; no identity-provider or personal-state dependency."""
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import ValidationError

from src.building_contract import (
    BuildingKey, CanonicalBuildingV1, ContractModel, ContractValidationError,
    DetailsV1, FieldProvenanceV1, IdentityV1, ModelV1, SelectionV1, AvailabilityV1, SourcesV1, SourceV1,
    validation_errors,
)
from src.building_resolver import BuildingSourceUnavailable, resolve_building
from src.db.session import SessionLocal


class BuildingErrorDetailV1(ContractModel):
    code: Literal['building_not_found', 'building_ambiguous', 'building_source_unavailable']
    message: str


class BuildingErrorV1(ContractModel):
    detail: BuildingErrorDetailV1


def contract_validation_response(error, *, prefix=('body',)):
    return JSONResponse(status_code=422, content={'detail': validation_errors(error, prefix)},
                        headers={'Cache-Control': 'no-cache'})


class ContractRoute(APIRoute):
    """Scoped validation adapter reusable by later personal routes."""
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except RequestValidationError as error:
                return contract_validation_response(error, prefix=())
            except (ContractValidationError, ValidationError) as error:
                return contract_validation_response(error)
        return handler


router = APIRouter(prefix='/energy-data/buildings', tags=['Canonical building details'], route_class=ContractRoute)


def building_session():
    with SessionLocal() as session:
        yield session


def public_example():
    source = SourceV1(status='missing')
    details = DetailsV1(address_fin='Example street 1', postal_code='00100', floor_area=0)
    provenance = {f'details.{k}': FieldProvenanceV1(origin='derived' if k == 'ventilation_category' else 'ryhti',
        source='ventilation_policy_v1' if k == 'ventilation_category' else f'main.{k}',
        status='available' if v is not None else 'missing') for k, v in details.model_dump().items()}
    return CanonicalBuildingV1(building_key='000-fixture-opaque-key', identity=IdentityV1(),
        details=details,
        selection=SelectionV1(geometry_status=AvailabilityV1(status='missing')),
        model=ModelV1(), sources=SourcesV1(main=SourceV1(status='available'), ryhti_building=source,
            ryhti_address=source, ryhti_polygon=source, certificate=source, code_catalog=source),
        resolved_at=datetime(2026, 10, 23, tzinfo=timezone.utc), field_provenance=provenance,
        source_availability={'source_observed_at': AvailabilityV1(status='missing', reason='unknown_source_freshness')}).model_dump(mode='json')


ERRORS = {
    404: ('building_not_found', 'Building not found'),
    409: ('building_ambiguous', 'Building identity is ambiguous'),
    503: ('building_source_unavailable', 'Building source is temporarily unavailable'),
}


def error_response(status):
    code, message = ERRORS[status]
    return JSONResponse(status_code=status, content={'detail': {'code': code, 'message': message}},
                        headers={'Cache-Control': 'no-cache'})


@router.get('/{building_key:path}', response_model=CanonicalBuildingV1, responses={
    200: {'content': {'application/json': {'example': public_example()}}},
    **{status: {'model': BuildingErrorV1, 'description': message,
        'content': {'application/json': {'example': {'detail': {'code': code, 'message': message}}}}}
       for status, (code, message) in ERRORS.items()},
})
def get_building(building_key: BuildingKey, response: Response, session=Depends(building_session)):
    try:
        result = resolve_building(session, building_key)
    except BuildingSourceUnavailable:
        return error_response(503)
    if result.status != 'available':
        return error_response(404 if result.status == 'missing' else 409)
    response.headers['Cache-Control'] = 'no-cache'
    return result.value
