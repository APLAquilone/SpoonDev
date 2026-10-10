"""Exercise safe ownership checks without stopping real host processes."""
import copy
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import errno
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import signal
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/stop-collector-mac.py'
spec = importlib.util.spec_from_file_location('stop_collector_mac', SCRIPT)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class CollectorStopTests(unittest.TestCase):
    def setUp(self):
        # Release validation runs this module alongside the production launcher.
        # Keep simulated PID and signal messages inside their test assertions.
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.enterContext(redirect_stdout(self.stdout))
        self.enterContext(redirect_stderr(self.stderr))
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve() / 'SpoonDev'
        self.root.mkdir()
        (self.root / 'data').mkdir()
        self.db = self.root / 'data/spoondev.sqlite3'
        self.auth = self.root / 'data/accounts.sqlite3'
        self.auth.touch()
        self.pid = 46651
        started = datetime.now(timezone.utc).replace(microsecond=0)
        self.recorded = {'pid': self.pid, 'owner_token': 'a'*32,
                         'started_at': (started+timedelta(milliseconds=100)).isoformat(),
                         'heartbeat_at': started.isoformat(), 'state': 'running',
                         'stopped_at': None, 'last_listener_observation': started.isoformat(),
                         'recent_snapshot_count': 1}
        self.process = {'uid': os.getuid(), 'parent': 1234,
                        'started': started.isoformat(),
                        'command': '/usr/bin/python3.12 -m spoondev collect-auto --concurrency 4'}
        self.evidence = {'root': str(self.root), 'database': str(self.db),
                         'recorded': self.recorded, 'process': self.process, 'kind': 'collect-auto'}

    def verify(self, *, recorded=None, current=None, cwd=None, open_lock=True, busy=True, selected=None):
        with (patch.object(helper, 'state', return_value=recorded or self.recorded),
              patch.object(helper, 'process', return_value=current or self.process),
              patch.object(helper, 'process_cwd', return_value=cwd or self.root),
              patch.object(helper, 'holds_open_lock', return_value=open_lock),
              patch.object(helper, 'lock_busy', return_value=busy)):
            return helper.verify(self.root, selected)

    def test_verified_cli_owner_has_cwd_database_token_start_and_held_lease(self):
        self.assertEqual(self.verify(selected=self.pid), self.evidence)

    def test_supervisor_can_predate_worker_restart_but_paths_are_exact(self):
        current = dict(self.process, command=shlex.join(['/usr/bin/python3.12',
                       str(self.root / 'scripts/supervise-collector.py'), '--skip-loaded-service']),
                       started=(datetime.fromisoformat(self.process['started'])-timedelta(days=1)).isoformat())
        self.assertEqual(self.verify(current=current)['kind'], 'supervisor')

    def test_supervisor_relative_path_and_absolute_db_are_accepted(self):
        command = shlex.join(['/usr/bin/python3.12', 'scripts/supervise-collector.py',
                              '--db', str(self.db), '--auth-db', str(self.auth)])
        self.assertEqual(helper.command_kind(command, self.root, self.db, self.auth), 'supervisor')

    def test_cli_explicit_global_database_is_accepted(self):
        command = shlex.join(['/usr/bin/python3.12', '-m', 'spoondev', '--db', str(self.db),
                              'collect-auto', '--auth-db', str(self.auth)])
        self.assertEqual(helper.command_kind(command, self.root, self.db, self.auth), 'collect-auto')

    def test_other_database_auth_database_and_unknown_flags_fail_closed(self):
        for command in (shlex.join(['/usr/bin/python3.12', '-m', 'spoondev', '--db',
                                   str(self.root / 'elsewhere.sqlite3'), 'collect-auto']),
                        '/usr/bin/python3.12 -m spoondev collect-auto --auth-db ../other/accounts.sqlite3',
                        '/usr/bin/python3.12 -m spoondev collect-auto --unknown 4'):
            with self.subTest(command=command), self.assertRaises(helper.UnsafeCollector):
                helper.command_kind(command, self.root, self.db, self.auth)

    def test_manual_collectors_web_tunnel_and_shell_are_never_accepted(self):
        for command in ('/usr/bin/python3.12 -m spoondev collect-spoon --max-rooms 0',
                        '/usr/bin/python3.12 -m spoondev collect-monthly --max-djs 0',
                        '/usr/bin/python3.12 -m spoondev serve --port 8080',
                        '/usr/local/bin/cloudflared tunnel --url http://127.0.0.1:8080',
                        'bash scripts/start-public-mac.sh',
                        shlex.join(['/usr/bin/python3.12',
                                    str(self.root.parent / 'other/scripts/supervise-collector.py')])):
            with self.subTest(command=command), self.assertRaises(helper.UnsafeCollector):
                helper.command_kind(command, self.root, self.db, self.auth)

    def test_other_uid_cwd_and_missing_lock_fail_closed(self):
        cases = ({'current': dict(self.process, uid=os.getuid()+1)},
                 {'cwd': self.root.parent}, {'open_lock': False}, {'busy': False},
                 {'selected': self.pid+1})
        for case in cases:
            with self.subTest(case=case), self.assertRaises(helper.UnsafeCollector):
                self.verify(**case)

    def test_invalid_pid_owner_state_and_stopped_timestamp_fail_closed(self):
        for changes in ({'pid': 1}, {'pid': True}, {'pid': os.getpid()}, {'owner_token': None},
                        {'owner_token': 'invalid'}, {'state': 'stopped'},
                        {'stopped_at': self.recorded['started_at']}):
            with self.subTest(changes=changes), self.assertRaises(helper.UnsafeCollector):
                self.verify(recorded=dict(self.recorded, **changes))

    def test_reused_pid_and_excessive_cli_start_gap_fail_closed(self):
        base = datetime.fromisoformat(self.process['started'])
        for start in (base+timedelta(seconds=10), base-timedelta(seconds=60)):
            with self.subTest(start=start), self.assertRaises(helper.UnsafeCollector):
                self.verify(current=dict(self.process, started=start.isoformat()))

    def test_naive_or_invalid_worker_start_time_fails_closed(self):
        for started in ('2026-10-10T20:00:00', 'invalid', None):
            with self.subTest(started=started), self.assertRaises(helper.UnsafeCollector):
                self.verify(recorded=dict(self.recorded, started_at=started))

    def test_auth_database_must_exist(self):
        self.auth.unlink()
        with self.assertRaises(helper.UnsafeCollector):
            self.verify()

    def test_checkout_alias_is_canonical_but_data_aliases_are_rejected(self):
        alias = self.root.parent / 'checkout-alias'
        alias.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(helper.paths(alias)[0], self.root)
        self.auth.unlink()
        actual = self.root / 'actual-accounts.sqlite3'
        actual.touch()
        self.auth.symlink_to(actual)
        with self.assertRaises(helper.UnsafeCollector):
            helper.paths(self.root)

    def test_lsof_paths_with_spaces_and_multiple_processes_are_not_shell_split(self):
        output = 'p46651\nf4\nn/tmp/My SpoonDev/data/spoondev.sqlite3.worker.lock\np66\nf5u\nn/tmp/other\n'
        records = helper.file_records(output)
        self.assertEqual(records, [{'pid': 46651, 'fd': '4', 'n': '/tmp/My SpoonDev/data/spoondev.sqlite3.worker.lock'},
                                   {'pid': 66, 'fd': '5u', 'n': '/tmp/other'}])

    def test_lsof_cwd_must_be_selected_pid_and_exact_descriptor(self):
        with patch.object(helper, 'run_inspection', return_value=f'p{self.pid}\nfcwd\nn{self.root}\n'):
            self.assertEqual(helper.process_cwd(self.pid), self.root)
        with patch.object(helper, 'run_inspection', return_value=f'p999\nfcwd\nn{self.root}\n'):
            with self.assertRaises(helper.UnsafeCollector):
                helper.process_cwd(self.pid)

    def test_lsof_worker_file_must_be_numeric_fd_at_exact_path_for_pid(self):
        lock = Path(str(self.db)+'.worker.lock')
        for output, expected in ((f'p{self.pid}\nf4\nn{lock}\n', True),
                                 (f'p999\nf4\nn{lock}\n', False),
                                 (f'p{self.pid}\nfcwd\nn{lock}\n', False),
                                 (f'p{self.pid}\nf4\nn{lock}.other\n', False)):
            with self.subTest(output=output), patch.object(helper, 'run_inspection', return_value=output):
                self.assertEqual(helper.holds_open_lock(self.pid, lock), expected)

    def test_ps_reads_uid_parent_start_command_in_one_call(self):
        output = ' 501 1234 Sat Oct 10 11:21:55 2026 /usr/bin/python3.12 -m spoondev collect-auto\n'
        with patch.object(helper, 'run_inspection', return_value=output) as inspect:
            current = helper.process(self.pid)
        self.assertEqual(current['uid'], 501)
        self.assertEqual(current['started'], '2026-10-10T11:21:55+00:00')
        self.assertEqual(current['command'], '/usr/bin/python3.12 -m spoondev collect-auto')
        self.assertIn('uid=,ppid=,lstart=,command=', inspect.call_args.args[0])

    def test_os_lease_probe_does_not_create_truncate_or_unlink_file(self):
        lock = Path(str(self.db)+'.worker.lock')
        with self.assertRaises(FileNotFoundError):
            helper.lock_busy(self.db)
        self.assertFalse(lock.exists())
        lock.write_text('preserved')
        with lock.open('r+') as owner:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(helper.lock_busy(self.db))
        self.assertFalse(helper.lock_busy(self.db))
        self.assertEqual(lock.read_text(), 'preserved')

    def test_symlink_lease_fails_closed(self):
        actual = self.root / 'actual-lock'
        actual.touch()
        Path(str(self.db)+'.worker.lock').symlink_to(actual)
        with self.assertRaises(helper.UnsafeCollector):
            helper.lock_busy(self.db)

    def test_state_read_uses_existing_database_and_keeps_progress_evidence(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute('''CREATE TABLE worker_state(singleton,pid,owner_token,started_at,heartbeat_at,state,stopped_at)''')
            conn.execute('INSERT INTO worker_state VALUES(1,?,?,?,?,?,?)', tuple(self.recorded[key] for key in
                         ('pid', 'owner_token', 'started_at', 'heartbeat_at', 'state', 'stopped_at')))
            conn.execute('CREATE TABLE snapshots(observed_at TEXT)')
            conn.execute('INSERT INTO snapshots VALUES(?)', (self.recorded['last_listener_observation'],))
        self.assertEqual(helper.state(self.db), self.recorded)
        missing = self.root / 'missing.sqlite3'
        with self.assertRaises(sqlite3.OperationalError):
            helper.state(missing)
        self.assertFalse(missing.exists())

    def test_stop_rechecks_identity_then_only_sends_sigterm_and_waits_for_exit_and_lease(self):
        with (patch.object(helper, 'verify', return_value=self.evidence) as verify,
              patch.object(helper, 'process', side_effect=helper.UnsafeCollector('exited')),
              patch.object(helper, 'process_exists', return_value=False),
              patch.object(helper, 'lock_busy', return_value=False),
              patch.object(helper.os, 'kill') as kill):
            self.assertEqual(helper.stop(self.root, self.pid), self.evidence)
        self.assertEqual(verify.call_count, 2)
        kill.assert_called_once_with(self.pid, signal.SIGTERM)
        self.assertIn(f'Graceful stop requested for verified collector PID {self.pid}.', self.stdout.getvalue())
        self.assertIn('Collector exited and its OS lease was released.', self.stdout.getvalue())
        self.assertEqual(self.stderr.getvalue(), '')

    def test_changed_owner_token_prevents_all_signals(self):
        second = copy.deepcopy(self.evidence)
        second['recorded']['owner_token'] = 'b'*32
        with (patch.object(helper, 'verify', side_effect=[self.evidence, second]),
              patch.object(helper.os, 'kill') as kill):
            with self.assertRaises(helper.UnsafeCollector):
                helper.stop(self.root, self.pid)
        kill.assert_not_called()

    def test_missing_explicit_pid_prevents_inspection_and_signals(self):
        with patch.object(helper, 'verify') as verify, patch.object(helper.os, 'kill') as kill:
            with self.assertRaises(helper.UnsafeCollector):
                helper.stop(self.root, None)
        verify.assert_not_called()
        kill.assert_not_called()

    def test_stop_timeout_never_escalates_to_sigkill(self):
        with (patch.object(helper, 'verify', return_value=self.evidence),
              patch.object(helper, 'process', return_value=self.process),
              patch.object(helper.time, 'monotonic', side_effect=[0, 61]),
              patch.object(helper.os, 'kill') as kill):
            with self.assertRaisesRegex(helper.UnsafeCollector, 'No forced termination'):
                helper.stop(self.root, self.pid)
        kill.assert_called_once_with(self.pid, signal.SIGTERM)
        self.assertNotIn('Collector exited', self.stdout.getvalue())

    def test_reused_pid_while_stopping_gets_no_further_signal(self):
        with (patch.object(helper, 'verify', return_value=self.evidence),
              patch.object(helper, 'process', return_value=dict(self.process, started='2027-01-01T00:00:00+00:00')),
              patch.object(helper.os, 'kill') as kill):
            with self.assertRaisesRegex(helper.UnsafeCollector, 'identity changed while stopping'):
                helper.stop(self.root, self.pid)
        kill.assert_called_once_with(self.pid, signal.SIGTERM)

    def test_process_inspection_failure_does_not_mistake_alive_process_for_exit(self):
        with (patch.object(helper, 'verify', return_value=self.evidence),
              patch.object(helper, 'process', side_effect=helper.UnsafeCollector('ps failed')),
              patch.object(helper, 'process_exists', return_value=True),
              patch.object(helper.os, 'kill') as kill):
            with self.assertRaisesRegex(helper.UnsafeCollector, 'evidence became unavailable'):
                helper.stop(self.root, self.pid)
        kill.assert_called_once_with(self.pid, signal.SIGTERM)

    def test_process_exit_check_does_not_treat_permission_error_as_exit(self):
        with patch.object(helper.os, 'kill', side_effect=ProcessLookupError(errno.ESRCH, 'gone')):
            self.assertFalse(helper.process_exists(self.pid))
        with patch.object(helper.os, 'kill', side_effect=PermissionError(errno.EPERM, 'denied')):
            with self.assertRaises(helper.UnsafeCollector):
                helper.process_exists(self.pid)

    def test_default_main_only_inspects(self):
        with (patch.object(helper.sys, 'platform', 'darwin'),
              patch.object(helper, 'verify', return_value=self.evidence) as verify,
              patch.object(helper, 'stop') as stop):
            self.assertEqual(helper.main([]), 0)
        verify.assert_called_once()
        stop.assert_not_called()
        output = self.stdout.getvalue()
        evidence, end = json.JSONDecoder().raw_decode(output)
        self.assertEqual(evidence, self.evidence)
        self.assertIn('Inspection only. No process was signaled;', output[end:])
        self.assertNotIn('Graceful stop requested', output)
        self.assertEqual(self.stderr.getvalue(), '')

    def test_stop_main_requires_pid_and_never_sends_signals_when_unverified(self):
        with (patch.object(helper.sys, 'platform', 'darwin'),
              patch.object(helper.os, 'kill') as kill):
            self.assertEqual(helper.main(['--stop']), 1)
        kill.assert_not_called()
        self.assertEqual(self.stdout.getvalue(), '')
        self.assertIn('Collector operation stopped: --stop requires an explicit valid --pid', self.stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
