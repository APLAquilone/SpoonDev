"""Worker polling must release SQLite descriptors without waiting for GC."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class WorkerDatabaseLifecycleTests(unittest.TestCase):
    def run_limited_worker(self, operation):
        # Keep the lower limit and delayed cyclic GC in a child process. This
        # models launchd's finite descriptor budget without changing the test
        # runner or depending on when Python happens to collect connections.
        program = '''
from contextlib import closing
from pathlib import Path
import gc, resource, sqlite3, sys, threading
from spoondev import worker

database=Path(sys.argv[1])
worker.initialize(database)
worker.enqueue(database,'gifts','10',priority=100)
gc.collect()
soft,hard=resource.getrlimit(resource.RLIMIT_NOFILE)
limit=256 if hard==resource.RLIM_INFINITY else min(256,hard)
resource.setrlimit(resource.RLIMIT_NOFILE,(limit,hard))
gc.disable()
budget=worker._Budget(database,threading.Event(),1)
for index in range(1000):
    budget.cooldown()
    worker._claim(database)
    OPERATION
with closing(sqlite3.connect(database)) as conn:
    row=conn.execute('SELECT failure_count FROM worker_jobs WHERE kind="gifts" AND dj_id="10"').fetchone()
    assert row is not None
    if sys.argv[2]=='failures':assert row[0]==1000,row
print('completed 1000 rounds without cyclic GC')
'''.replace('    OPERATION', '    ' + operation)
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, '-c', program,
                 str(Path(directory) / 'worker.sqlite3'),
                 'failures' if operation.startswith('worker._record_task_failure') else 'poll'],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('completed 1000 rounds', result.stdout)

    @unittest.skipUnless(sys.platform in ('darwin', 'linux'), 'Requires POSIX descriptor limits')
    def test_repeated_polling_stays_within_launchd_descriptor_limit(self):
        self.run_limited_worker('pass')

    @unittest.skipUnless(sys.platform in ('darwin', 'linux'), 'Requires POSIX descriptor limits')
    def test_repeated_failures_release_connections_and_commit_retry_state(self):
        self.run_limited_worker(
            "worker._record_task_failure(database,{'kind':'gifts','dj_id':'10','failure_count':0},RuntimeError('test failure'),3600)")

    @unittest.skipUnless(sys.platform in ('darwin', 'linux'), 'Requires POSIX descriptor limits')
    def test_repeated_complete_live_sweeps_persist_under_descriptor_limit(self):
        program = '''
from contextlib import closing
from pathlib import Path
import gc, resource, sqlite3, sys, threading
from unittest.mock import patch
from spoondev import db, worker

root=Path(sys.argv[1])
database=root/'observations.sqlite3'
auth=root/'accounts.sqlite3'
db.initialize(database)
worker.initialize(database)
with closing(sqlite3.connect(auth)) as conn, conn:
    conn.execute('CREATE TABLE accounts(id TEXT PRIMARY KEY)')
    conn.execute('INSERT INTO accounts VALUES(?)',('a'*32,))
gc.collect()
soft,hard=resource.getrlimit(resource.RLIMIT_NOFILE)
limit=256 if hard==resource.RLIM_INFINITY else min(256,hard)
resource.setrlimit(resource.RLIMIT_NOFILE,(limit,hard))
gc.disable()

def collect(**options):
    stamp=worker._iso()
    snapshot={'room_id':'1','broadcaster':{'id':'10','name':'DJ'},
              'listeners':[{'id':'77','name':'Listener','favorite_temperature':72}],
              'complete':True,'observed_at':stamp}
    metadata={'room_id':'1','broadcaster_id':'10','started_at':stamp,
              'finished_at':stamp,'page_count':1,'state':'completed'}
    options['room_callback'](snapshot,metadata)
    stop.set()
    return [snapshot],[]

rounds=128
with patch('spoondev.worker._discover',return_value={}), patch('spoondev.worker.collect_spoon',side_effect=collect):
    for index in range(rounds):
        stop=threading.Event()
        with closing(sqlite3.connect(database)) as conn, conn:
            conn.execute("UPDATE worker_jobs SET due_at=0 WHERE kind='live'")
        result=worker.run(database,auth,stop_event=stop,concurrency=1)
        assert result=={'state':'stopped','attempt_count':1},result

with closing(sqlite3.connect(database)) as conn:
    for table in ('snapshots','memberships','worker_attempts','worker_snapshot_links','collection_runs'):
        assert conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==rounds,table
    assert conn.execute("SELECT COUNT(*) FROM worker_attempts WHERE state='completed'").fetchone()[0]==rounds
    assert conn.execute("SELECT COUNT(*) FROM collection_runs WHERE state='completed'").fetchone()[0]==rounds
    assert worker.read_status(conn)['state']=='stopped'
print('persisted 128 complete live sweeps without cyclic GC')
'''
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, '-c', program, directory],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('persisted 128 complete live sweeps', result.stdout)


if __name__ == '__main__':
    unittest.main()
