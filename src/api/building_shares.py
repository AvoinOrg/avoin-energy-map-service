"""Anonymous read-only capabilities; deliberately no require_principal dependency."""
from fastapi import APIRouter

from src.api.building_examples import shared_example
from src.api.user_buildings import OwnerRoute, error, observe
from src.building_share_contract import SharedBuildingV1, shared_projection
from src.building_shares import ShareNotFound, TOKEN_PATTERN, lookup
from src.db.state import state_session
from src.user_building_contract import OwnerErrorV1

router = APIRouter(prefix='/energy-data/shared-buildings', tags=['Shared buildings'], route_class=OwnerRoute)


@router.get('', include_in_schema=False)
@router.get('/', include_in_schema=False)
def missing_shared_token():
    return error(404, 'shared_building_not_found', 'Shared building not found')


@router.get('/{share_token:path}', response_model=SharedBuildingV1, responses={
    200: {'content': {'application/json': {'example': shared_example()}}},
    404: {'model': OwnerErrorV1, 'description': 'Share not found'},
    503: {'model': OwnerErrorV1, 'description': 'Personal state unavailable'},
})
def get_shared_building(share_token: str):
    try:
        if not TOKEN_PATTERN.fullmatch(share_token):
            raise ShareNotFound
        with state_session(read_only=True) as state:
            row = lookup(state, share_token)
            key = row.building_key
        base, reason, _ = observe(key)
        # Fresh READ COMMITTED statement after potentially slow source work is
        # the read linearization point. Reuse latest saved overrides/revision.
        with state_session(read_only=True) as state:
            row = lookup(state, share_token)
            return shared_projection(row, base, reason)
    except ShareNotFound:
        return error(404, 'shared_building_not_found', 'Shared building not found')
