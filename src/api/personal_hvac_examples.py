"""Synthetic HVAC inputs used by the current personal examples."""
from src.hvac_catalog import DEFAULTS_VERSION


def full_systems_example():
    primary = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
    return {'defaults_version': DEFAULTS_VERSION,
        'heating': {'sources': [
            {'source_id': primary, 'source_code': 'ryhti:heating_energy_source:01', 'share_percent': 65.5},
            {'source_id': 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', 'source_code': 'ryhti:heating_energy_source:1101', 'share_percent': 0},
            {'source_id': 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', 'source_code': 'ryhti:heating_energy_source:04', 'share_percent': None}],
            'primary_source_id': primary},
        'heated_quantities': {'total_area_m2': 0, 'total_volume_m3': None, 'residential_area_m2': 120.25, 'residential_volume_m3': 360.5},
        'cooling': {'kind': 'selected', 'codes': ['energy:cooling:district', 'energy:cooling:heat_pump']},
        'ventilation': {'kind': 'multiple', 'codes': ['0', '8']},
        'equipment': {'geothermal_installed': False, 'solar_panels_installed': True, 'energy_storage_ready': None}}
