"""Supported personal HVAC inputs with explicitly deferred coefficients."""
from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError

from src.api.user_buildings import OwnerRoute
from src.hvac_catalog import HvacCatalogV2, load_hvac_catalog

router = APIRouter(tags=['Building reference data'], route_class=OwnerRoute)


@router.get('/energy-data/building-hvac-choices', response_model=HvacCatalogV2,
            responses={503: {'description': 'Required bundled HVAC pin unavailable'}})
def hvac_choices(request: Request):
    if request.query_params:
        raise RequestValidationError([{'loc': ['query'], 'type': 'extra_forbidden',
            'msg': 'HVAC discovery accepts no query parameters'}])
    return load_hvac_catalog()
