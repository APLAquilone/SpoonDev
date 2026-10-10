"""Inspect the actual launchd payload without creating a service on this host."""
import importlib.util
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/collector-service-mac.py'
spec = importlib.util.spec_from_file_location('collector_service', SCRIPT)
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


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


if __name__ == '__main__':
    unittest.main()
