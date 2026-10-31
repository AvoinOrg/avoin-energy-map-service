"""Fixed allowlist; raw facts remain distinct from display/address edges."""
import re
from datetime import datetime
from .validation import ExportError, text_ok, validate

TEXT_COLUMNS = {
    'main': ('building_key', 'permanent_building_identifier', 'property_identifier',
             'address_key', 'address_fin', 'postal_office_fin', 'postal_code'),
    'building': ('building_key', 'permanent_building_identifier', 'property_identifier', 'modified_timestamp_utc'),
    'address': ('building_key', 'address_key', 'address_fin', 'postal_office_fin',
                'postal_code', 'municipality_number', 'modified_timestamp_utc'),
}
TABLES = {'main': 'main', 'building': 'buildings', 'address': 'addresses'}
GEOMETRY = {'main': ('point', 'polygon'), 'building': ('point',), 'address': ('point',)}


def key(value, field='building_key', nullable=False):
    if value is None and nullable:
        return None
    if not text_ok(value):
        raise ExportError('unrepresentable', field)
    return value


def fact(value, field, *, projected=True, label=False):
    if not projected:
        return {'value': None, 'status': 'not_projected', 'reason': 'projection_gap'}
    if value is None:
        return {'value': None, 'status': 'missing', 'reason': 'source_null'}
    if not text_ok(value, 512 if label else 256, True):
        raise ExportError('unrepresentable', field)
    valid = True
    if field in ('property_identifier', 'postal_code', 'municipality_number'):
        length = {'property_identifier': 14, 'postal_code': 5, 'municipality_number': 3}[field]
        valid = re.fullmatch('[0-9]{' + str(length) + '}', value) is not None
        if field == 'property_identifier' and value == '0' * 14:
            valid = False
    if field == 'modified_timestamp_utc':
        try:
            valid = datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is not None
        except ValueError:
            valid = False
    return {'value': value, 'status': 'available' if valid else 'invalid',
            'reason': None if valid else 'source_syntax'}


def address(row, main=False):
    return {'namespace': 'energy.main-display-v1' if main else 'energy.address-key-v1',
            'address_key': key(row['address_key'], 'address_key', True),
            'kind': 'main_display' if main else 'address_edge',
            'fi': fact(row['address_fin'], 'address_fin', label=True),
            'sv': fact(None, 'sv', projected=False),
            'postal_office_fi': fact(row['postal_office_fin'], 'postal_office_fin', label=True),
            'postal_office_sv': fact(None, 'postal_office_sv', projected=False),
            'postal_code': fact(row['postal_code'], 'postal_code'),
            'municipality_number': fact(row.get('municipality_number'), 'municipality_number', projected=not main)}


def project(component, row, stable_key=False):
    k = key(row['building_key'])
    geometry = 'present_unvalidated' if row['geometry_present'] else 'missing_in_scope'
    modified = fact(row.get('modified_timestamp_utc'), 'modified_timestamp_utc', projected=component != 'main')
    if component == 'address':
        result = {'building_key': k, 'source_row_id': key(row['address_key'], 'source_row_id', True) if stable_key else None,
                  'address': address(row), 'source_modified': modified, 'geometry': geometry}
    else:
        result = {'component': component, 'building_key': k, 'source_row_id': k if component == 'building' and stable_key else None,
                  'permanent': fact(row['permanent_building_identifier'], 'permanent_building_identifier'),
                  'property': fact(row['property_identifier'], 'property_identifier'),
                  'source_modified': modified, 'geometry': geometry,
                  'main_display_address': address(row, True) if component == 'main' else None}
    return validate('AddressRecord' if component == 'address' else 'IdentityRecord', result)
