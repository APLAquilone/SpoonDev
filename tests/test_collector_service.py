"""Inspect the actual launchd payload without creating a service on this host."""
import importlib.util
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/collector-service-mac.py'
spec = importlib.util.spec_from_file_location('collector_service', SCRIPT)
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class FakeLaunchd:
    """Model asynchronous job removal and disabled overrides, without macOS."""
    def __init__(self, config, plist, *, loaded=True, unload_polls=0,
                 bootout_error=None, bootstrap_error=None, enable_error=None):
        self.config = config
        self.plist = plist
        self.domain = 'gui/501'
        self.target = self.domain + '/' + config['label']
        self.loaded = loaded
        self.unload_polls = unload_polls
        self.unloading = False
        self.disabled = True
        self.bootout_error = bootout_error
        self.bootstrap_error = bootstrap_error
        self.enable_error = enable_error
        self.calls = []
        self.runtime = self.description()

    def description(self):
        payload = service.job(self.config)
        arguments = '\n'.join('\t\t' + arg for arg in payload['ProgramArguments'])
        return (self.target + ' = {\n\tpath = ' + str(self.plist)
                + '\n\tstate = running\n\tprogram = ' + self.config['python']
                + '\n\targuments = {\n' + arguments + '\n\t}\n'
                + '\tworking directory = ' + self.config['root'] + '\n\tpid = 56770\n}\n')

    def __call__(self, *arguments):
        self.calls.append((arguments, self.loaded, self.unloading))
        code = 0
        stdout = stderr = ''
        if arguments == ('print', self.domain):
            stdout = self.domain + ' = {}'
        elif arguments == ('print', self.target):
            if self.unloading:
                if self.unload_polls:
                    self.unload_polls -= 1
                else:
                    self.loaded = False
                    self.unloading = False
            if self.loaded:
                stdout = self.runtime
            else:
                code = 113
                stderr = f'Bad request.\nCould not find service "{self.config["label"]}" in domain for user gui: 501'
        elif arguments == ('bootout', self.target):
            if self.bootout_error:
                code, stderr = self.bootout_error
            else:
                self.unloading = True
        elif arguments == ('enable', self.target):
            if self.enable_error:
                code, stderr = self.enable_error
            else:
                self.disabled = False
        elif arguments == ('bootstrap', self.domain, str(self.plist)):
            if self.bootstrap_error:
                code, stderr = self.bootstrap_error
            elif self.loaded or self.disabled:
                code, stderr = 5, 'Bootstrap failed: 5: Input/output error'
            else:
                self.loaded = True
                self.runtime = self.description()
        else:
            raise AssertionError('Unexpected launchctl operation: ' + repr(arguments))
        return subprocess.CompletedProcess(['launchctl', *arguments], code, stdout, stderr)


class CollectorServiceTests(unittest.TestCase):
    def test_printed_job_uses_absolute_paths_and_only_restarts_failed_collector(self):
        result = subprocess.run([sys.executable, str(SCRIPT), 'install', '--print-plist'],
                                check=True, capture_output=True, timeout=5)
        job = plistlib.loads(result.stdout)
        arguments = job['ProgramArguments']
        self.assertEqual(Path(arguments[0]), Path(sys.executable).resolve())
        self.assertEqual(arguments[1], str(SCRIPT.parent / 'supervise-collector.py'))
        self.assertEqual(arguments[arguments.index('--db')+1],
                         str(SCRIPT.parents[1] / 'data/spoondev.sqlite3'))
        self.assertEqual(arguments[arguments.index('--auth-db')+1],
                         str(SCRIPT.parents[1] / 'data/accounts.sqlite3'))
        self.assertEqual(job['KeepAlive'], {'SuccessfulExit': False})
        self.assertTrue(job['RunAtLoad'])
        self.assertNotIn('cloudflared', ' '.join(arguments))
        self.assertNotIn('serve', arguments)

    def test_separate_checkouts_have_distinct_service_ids_and_keep_spaces_as_arguments(self):
        public = service.configuration('/tmp/My SpoonDev', sys.executable)
        development = service.configuration('/tmp/My SpoonDev-dev', sys.executable)
        self.assertNotEqual(public['label'], development['label'])
        # macOS resolves /tmp to /private/tmp; launchd uses canonical paths.
        expected_root = Path('/tmp/My SpoonDev').resolve()
        job = plistlib.loads(plistlib.dumps(service.job(public)))
        self.assertEqual(job['WorkingDirectory'], str(expected_root))
        self.assertEqual(job['ProgramArguments'][1], str(expected_root / 'scripts/supervise-collector.py'))
        self.assertEqual(job['ProgramArguments'][-1], str(expected_root / 'data/accounts.sqlite3'))

    def test_symlink_alias_uses_the_same_service_and_canonical_job_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve() / 'My SpoonDev'
            root.mkdir()
            alias = root.parent / 'checkout alias'
            alias.symlink_to(root, target_is_directory=True)
            direct = service.configuration(root, sys.executable)
            aliased = service.configuration(alias, sys.executable)
            self.assertEqual(aliased, direct)
            job = plistlib.loads(plistlib.dumps(service.job(aliased)))
            self.assertEqual(job['WorkingDirectory'], str(root))
            self.assertEqual(job['ProgramArguments'][1], str(root / 'scripts/supervise-collector.py'))
            self.assertEqual(job['ProgramArguments'][-1], str(root / 'data/accounts.sqlite3'))


class CollectorServiceInstallTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve() / 'My SpoonDev'
        self.root.mkdir()
        self.config = service.configuration(self.root, sys.executable)
        self.plist = Path(self.folder.name).resolve() / 'LaunchAgents' / (self.config['label'] + '.plist')
        self.marker = self.root / 'data/collector-service.json'

    def install(self, fake):
        with patch.object(service, 'launchctl', fake), patch.object(service.time, 'sleep'):
            service.install(self.config, fake.domain, self.plist, self.marker)

    def test_refresh_waits_for_loaded_service_to_disappear_before_bootstrap(self):
        fake = FakeLaunchd(self.config, self.plist, unload_polls=3)
        self.install(fake)
        bootstrap = [call for call in fake.calls if call[0][0] == 'bootstrap']
        self.assertEqual(len(bootstrap), 1)
        self.assertEqual(bootstrap[0][1:], (False, False))
        self.assertTrue(fake.loaded)
        self.assertFalse(fake.disabled)
        self.assertEqual(plistlib.loads(self.plist.read_bytes()), service.job(self.config))
        self.assertEqual(json.loads(self.marker.read_text()), self.config)
        self.assertEqual(self.plist.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.marker.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.plist.with_suffix('.plist.tmp').exists())

    def test_new_install_enables_only_this_exact_label_without_bootout(self):
        fake = FakeLaunchd(self.config, self.plist, loaded=False)
        self.install(fake)
        self.assertNotIn('bootout', [call[0][0] for call in fake.calls])
        self.assertIn((('enable', fake.target), False, False), fake.calls)
        self.assertTrue(fake.loaded)

    def test_bootout_failure_preserves_existing_files_and_stops_before_bootstrap(self):
        self.plist.parent.mkdir()
        self.plist.write_bytes(b'old plist')
        self.marker.parent.mkdir()
        self.marker.write_text('old marker')
        fake = FakeLaunchd(self.config, self.plist, bootout_error=(5, 'Input/output error'))
        with self.assertRaisesRegex(service.ServiceError, r'Unloading collector service failed \(5\)'):
            self.install(fake)
        self.assertTrue(fake.loaded)
        self.assertEqual(self.plist.read_bytes(), b'old plist')
        self.assertEqual(self.marker.read_text(), 'old marker')
        self.assertFalse(any(call[0][0] in ('bootstrap', 'enable') for call in fake.calls))

    def test_job_disappearing_during_bootout_is_confirmed_before_new_install(self):
        fake = FakeLaunchd(self.config, self.plist)
        def command(*arguments):
            if arguments[0] == 'bootout':
                fake.loaded = False
                fake.bootout_error = (3, 'Boot-out failed: 3: No such process')
            return fake(*arguments)
        with patch.object(service, 'launchctl', command):
            service.install(self.config, fake.domain, self.plist, self.marker)
        self.assertTrue(fake.loaded)
        self.assertTrue(self.marker.exists())
        self.assertEqual([call[1] for call in fake.calls if call[0][0] == 'bootstrap'], [False])

    def test_unloading_timeout_never_bootstraps_or_claims_installation(self):
        fake = FakeLaunchd(self.config, self.plist, unload_polls=100)
        with patch.object(service.time, 'monotonic', side_effect=(0, 31)):
            with self.assertRaisesRegex(service.ServiceError, 'still unloading'):
                self.install(fake)
        self.assertFalse(self.plist.exists())
        self.assertFalse(self.marker.exists())
        self.assertNotIn('bootstrap', [call[0][0] for call in fake.calls])

    def test_other_checkout_or_modified_loaded_command_is_never_unloaded(self):
        for field, replacement in ((self.config['root'], self.config['root'] + '-dev'),
                                   (self.config['python'], '/tmp/other-python'),
                                   (str(self.plist), '/tmp/foreign.plist')):
            with self.subTest(field=field):
                fake = FakeLaunchd(self.config, self.plist)
                fake.runtime = fake.runtime.replace(field, replacement)
                with self.assertRaisesRegex(service.ServiceError, 'different command or checkout'):
                    self.install(fake)
                self.assertFalse(any(call[0][0] in ('bootout', 'bootstrap', 'enable') for call in fake.calls))
                self.assertFalse(self.plist.exists())

    def test_incomplete_loaded_identity_is_never_unloaded(self):
        fake = FakeLaunchd(self.config, self.plist)
        fake.runtime = fake.runtime.replace('\tprogram = ' + self.config['python'] + '\n', '')
        with self.assertRaisesRegex(service.ServiceError, 'identity is incomplete'):
            self.install(fake)
        self.assertNotIn('bootout', [call[0][0] for call in fake.calls])

    def test_bootstrap_failure_retains_original_error_and_no_success_marker(self):
        fake = FakeLaunchd(self.config, self.plist, loaded=False,
                           bootstrap_error=(5, 'Bootstrap failed: 5: Input/output error'))
        with self.assertRaisesRegex(service.ServiceError, 'Bootstrap failed: 5: Input/output error'):
            self.install(fake)
        self.assertFalse(self.marker.exists())
        self.assertFalse(fake.loaded)

    def test_enable_failure_does_not_attempt_bootstrap(self):
        fake = FakeLaunchd(self.config, self.plist, loaded=False,
                           enable_error=(1, 'Not permitted'))
        with self.assertRaisesRegex(service.ServiceError, 'Enabling collector service failed'):
            self.install(fake)
        self.assertFalse(self.marker.exists())
        self.assertNotIn('bootstrap', [call[0][0] for call in fake.calls])

    def test_gui_domain_failure_is_reported_before_mutation(self):
        result = subprocess.CompletedProcess([], 125, '', 'Domain does not exist')
        with patch.object(service, 'launchctl', return_value=result) as command:
            with self.assertRaisesRegex(service.ServiceError, 'current login GUI domain'):
                service.install(self.config, 'gui/501', self.plist, self.marker)
        self.assertEqual(command.call_args.args, ('print', 'gui/501'))
        self.assertEqual(command.call_count, 1)
        self.assertFalse(self.plist.exists())

    def test_inspection_error_is_not_misread_as_absent_job(self):
        result = subprocess.CompletedProcess([], 5, '', 'Input/output error')
        with patch.object(service, 'launchctl', return_value=result) as command:
            with self.assertRaisesRegex(service.ServiceError, 'Inspecting collector service failed'):
                service.unload_service('gui/501/' + self.config['label'], self.config, self.plist)
        self.assertEqual(command.call_count, 1)

    def test_remove_waits_until_service_is_absent(self):
        fake = FakeLaunchd(self.config, self.plist, unload_polls=2)
        with patch.object(service, 'launchctl', fake), patch.object(service.time, 'sleep'):
            service.unload_service(fake.target, self.config, self.plist)
        self.assertFalse(fake.loaded)
        self.assertFalse(any(call[0][0] in ('enable', 'bootstrap') for call in fake.calls))


if __name__ == '__main__':
    unittest.main()
