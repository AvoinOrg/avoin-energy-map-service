"""Seed historical storage revisions without requiring a legacy application contract."""
from src.personal_utilities_contract import PersonalBuildingPutV5


def archive_document(data):
    version = data['schema_version']
    document = PersonalBuildingPutV5.model_validate({**data, 'schema_version': 5}, context={'stored_document': True})
    return document.model_copy(update={'schema_version': version})
