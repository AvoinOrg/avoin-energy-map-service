"""Focused adapter boundary tests. Actual-image proof is doctor-owned/private."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from src.state_storage import sandbox_startup as startup
from src.state_storage import hba_file


class StateStartupTests(unittest.TestCase):
    def setUp(self):
        caller = patch.object(startup, 'CALLER_IDENTITY', (os.getuid(), os.getgid()))
        caller.start()
        self.addCleanup(caller.stop)

    def fixture(self, folder):
        root = Path(folder)
        directory = root / 'energy-state'; directory.mkdir(mode=0o700)
        setup = root / 'setup.sh'
        # A later final writer must execute BEFORE the guard; suffix is retained.
        original = (b'if [ "${TEST_INITIALIZED:-}" = 1 ]; then\n    return 0\nfi\n'
                    b'printf "# final baseline\\n" > "$TEST_HBA"\n')
        setup.write_bytes(original); setup.chmod(0o755)
        hba = root / 'hba'; hba.write_bytes(b'# final baseline\n'); hba.chmod(0o644)
        request = dict(mode='sandbox', database='energymap_personal_state',
            migrator='energymap_state_migrator', writer='energymap_state_writer',
            provisioning_id=str(uuid4()), hba_file=str(hba),
            baseline_sha256=hba_file.digest(hba.read_bytes()))
        request_file = directory / 'request.json'
        request_file.write_text(json.dumps(request)); request_file.chmod(0o600)
        return directory, setup, original, hba, request_file, request

    def test_helper_pin_matches_delivered_frozen_bytes(self):
        self.assertEqual(hashlib.sha256(Path(hba_file.__file__).read_bytes()).hexdigest(),
                         startup.HBA_HELPER_HASH)

    def test_only_fixed_native_paths_accept_the_pre_repair_owner_phase(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            root.chmod(0o755)
            native = root / 'native.sh'
            custom = root / 'custom.sh'
            for path in (native, custom):
                path.write_bytes(b'# pinned script\n')
                path.chmod(0o755)
            # Model the distinct native root/service phases without privileged
            # test chown. setUp fixes CALLER_IDENTITY to the real file owner.
            with patch.multiple(startup, IDENTITY=(os.getuid() + 1, os.getgid()),
                    NATIVE_SCRIPTS=frozenset({str(native)})):
                self.assertEqual(startup.script(native), native)
                with self.assertRaises(ValueError):
                    startup.script(custom)
                native.chmod(0o775)
                with self.assertRaises(ValueError):
                    startup.script(native)
                native.chmod(0o755)
                root.chmod(0o775)
                with self.assertRaises(ValueError):
                    startup.script(native)

    def test_prepare_final_guard_idempotence_and_exact_active_rollback(self):
        with tempfile.TemporaryDirectory() as folder:
            directory, setup, original, hba, request_file, request = self.fixture(folder)
            before = (hba.stat().st_uid, hba.stat().st_gid, hba.stat().st_mode)
            with patch.multiple(startup, DIRECTORY=directory, REQUEST=request_file,
                    IDENTITY=(os.getuid(), os.getgid()), SETUP=setup, HBA=str(hba),
                    SETUP_HASH=hba_file.digest(original)), patch.object(startup, 'check_chain'), \
                    patch.object(startup, 'drop_to_hba_owner') as drop:
                startup.prepare(); startup.prepare()
                backup = directory / 'setup-original.backup'
                self.assertEqual(backup.read_bytes(), original)
                self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
                self.assertEqual(setup.read_bytes(), startup.managed_setup(original))
                startup.guard(); startup.guard()
                self.assertEqual(drop.call_count, 2)
                self.assertEqual(hba.read_bytes(), hba_file.validate(request) + b'# final baseline\n')
                self.assertEqual((hba.stat().st_uid, hba.stat().st_gid, hba.stat().st_mode), before)
                hba_file.change(request, rollback=True)
                self.assertEqual(hba.read_bytes(), b'# final baseline\n')
                current = setup.read_bytes()
                startup.replace_script(setup, startup.original_setup(current), current)
                self.assertEqual(setup.read_bytes(), original)
                self.assertEqual(setup.stat().st_mode & 0o777, 0o755)
                with self.assertRaises(ValueError): startup.original_setup(original + b'# drift')

    def test_startup_lock_and_unrelated_backup_refuse_without_changing_setup(self):
        import fcntl
        for defect in ('locked', 'backup'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as folder:
                directory, setup, original, hba, request_file, request = self.fixture(folder)
                lock = directory / 'startup.lock'; lock.touch(mode=0o600)
                if defect == 'backup':
                    backup = directory / 'setup-original.backup'
                    backup.write_bytes(b'unrelated'); backup.chmod(0o600)
                with lock.open('rb') as held:
                    if defect == 'locked': fcntl.flock(held.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)
                    with patch.multiple(startup, DIRECTORY=directory, REQUEST=request_file,
                            IDENTITY=(os.getuid(), os.getgid()), SETUP=setup, HBA=str(hba),
                            SETUP_HASH=hba_file.digest(original)), patch.object(startup, 'check_chain'):
                        with self.assertRaises((ValueError, BlockingIOError)): startup.prepare()
                self.assertEqual(setup.read_bytes(), original)

    def test_setup_drift_wrong_owner_symlink_and_hardlink_refuse(self):
        for defect in ('drift', 'owner', 'symlink', 'hardlink'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as folder:
                directory, setup, original, hba, request_file, request = self.fixture(folder)
                identity = (os.getuid(), os.getgid())
                if defect == 'drift': setup.write_bytes(original + b'# unrelated\n')
                if defect == 'owner': identity = (os.getuid()+1, os.getgid())
                if defect == 'symlink':
                    target = setup.with_suffix('.original'); setup.rename(target); setup.symlink_to(target)
                if defect == 'hardlink': os.link(setup, setup.with_suffix('.alias'))
                with patch.multiple(startup, DIRECTORY=directory, REQUEST=request_file,
                        IDENTITY=identity, SETUP=setup, HBA=str(hba),
                        SETUP_HASH=hba_file.digest(original)), patch.object(startup, 'check_chain'), \
                        patch.object(startup, 'drop_to_hba_owner'):
                    with self.assertRaises(ValueError): startup.prepare()
                self.assertEqual(hba.read_bytes(), b'# final baseline\n')

    def test_bad_request_or_baseline_never_changes_hba(self):
        for change in ({'mode': 'disposable'}, {'database': 'other'},
                       {'baseline_sha256': '0'*64}, {'unknown': True}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as folder:
                directory, setup, original, hba, request_file, request = self.fixture(folder)
                request_file.write_text(json.dumps(request | change))
                with patch.multiple(startup, DIRECTORY=directory, REQUEST=request_file,
                        IDENTITY=(os.getuid(), os.getgid()), SETUP=setup, HBA=str(hba),
                        SETUP_HASH=hba_file.digest(original)), patch.object(startup, 'check_chain'), \
                        patch.object(startup, 'drop_to_hba_owner'):
                    setup.write_bytes(startup.managed_setup(original))
                    with self.assertRaises(ValueError): startup.guard()
                self.assertEqual(hba.read_bytes(), b'# final baseline\n')

    def test_unprepared_or_changed_call_order_refuses(self):
        with tempfile.TemporaryDirectory() as folder:
            directory, setup, original, hba, request_file, request = self.fixture(folder)
            with patch.multiple(startup, DIRECTORY=directory, REQUEST=request_file,
                    IDENTITY=(os.getuid(), os.getgid()), SETUP=setup, HBA=str(hba),
                    SETUP_HASH=hba_file.digest(original)), patch.object(startup, 'check_chain'):
                with self.assertRaises(ValueError): startup.guard()
                setup.write_bytes(startup.GUARD_CALL + original)
                with self.assertRaises(ValueError): startup.guard()
            self.assertEqual(hba.read_bytes(), b'# final baseline\n')

    def test_modifier_roundtrip_refuses_unrelated_bytes(self):
        original = b'#!/bin/bash\n# retained modifier\n'
        with patch.object(startup, 'MODIFIER_HASH', hba_file.digest(original)):
            amended = startup.modifier_bytes(original)
            self.assertEqual(amended, b'#!/bin/bash\n' + startup.NORMALIZE_CALL +
                             b'# retained modifier\n' + startup.PREPARE_CALL)
            self.assertEqual(startup.original_modifier(amended, original), original)
            with self.assertRaises(ValueError): startup.modifier_bytes(original+b'# drift')
            with self.assertRaises(ValueError): startup.original_modifier(amended+b'# drift', original)

    def test_chain_checks_each_script_and_exact_modifier_suffix(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            base = root/'base.sh'; base.write_bytes(b'# pinned entrypoint\n'); base.chmod(0o755)
            modifier = root/'modifier.sh'
            original = b'#!/bin/bash\n# pinned modifier\n'
            with patch.object(startup, 'MODIFIER_HASH', hba_file.digest(original)):
                modifier.write_bytes(startup.modifier_bytes(original))
            modifier.chmod(0o755)
            with patch.multiple(startup, IDENTITY=(os.getuid(), os.getgid()),
                    CHAIN={str(base): hba_file.digest(base.read_bytes())}, MODIFIER=modifier,
                    MODIFIER_HASH=hba_file.digest(original)):
                startup.check_chain()
                base.write_bytes(b'# changed entrypoint\n')
                with self.assertRaises(ValueError): startup.check_chain()
                base.write_bytes(b'# pinned entrypoint\n')
                modifier.write_bytes(startup.PREPARE_CALL + original)
                with self.assertRaises(ValueError): startup.check_chain()

    def test_pinned_normalization_replays_one_original_injection_only(self):
        with tempfile.TemporaryDirectory() as folder:
            directory, setup, pristine, hba, request_file, request = self.fixture(folder)
            canonical = pristine + b'. /scripts/custom/pg_hba-additions.sh\n'
            with patch.multiple(startup, DIRECTORY=directory, REQUEST=request_file,
                    IDENTITY=(os.getuid(), os.getgid()), SETUP=setup, HBA=str(hba),
                    SETUP_HASH=hba_file.digest(canonical), PRISTINE_SETUP_HASH=hba_file.digest(pristine)), \
                    patch.object(startup, 'check_chain'):
                for _ in range(2):
                    startup.prepare(normalize=True)
                    self.assertEqual(setup.read_bytes(), pristine)
                    # The unchanged original modifier supplies this insertion.
                    setup.write_bytes(canonical)
                    startup.prepare()
                    self.assertEqual(setup.read_bytes(), startup.managed_setup(canonical))
                self.assertEqual((directory/'setup-original.backup').read_bytes(), canonical)
                setup.write_bytes(canonical + b'. /scripts/custom/pg_hba-additions.sh\n')
                before = setup.read_bytes()
                with self.assertRaises(ValueError): startup.prepare(normalize=True)
                self.assertEqual(setup.read_bytes(), before)

    def test_configuration_modifier_replay_accepts_only_exact_fixed_site(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'setup-conf.sh'
            path.touch(mode=0o755)
            modifier = Path(folder) / 'modifier.sh'
            original = b'#!/bin/bash\n# original modifier\n'
            prefix = b'# original configuration\nshared_buffers = 128MB\n'
            suffix = b'# original remaining configuration\n'
            canonical = prefix + startup.CONFIG_INSERTION + suffix
            with patch.multiple(startup, IDENTITY=(os.getuid(), os.getgid()),
                    CONFIGURATION=str(path), CHAIN={str(path): hba_file.digest(canonical)},
                    MODIFIER=modifier, MODIFIER_HASH=hba_file.digest(original)):
                modifier.write_bytes(startup.modifier_bytes(original))
                modifier.chmod(0o755)
                for count in (1, 2, 3):
                    current = prefix + startup.CONFIG_INSERTION * count + suffix
                    path.write_bytes(current)
                    startup.check_chain()
                    self.assertEqual(path.read_bytes(), current)
                for current in (
                    prefix + suffix,
                    prefix + startup.CONFIG_INSERTION + b'# drift\n' + suffix,
                    prefix + startup.CONFIG_INSERTION + suffix + startup.CONFIG_INSERTION,
                    startup.CONFIG_INSERTION + prefix + suffix,
                    canonical.replace(b'1000', b'1001'),
                    canonical.replace(b'128MB', b'256MB'),
                ):
                    with self.subTest(current=current):
                        path.write_bytes(current)
                        with self.assertRaises(ValueError):
                            startup.check_chain()

    def test_initialized_return_cannot_bypass_failed_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            directory, setup, original, hba, request_file, request = self.fixture(folder)
            with patch.object(startup, 'SETUP_HASH', hba_file.digest(original)):
                managed = startup.managed_setup(original)
                self.assertEqual(managed.count(startup.GUARD_CALL), 2)
                self.assertEqual(startup.original_setup(managed), original)
            setup.write_bytes(managed.replace(b'/usr/bin/python3', b'/bin/false'))
            for initialized in ('', '1'):
                for enclosing in ('source "$1"; echo LISTENER',
                                  'f() { source "$1"; echo LISTENER; }; if f "$1"; then echo FALLBACK; fi'):
                    with self.subTest(initialized=initialized, enclosing=enclosing):
                        result = subprocess.run(['/bin/bash', '-c', enclosing, 'test', str(setup)],
                            env={'PATH': '/usr/bin:/bin', 'TEST_HBA': str(hba),
                                 'TEST_INITIALIZED': initialized}, capture_output=True, timeout=5)
                        self.assertEqual(result.returncode, 1)
                        self.assertNotIn(b'LISTENER', result.stdout)
                        self.assertNotIn(b'FALLBACK', result.stdout)

    def test_hba_mutation_identity_descent_is_fixed_and_verified(self):
        with patch.object(startup, 'IDENTITY', (101, 103)), \
                patch.object(startup.os, 'setgroups') as groups, \
                patch.object(startup.os, 'setresgid') as gid, \
                patch.object(startup.os, 'setresuid') as uid, \
                patch.object(startup.os, 'getresuid', return_value=(101,)*3), \
                patch.object(startup.os, 'getresgid', return_value=(103,)*3), \
                patch.object(startup.os, 'getgroups', return_value=[]):
            startup.drop_to_hba_owner()
            groups.assert_called_once_with([])
            gid.assert_called_once_with(103, 103, 103)
            uid.assert_called_once_with(101, 101, 101)
        with patch.object(startup, 'CALLER_IDENTITY', (-1, -1)), \
                patch.object(startup.os, 'setgroups') as groups:
            with self.assertRaises(ValueError): startup.drop_to_hba_owner()
            groups.assert_not_called()

    def test_hba_ownership_preparation_preserves_service_file_and_refuses_drift(self):
        for defect in (None, 'owner', 'mode', 'symlink', 'hardlink'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as folder:
                directory, setup, original, hba, request_file, request = self.fixture(folder)
                identity = (os.getuid(), os.getgid())
                if defect == 'owner':
                    identity = (os.getuid() + 1, os.getgid())
                elif defect == 'mode':
                    hba.chmod(0o600)
                elif defect == 'symlink':
                    target = hba.with_suffix('.original')
                    hba.rename(target)
                    hba.symlink_to(target)
                elif defect == 'hardlink':
                    os.link(hba, hba.with_suffix('.alias'))
                before = hba.stat()
                with patch.multiple(startup, HBA=str(hba), IDENTITY=identity), \
                        patch.object(startup.os, 'fchown') as chown:
                    if defect is None:
                        startup.prepare_hba_owner(request)
                    else:
                        with self.assertRaises(ValueError):
                            startup.prepare_hba_owner(request)
                    chown.assert_not_called()
                self.assertEqual(hba.read_bytes(), b'# final baseline\n')
                after = hba.stat()
                self.assertEqual((after.st_uid, after.st_gid, after.st_mode),
                                 (before.st_uid, before.st_gid, before.st_mode))

    def test_checked_sourced_failure_exits_even_inside_function_and_conditional(self):
        # Exercise the actual new shell bytes with a failed/missing subprocess.
        # The separate private actual-image proof executes the complete chain.
        for call in (startup.NORMALIZE_CALL, startup.PREPARE_CALL, startup.GUARD_CALL):
            for program in ('/bin/false', '/missing/energy-python'):
                for enclosing in ('source "$1"; echo LISTENER',
                                  'f() { source "$1"; echo LISTENER; }; f "$1"',
                                  'f() { source "$1"; echo LISTENER; }; if f "$1"; then echo FALLBACK; fi'):
                    with self.subTest(program=program, enclosing=enclosing), tempfile.TemporaryDirectory() as folder:
                        path = Path(folder)/'hook.sh'
                        path.write_bytes(call.replace(b'/usr/bin/python3', program.encode()))
                        result = subprocess.run(['/bin/bash','-c',enclosing,'test',str(path)],
                            capture_output=True, timeout=5)
                        self.assertEqual(result.returncode, 1)
                        self.assertNotIn(b'LISTENER', result.stdout)
                        self.assertNotIn(b'FALLBACK', result.stdout)

    def test_standalone_isolated_python_fails_sanitized_without_install(self):
        result = subprocess.run(['/usr/bin/python3', '-I', startup.__file__, 'guard'],
            capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), {'status':'failed','category':'startup_refused'})
        self.assertEqual(result.stderr, b'')
