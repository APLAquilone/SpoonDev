"""Exercise Dev updates against real local Git histories, without network access."""

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest


UPDATE_SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/update-dev-mac.sh'
GITHUB_URL = 'https://github.com/APLAquilone/SpoonDev.git'
DEV_BRANCH = 'dev/v0.2.2'


class DevUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.upstream = self.root / 'upstream'
        self.production = self.root / 'SpoonDev'
        self.dev = self.root / 'SpoonDev-dev'
        self.upstream.mkdir()
        self.git(self.upstream, 'init', '-b', 'main')
        self.identity(self.upstream)
        (self.upstream / 'scripts').mkdir()
        shutil.copy2(UPDATE_SCRIPT, self.upstream / 'scripts/update-dev-mac.sh')
        (self.upstream / 'spoondev').mkdir()
        (self.upstream / 'spoondev/__init__.py').write_text("__version__ = '0.2.1'\n")
        (self.upstream / '.gitignore').write_text('data/\n__pycache__/\n')
        (self.upstream / 'README.md').write_text('Initial production version\n')
        self.commit(self.upstream, 'Initial v0.2.1')
        self.git(self.root, 'clone', '--no-hardlinks', str(self.upstream), str(self.production))
        self.git(self.root, 'clone', '--no-hardlinks', str(self.production), str(self.dev))
        self.identity(self.dev)
        self.initial_dev_commit = self.git(self.dev, 'rev-parse', 'HEAD')
        self.production_commit = self.git(self.production, 'rev-parse', 'HEAD')
        self.original_origin = self.git(self.dev, 'remote', 'get-url', 'origin')
        # Redirect only this fixture's explicit GitHub URL to the local upstream.
        self.git(self.dev, 'config', f'url.{self.upstream.as_uri()}.insteadOf', GITHUB_URL)

        self.database = self.dev / 'data/private.sqlite3'
        self.database.parent.mkdir()
        with sqlite3.connect(self.database) as connection:
            connection.execute('CREATE TABLE sentinel(value TEXT)')
            connection.execute("INSERT INTO sentinel VALUES('private Dev account data')")
        self.database_bytes = self.database.read_bytes()

        bin_directory = self.root / 'bin'
        bin_directory.mkdir()
        self.install_log = self.root / 'pip-calls.jsonl'
        fake_python = bin_directory / 'python'
        fake_python.write_text(
            f'#!{sys.executable}\n'
            'import json, os, sys\n'
            "if sys.argv[1:3] == ['-m', 'pip']:\n"
            "    with open(os.environ['DEV_UPDATE_PIP_LOG'], 'a') as output:\n"
            '        output.write(json.dumps(sys.argv[1:]) + "\\n")\n'
            '    raise SystemExit(0)\n'
            'os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n'
        )
        fake_python.chmod(0o755)
        self.environment = os.environ.copy()
        self.environment.pop('PYTHONPATH', None)
        self.environment.update(
            PATH=str(bin_directory) + os.pathsep + self.environment.get('PATH', ''),
            DEV_UPDATE_PIP_LOG=str(self.install_log),
            PYTHONDONTWRITEBYTECODE='1',
            GIT_TERMINAL_PROMPT='0',
        )
        self.git(self.upstream, 'switch', '-c', DEV_BRANCH)
        (self.upstream / 'spoondev/__init__.py').write_text("__version__ = '0.2.2'\n")
        self.release_commit = self.commit(self.upstream, 'Development v0.2.2')

    def git(self, root, *arguments):
        return subprocess.check_output(
            ['git', *arguments], cwd=root, text=True, stderr=subprocess.PIPE,
        ).strip()

    def identity(self, root):
        self.git(root, 'config', 'user.name', 'Dev update fixture')
        self.git(root, 'config', 'user.email', 'dev-update@example.invalid')

    def commit(self, root, message):
        self.git(root, 'add', '.')
        self.git(root, 'commit', '-m', message)
        return self.git(root, 'rev-parse', 'HEAD')

    def update(self, *arguments):
        return subprocess.run(
            ['bash', 'scripts/update-dev-mac.sh', *arguments], cwd=self.dev,
            env=self.environment, text=True, capture_output=True, timeout=20,
        )

    def installations(self):
        if not self.install_log.exists():
            return []
        return [json.loads(line) for line in self.install_log.read_text().splitlines()]

    def assert_private_data_and_production_preserved(self):
        self.assertEqual(self.database.read_bytes(), self.database_bytes)
        self.assertEqual(self.git(self.dev, 'remote', 'get-url', 'origin'), self.original_origin)
        self.assertEqual(self.git(self.production, 'rev-parse', 'HEAD'), self.production_commit)
        self.assertEqual(self.git(self.production, 'branch', '--show-current'), 'main')

    def test_first_update_creates_release_branch_without_touching_origin_or_data(self):
        result = self.update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.dev, 'branch', '--show-current'), DEV_BRANCH)
        self.assertEqual(self.git(self.dev, 'rev-parse', 'HEAD'), self.release_commit)
        self.assertIn('Development code updated to v0.2.2', result.stdout)
        self.assertEqual(self.installations(), [['-m', 'pip', 'install', '-e', '.']])
        self.assert_private_data_and_production_preserved()

    def test_existing_release_branch_fast_forwards_after_upstream_fix(self):
        initial = self.update()
        self.assertEqual(initial.returncode, 0, initial.stdout + initial.stderr)
        (self.upstream / 'README.md').write_text('Fixed development release\n')
        updated_commit = self.commit(self.upstream, 'Fix development release')
        result = self.update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.dev, 'rev-parse', 'HEAD'), updated_commit)
        self.assertEqual((self.dev / 'README.md').read_text(), 'Fixed development release\n')
        self.assertEqual(len(self.installations()), 2)
        self.assert_private_data_and_production_preserved()

    def test_missing_branch_does_not_use_stale_fetch_head_or_install(self):
        self.git(self.dev, 'fetch', str(self.upstream), DEV_BRANCH)
        self.assertEqual(self.git(self.dev, 'rev-parse', 'FETCH_HEAD'), self.release_commit)
        result = self.update('dev/v0.9.9')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("couldn't find remote ref", result.stderr)
        self.assertEqual(self.git(self.dev, 'branch', '--show-current'), 'main')
        self.assertEqual(self.git(self.dev, 'rev-parse', 'HEAD'), self.initial_dev_commit)
        self.assertEqual(self.installations(), [])
        self.assert_private_data_and_production_preserved()

    def test_unstaged_changes_are_preserved_and_prevent_installation(self):
        changed = 'Uncommitted local Dev work\n'
        (self.dev / 'README.md').write_text(changed)
        result = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Save your tracked Dev changes', result.stdout)
        self.assertEqual((self.dev / 'README.md').read_text(), changed)
        self.assertEqual(self.git(self.dev, 'rev-parse', 'HEAD'), self.initial_dev_commit)
        self.assertEqual(self.installations(), [])
        self.assert_private_data_and_production_preserved()

    def test_staged_changes_are_preserved_and_prevent_installation(self):
        changed = 'Staged local Dev work\n'
        (self.dev / 'README.md').write_text(changed)
        self.git(self.dev, 'add', 'README.md')
        staged_before = self.git(self.dev, 'diff', '--cached')
        result = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Save your tracked Dev changes', result.stdout)
        self.assertEqual(self.git(self.dev, 'diff', '--cached'), staged_before)
        self.assertEqual(self.installations(), [])
        self.assert_private_data_and_production_preserved()

    def test_diverging_dev_branch_preserves_local_commit_and_skips_install(self):
        self.git(self.dev, 'fetch', str(self.upstream), DEV_BRANCH)
        self.git(self.dev, 'switch', '-c', DEV_BRANCH, 'FETCH_HEAD')
        (self.dev / 'README.md').write_text('Local committed Dev feature\n')
        local_commit = self.commit(self.dev, 'Local Dev feature')
        (self.upstream / 'README.md').write_text('Different upstream Dev feature\n')
        self.commit(self.upstream, 'Upstream Dev feature')
        result = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('cannot be updated by fast-forward', result.stdout)
        self.assertEqual(self.git(self.dev, 'branch', '--show-current'), DEV_BRANCH)
        self.assertEqual(self.git(self.dev, 'rev-parse', 'HEAD'), local_commit)
        self.assertEqual((self.dev / 'README.md').read_text(), 'Local committed Dev feature\n')
        self.assertEqual(self.installations(), [])
        self.assert_private_data_and_production_preserved()


if __name__ == '__main__':
    unittest.main()
