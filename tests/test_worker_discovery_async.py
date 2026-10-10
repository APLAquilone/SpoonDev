"""Listener scans remain available during slow or damaged target discovery."""
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from spoondev import accounts, db, worker


class DiscoverySchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.database = self.root / 'observations.sqlite3'
        self.auth = self.root / 'auth.sqlite3'
        db.initialize(self.database)
        worker.initialize(self.database)
        with sqlite3.connect(self.auth) as conn:
            conn.execute('CREATE TABLE accounts(id TEXT PRIMARY KEY)')
            conn.execute('INSERT INTO accounts VALUES(?)', ('a' * 32,))
        self.private = accounts.private(self.auth, 'a' * 32)
        with sqlite3.connect(self.private) as conn:
            conn.execute('UPDATE account_settings SET spoon_id=10 WHERE singleton=1')

    def test_listener_scan_starts_while_discovery_is_still_blocked(self):
        stop = threading.Event()
        discovery_started = threading.Event()
        discovery_finished = threading.Event()
        scanned = []

        def discover(*args, **kwargs):
            discovery_started.set()
            self.assertTrue(kwargs['stop_event'].wait(2))
            discovery_finished.set()
            return {}

        def attempt(database, job, *args):
            self.assertTrue(discovery_started.wait(1))
            self.assertFalse(discovery_finished.is_set())
            scanned.append(job['kind'])
            stop.set()
            return {'complete': True}

        with patch('spoondev.worker._discover', side_effect=discover), \
                patch('spoondev.worker._attempt', side_effect=attempt):
            worker.run(self.database, self.auth, stop_event=stop, concurrency=2)
        self.assertEqual(scanned, ['live'])
        self.assertTrue(discovery_finished.is_set())

    def test_long_rankings_cannot_take_the_reserved_listener_slot(self):
        stop = threading.Event()
        first_live = threading.Event()
        two_rankings = threading.Event()
        second_live = threading.Event()
        guard = threading.Lock()
        live_count = 0
        rank_count = 0
        failures = []
        for uid in ('10', '20', '30'):
            worker.enqueue(self.database, 'monthly', uid, priority=100)

        def attempt(database, job, *args):
            nonlocal live_count, rank_count
            if job['kind'] == 'live':
                with guard:
                    live_count += 1
                    count = live_count
                with sqlite3.connect(database, timeout=5) as conn:
                    conn.execute("UPDATE worker_jobs SET state='completed',due_at=? WHERE kind='live'",
                                 (time.time() + 3600,))
                if count == 1:
                    first_live.set()
                else:
                    second_live.set()
                    stop.set()
            else:
                with guard:
                    rank_count += 1
                    if rank_count == 2:
                        two_rankings.set()
                stop.wait(3)
            return {'complete': True}

        def run():
            try:
                worker.run(self.database, self.auth, stop_event=stop, concurrency=3)
            except BaseException as exc:
                failures.append(exc)

        with patch('spoondev.worker._discover', return_value={}), \
                patch('spoondev.worker._attempt', side_effect=attempt):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(first_live.wait(2))
                self.assertTrue(two_rankings.wait(2))
                with sqlite3.connect(self.database, timeout=5) as conn:
                    conn.execute("UPDATE worker_jobs SET due_at=? WHERE kind='live'", (time.time() - 1,))
                self.assertTrue(second_live.wait(2), 'Rankings prevented the due listener scan')
            finally:
                stop.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(live_count, 2)
        self.assertEqual(rank_count, 2)

    def test_unreadable_private_account_does_not_discard_other_accounts(self):
        uid = 'b' * 32
        with sqlite3.connect(self.auth) as conn:
            conn.execute('INSERT INTO accounts VALUES(?)', (uid,))
        bad_private = accounts.private(self.auth, uid)
        Path(bad_private).write_bytes(b'not a database')
        before = Path(self.private).read_bytes()
        warnings = []
        owners, saved = worker._account_inputs(self.auth, warnings=warnings)
        self.assertEqual((owners, saved), ({'10'}, set()))
        self.assertEqual(len(warnings), 1)
        self.assertIn('file is not a database', warnings[0])
        self.assertNotIn(str(bad_private), warnings[0])
        result = worker._discover(self.database, self.auth)
        self.assertEqual(result['bound_dj_count'], 1)
        self.assertEqual(len(result['warnings']), 1)
        with sqlite3.connect(self.database) as conn:
            self.assertTrue(any(j['kind'] == 'live' for j in worker.read_jobs(conn)))
        self.assertEqual(Path(self.private).read_bytes(), before)

    def test_sql_time_budget_interrupts_discovery_without_changing_data(self):
        before = self.database.read_bytes()
        with self.assertRaisesRegex(sqlite3.OperationalError, 'interrupted'):
            with worker._readonly(self.database, deadline=time.monotonic() + .02) as conn:
                conn.execute('''WITH RECURSIVE n(x) AS (
                    VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000000)
                    SELECT SUM(x) FROM n''').fetchone()
        self.assertEqual(self.database.read_bytes(), before)

    def test_stopping_worker_interrupts_in_progress_discovery_sql(self):
        stop = threading.Event()
        started = threading.Event()
        errors = []

        def query():
            try:
                with worker._readonly(self.database, stop_event=stop) as conn:
                    started.set()
                    conn.execute('''WITH RECURSIVE n(x) AS (
                        VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000000)
                        SELECT SUM(x) FROM n''').fetchone()
            except sqlite3.OperationalError as exc:
                errors.append(str(exc))

        thread = threading.Thread(target=query)
        thread.start()
        try:
            self.assertTrue(started.wait(1))
            stop.set()
            thread.join(2)
        finally:
            stop.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ['interrupted'])

    def test_next_discovery_interval_starts_after_previous_completion(self):
        stop = threading.Event()
        starts = []
        finishes = []

        def discover(*args, **kwargs):
            starts.append(time.monotonic())
            if len(starts) == 1:
                stop.wait(.08)
            else:
                stop.set()
            finishes.append(time.monotonic())
            return {}

        def attempt(*args):
            stop.wait(2)
            return {'complete': True}

        with patch('spoondev.worker._discover', side_effect=discover), \
                patch('spoondev.worker._attempt', side_effect=attempt), \
                patch('spoondev.worker.DISCOVERY_INTERVAL', .04):
            worker.run(self.database, self.auth, stop_event=stop, concurrency=2)
        self.assertEqual(len(starts), 2)
        self.assertGreaterEqual(starts[1] - finishes[0], .04)

    def test_successful_discovery_clears_only_discovery_errors(self):
        for message, expected in (
                ('Account discovery failed (OperationalError): database is locked.', None),
                ('Collection task failed (RuntimeError): task failed',
                 'Collection task failed (RuntimeError): task failed')):
            with self.subTest(message=message):
                stop = threading.Event()
                calls = []
                errors = []

                def discover(*args, **kwargs):
                    calls.append(True)
                    with sqlite3.connect(self.database, timeout=5) as conn:
                        if len(calls) == 1:
                            conn.execute('UPDATE worker_state SET last_error=? WHERE singleton=1', (message,))
                        else:
                            errors.append(conn.execute('SELECT last_error FROM worker_state WHERE singleton=1').fetchone()[0])
                            stop.set()
                    return {}

                def attempt(*args):
                    stop.wait(2)
                    return {'complete': True}

                with patch('spoondev.worker._discover', side_effect=discover), \
                        patch('spoondev.worker._attempt', side_effect=attempt), \
                        patch('spoondev.worker.DISCOVERY_INTERVAL', .01):
                    worker.run(self.database, self.auth, stop_event=stop, concurrency=2)
                self.assertEqual(errors, [expected])


if __name__ == '__main__':
    unittest.main()
