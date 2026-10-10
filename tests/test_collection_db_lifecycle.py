"""Collection must preserve results without retaining SQLite file handles."""
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from spoondev import collection_status


class CollectionDatabaseLifecycleTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.database = Path(folder.name).resolve() / 'observations.sqlite3'

    def test_round_status_is_committed_and_owned_handles_are_closed(self):
        original = sqlite3.connect
        retained = []

        def connect(*args, **kwargs):
            connection = original(*args, **kwargs)
            retained.append(connection)
            self.addCleanup(connection.close)
            return connection

        with patch.object(collection_status.sqlite3, 'connect', new=connect):
            run = collection_status.begin(self.database, 'live', interval=300)
            collection_status.finish(self.database, run, {'complete': True, 'room_count': 2})
        for connection in retained:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute('SELECT 1')
        with closing(original(self.database)) as reader:
            self.assertEqual(reader.execute('SELECT state FROM collection_runs').fetchone(), ('completed',))

    def test_failed_status_write_closes_handle_and_keeps_previous_result(self):
        original = sqlite3.connect
        run = collection_status.begin(self.database, 'live')
        with closing(original(self.database)) as setup, setup:
            setup.execute("""CREATE TRIGGER reject_finish BEFORE UPDATE ON collection_runs
              BEGIN SELECT RAISE(ABORT, 'write rejected'); END""")
        retained = original(self.database)
        self.addCleanup(retained.close)
        with patch.object(collection_status.sqlite3, 'connect', return_value=retained):
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'write rejected'):
                collection_status.finish(self.database, run, {'complete': True, 'room_count': 1})
        with self.assertRaises(sqlite3.ProgrammingError):
            retained.execute('SELECT 1')
        with closing(original(self.database)) as reader:
            self.assertEqual(reader.execute('SELECT state,finished_at FROM collection_runs').fetchone(),
                             ('running', None))

    @unittest.skipUnless(sys.platform in ('darwin', 'linux'), 'Requires POSIX descriptor limits')
    def test_repeated_live_and_monthly_rounds_fit_small_limit_without_gc(self):
        # Stub only the remote API. Exercise real queue claims, round outcomes,
        # snapshot callbacks, monthly persistence and queries in each sweep.
        program = r'''
from contextlib import closing
from pathlib import Path
import gc, resource, sqlite3, sys, threading
from unittest.mock import patch
from spoondev import db, monthly, profiledb, worker

database=Path(sys.argv[1])
db.initialize(database); worker.initialize(database); profiledb.initialize(database)
profiledb.cache_users(database,[{'id':'10','name':'DJ'}])
_,hard=resource.getrlimit(resource.RLIMIT_NOFILE)
limit=64 if hard==resource.RLIM_INFINITY else min(64,hard)
resource.setrlimit(resource.RLIMIT_NOFILE,(limit,hard))
gc.collect(); gc.disable()
stop=threading.Event()

def live(**kwargs):
    stamp=worker._iso()
    snapshot={'room_id':'room','broadcaster':{'id':'10','name':'DJ'},
      'listeners':[{'id':'77','name':'Listener','favorite_temperature':72}],
      'complete':True,'observed_at':stamp}
    metadata={'room_id':'room','broadcaster_id':'10','started_at':stamp,
      'finished_at':stamp,'page_count':1,'state':'completed'}
    kwargs['room_callback'](snapshot,metadata)
    return [snapshot],[]

ranking={'results':[{'user':{'id':77,'nickname':'Listener'},'favoriteTemperature':35}],'next':None}
with patch('spoondev.worker.collect_spoon',new=live), \
     patch('spoondev.monthly.fetch_snapshot',return_value=ranking):
    for index in range(50):
        worker.enqueue(database,'live',priority=90)
        job=worker._claim(database)
        assert job and job['kind']=='live'
        outcome=worker._attempt(database,job,stop,1,0)
        assert outcome['complete'] and outcome['room_count']==1,outcome
        outcome=monthly.collect_monthly(database,dj_ids=['10'],concurrency=1)
        assert outcome['complete'] and outcome['user_count']==1,outcome
with closing(sqlite3.connect(database)) as reader:
    assert reader.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0]==50
    assert reader.execute('SELECT COUNT(*) FROM observation_attempts').fetchone()[0]==50
    assert reader.execute("SELECT COUNT(*) FROM worker_attempts WHERE state='completed'").fetchone()[0]==50
    assert reader.execute("SELECT COUNT(*) FROM collection_runs WHERE state='completed'").fetchone()[0]==50
    assert reader.execute('SELECT COUNT(*) FROM monthly_dj_snapshots').fetchone()[0]==50
print('50 complete live and monthly rounds saved without cyclic GC')
'''
        result = subprocess.run([sys.executable, '-c', program, str(self.database)],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('50 complete live and monthly rounds', result.stdout)


if __name__ == '__main__':
    unittest.main()
