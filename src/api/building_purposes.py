"""Public reference catalog, independent of owner state and HVAC data."""
from fastapi import APIRouter, Request, Response
from fastapi.exceptions import RequestValidationError

from src.api.user_buildings import OwnerRoute
from src.purpose_catalog import PurposeCatalogV1, load_purpose_catalog

router = APIRouter(tags=['Building reference data'], route_class=OwnerRoute)


@router.get('/energy-data/building-purpose-choices', response_model=PurposeCatalogV1,
            responses={503: {'description': 'Adopted purpose snapshot unavailable'}})
def purpose_choices(request: Request, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    if request.query_params:
        raise RequestValidationError([{'loc': ['query'], 'type': 'extra_forbidden',
            'msg': 'Purpose discovery accepts no query parameters'}])
    return load_purpose_catalog()
