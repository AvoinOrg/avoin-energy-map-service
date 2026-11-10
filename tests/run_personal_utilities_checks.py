"""Ordinary serial v5 checks in the installed project Python; no env-file import.

Run from repo root using the existing interpreter:
  .codex-orch/runtime/envs/avoin-energy-map-service-5iS2udd1-py3.12/bin/python -m tests.run_personal_utilities_checks
The fixture helper borrows only the existing private 55433 bootstrap reference.
"""
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    env = {'HOME': str(Path.home()), 'PATH': '/usr/bin:/bin',
        'PYTHONPATH': f'{root}:{root}/src', 'PYTHONDONTWRITEBYTECODE': '1',
        'RUN_PERSONAL_RENOVATIONS_DB_TESTS': '1', 'RUN_PERSONAL_UTILITIES_DB_TESTS': '1'}
    return subprocess.call([sys.executable, '-m', 'unittest',
        'tests.test_personal_utilities', 'tests.test_personal_renovations', 'tests.test_personal_hvac', 'tests.test_personal_property',
        'tests.test_building_contract', 'tests.test_user_buildings', 'tests.test_building_shares',
        'tests.test_state_storage', 'tests.test_personal_renovations_db', 'tests.test_personal_utilities_db',
        'tests.test_personal_renovations_storage_db', '-v'], cwd=root, env=env)


if __name__ == '__main__': raise SystemExit(main())
