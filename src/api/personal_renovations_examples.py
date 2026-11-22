"""Synthetic completed renovation inputs, with added insulation in millimetres."""


def completed_works_example():
    return {'facade': {'completed': False}, 'windows': None,
        'plumbing': {'completed': True}, 'roof': {},
        'top_floor_insulation': {'completed': True, 'execution_year': 2025, 'added_insulation_thickness_mm': 230.5},
        'drainage': {'completed': None}, 'energy_smart_solutions': {'completed': True},
        'energy_storage': {'completed': False}}
