from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from spoondev.cli import main


def account_file(path, count=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE accounts(id TEXT)')
        connection.executemany('INSERT INTO accounts VALUES(?)', [(str(i),) for i in range(count)])


class AutomaticCollectorCliTests(unittest.TestCase):
    def test_options_and_sigterm_are_forwarded_then_handlers_restored(self):
        with tempfile.TemporaryDirectory() as folder:
            auth = Path(folder) / 'accounts.sqlite3'
            db = Path(folder) / 'observations.sqlite3'
            account_file(auth)
            previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

            def stop(database, auth_database, **options):
                self.assertEqual((database, auth_database), (str(db), str(auth)))
                self.assertEqual(options['concurrency'], 2)
                self.assertEqual(options['live_interval'], 120)
                self.assertEqual(options['ranking_interval'], 1800)
                self.assertFalse(options['stop_event'].is_set())
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                self.assertTrue(options['stop_event'].is_set())
                return {'state': 'stopped', 'attempt_count': 3}

            output = io.StringIO()
            with patch('spoondev.worker.run', side_effect=stop), redirect_stdout(output):
                result = main(['--db', str(db), 'collect-auto', '--auth-db', str(auth),
                    '--concurrency', '2', '--live-interval', '120', '--ranking-interval', '1800'])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue().splitlines()[-1])['attempt_count'], 3)
            for sig, handler in previous.items():
                self.assertIs(signal.getsignal(sig), handler)

    def test_duplicate_keeps_existing_collector_and_restores_handlers(self):
        from spoondev.worker import WorkerAlreadyRunning
        with tempfile.TemporaryDirectory() as folder:
            auth = Path(folder) / 'accounts.sqlite3'
            account_file(auth)
            output = io.StringIO()
            previous = signal.getsignal(signal.SIGTERM)
            with patch('spoondev.worker.run', side_effect=WorkerAlreadyRunning('locked')), redirect_stdout(output):
                result = main(['--db', str(Path(folder) / 'db'), 'collect-auto', '--auth-db', str(auth)])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue().splitlines()[-1])['state'], 'already_running')
            self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    def test_missing_or_empty_accounts_do_not_create_observations(self):
        with tempfile.TemporaryDirectory() as folder:
            auth = Path(folder) / 'accounts.sqlite3'
            db = Path(folder) / 'observations.sqlite3'
            with redirect_stderr(io.StringIO()):
                self.assertEqual(main(['--db', str(db), 'collect-auto', '--auth-db', str(auth)]), 1)
            self.assertFalse(auth.exists())
            self.assertFalse(db.exists())
            account_file(auth, count=0)
            with redirect_stderr(io.StringIO()):
                self.assertEqual(main(['--db', str(db), 'collect-auto', '--auth-db', str(auth)]), 1)
            self.assertFalse(db.exists())

    def test_invalid_config_is_rejected_before_any_database_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'db'
            for arguments in (['--concurrency', '0'], ['--concurrency', '17'],
                    ['--live-interval', 'nan'], ['--ranking-interval', 'inf'],
                    ['--live-interval', '29'], ['--ranking-interval', '0']):
                with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    main(['--db', str(db), 'collect-auto', *arguments])
                self.assertFalse(db.exists())

    def test_worker_failure_restores_signal_handlers(self):
        with tempfile.TemporaryDirectory() as folder:
            auth = Path(folder) / 'accounts.sqlite3'
            account_file(auth)
            previous = signal.getsignal(signal.SIGINT)
            with patch('spoondev.worker.run', side_effect=ValueError('cannot start')), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(['collect-auto', '--auth-db', str(auth)]), 1)
            self.assertIs(signal.getsignal(signal.SIGINT), previous)


# Real shell launcher lifecycle with local process doubles. No Spoon or tunnel
# network requests are made; the fake web server only returns localhost 401.
PROCESS_DOUBLE = '''import json,os,signal,sys,threading
from http.server import BaseHTTPRequestHandler,HTTPServer
from pathlib import Path
events=Path(os.environ['SPOONDEV_TEST_EVENTS'])
def record(kind):
    with events.open('a') as stream: stream.write(json.dumps({'event':kind,'pid':os.getpid()})+'\\n')
stop=threading.Event()
signal.signal(signal.SIGTERM,lambda *_:stop.set())
if Path(sys.argv[0]).name=='cloudflared':
    record('tunnel_start')
    print('https://controlled-launcher-test.trycloudflare.com',flush=True)
    stop.wait()
    record('tunnel_stop')
elif sys.argv[1:3]==['-m','spoondev']:
    if sys.argv[3]=='collect-auto':
        record('worker_start');stop.wait();record('worker_stop')
    elif sys.argv[3]=='serve':
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self): self.send_response(401);self.end_headers()
            def log_message(self,*_): pass
        port=int(sys.argv[sys.argv.index('--port')+1])
        server=HTTPServer(('127.0.0.1',port),Handler);server.timeout=.1
        record('web_start')
        while not stop.is_set(): server.handle_request()
        server.server_close();record('web_stop')
    else: raise SystemExit('Unexpected command')
else:
    os.execv(sys.executable,[sys.executable,*sys.argv[1:]])
'''


@unittest.skipUnless(os.name == 'posix', 'Mac/Linux launcher tests')
class MacLauncherCollectorTests(unittest.TestCase):
    def setup_launcher(self, folder, script, collect=None):
        root = Path(folder) / ('SpoonDev-dev' if script == 'start-dev-mac.sh' else 'SpoonDev')
        (root / 'scripts').mkdir(parents=True)
        source = Path(__file__).resolve().parents[1] / 'scripts' / script
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        # Exercise a copy on an ephemeral localhost port without touching a
        # user's running Dev/public server or the cloud preview on port 8080.
        text = re.sub(r'\b808[01]\b', str(port), source.read_text())
        (root / 'scripts' / script).write_text(text)
        account_file(root / 'data/accounts.sqlite3')
        binaries = Path(folder) / 'bin'
        binaries.mkdir()
        for command in ('python', 'cloudflared'):
            path = binaries / command
            path.write_text('#!' + sys.executable + '\n' + PROCESS_DOUBLE)
            path.chmod(0o700)
        events = Path(folder) / 'events.jsonl'
        env = dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ['PATH'],
            SPOONDEV_TEST_EVENTS=str(events))
        env.pop('SPOONDEV_COLLECT', None)
        if collect is not None:
            env['SPOONDEV_COLLECT'] = collect
        process = subprocess.Popen(['bash', str(root / 'scripts' / script)], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return root, events, process

    def events(self, path):
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def wait_for(self, condition, process, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            if process.poll() is not None:
                output, _ = process.communicate()
                self.fail('Launcher exited before its expected state: ' + output)
            time.sleep(.03)
        self.fail('Launcher did not reach its expected state')

    def test_public_reload_keeps_one_collector_and_exit_stops_owned_collector(self):
        with tempfile.TemporaryDirectory() as folder:
            root, events, process = self.setup_launcher(folder, 'start-public-mac.sh')
            try:
                self.wait_for(lambda: (root / 'data/public-run.json').exists(), process)
                self.wait_for(lambda: any(e['event'] == 'worker_start' for e in self.events(events)), process)
                self.assertEqual(sum(e['event'] == 'worker_start' for e in self.events(events)), 1)
                os.kill(process.pid, signal.SIGHUP)
                self.wait_for(lambda: sum(e['event'] == 'web_start' for e in self.events(events)) == 2, process)
                self.assertEqual(sum(e['event'] == 'worker_start' for e in self.events(events)), 1)
                self.assertFalse(any(e['event'] == 'worker_stop' for e in self.events(events)))
                self.assertEqual(json.loads((root / 'data/public-run.json').read_text())['url'],
                    'https://controlled-launcher-test.trycloudflare.com')
            finally:
                process.terminate()
                process.communicate(timeout=10)
            self.assertEqual(sum(e['event'] == 'worker_stop' for e in self.events(events)), 1)
            self.assertFalse((root / 'data/public-run.json').exists())

    def test_dev_is_off_by_default_and_explicit_opt_in_stops_on_exit(self):
        for collect, expected in ((None, 0), ('1', 1)):
            with self.subTest(collect=collect), tempfile.TemporaryDirectory() as folder:
                _, events, process = self.setup_launcher(folder, 'start-dev-mac.sh', collect)
                try:
                    self.wait_for(lambda: any(e['event'] == 'web_start' for e in self.events(events)), process)
                    if expected:
                        self.wait_for(lambda: any(e['event'] == 'worker_start' for e in self.events(events)), process)
                    self.assertEqual(sum(e['event'] == 'worker_start' for e in self.events(events)), expected)
                finally:
                    process.terminate()
                    process.communicate(timeout=10)
                self.assertEqual(sum(e['event'] == 'worker_stop' for e in self.events(events)), expected)

    def test_public_opt_out_keeps_manual_collection_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            root, events, process = self.setup_launcher(folder, 'start-public-mac.sh', '0')
            try:
                self.wait_for(lambda: (root / 'data/public-run.json').exists(), process)
                self.assertFalse(any(e['event'] == 'worker_start' for e in self.events(events)))
            finally:
                process.terminate()
                process.communicate(timeout=10)
