"""Observation persistence must release owned handles and preserve caller transactions."""

from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from spoondev import db, gifts, profiledb


class ObservationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name).resolve() / 'observations.sqlite3'
        db.initialize(self.database)
        self.payload = {
            'room_id': 'room', 'broadcaster': {'id': 'dj', 'name': 'DJ'},
            'listeners': [{'id': 'listener', 'name': 'Listener', 'favorite_temperature': 50}],
            'complete': True, 'observed_at': '2026-10-10T00:00:00+00:00',
        }
        self.metadata = {
            'room_id': 'room', 'broadcaster_id': 'dj',
            'started_at': '2026-10-10T00:00:00+00:00',
            'finished_at': '2026-10-10T00:00:01+00:00',
            'page_count': 1, 'state': 'completed',
        }

    def test_setup_failure_closes_an_owned_connection(self):
        class BrokenSetup(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql == 'PRAGMA foreign_keys=ON':
                    raise sqlite3.OperationalError('setup failed')
                return super().execute(sql, parameters)

        for module in (db, gifts, profiledb):
            with self.subTest(module=module.__name__):
                connection = sqlite3.connect(self.database, factory=BrokenSetup)
                self.addCleanup(connection.close)
                with patch.object(module.sqlite3, 'connect', return_value=connection):
                    with self.assertRaisesRegex(sqlite3.OperationalError, 'setup failed'):
                        module.initialize(self.database)
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute('SELECT 1')

    def test_setup_failure_does_not_close_a_caller_connection(self):
        class BrokenSetup(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql == 'PRAGMA foreign_keys=ON':
                    raise sqlite3.OperationalError('setup failed')
                return super().execute(sql, parameters)

        for module in (db, gifts, profiledb):
            with self.subTest(module=module.__name__):
                with closing(sqlite3.connect(self.database, factory=BrokenSetup)) as connection:
                    with self.assertRaisesRegex(sqlite3.OperationalError, 'setup failed'):
                        module.initialize(connection)
                    self.assertEqual(connection.execute('SELECT 1').fetchone(), (1,))

    def test_successful_owned_write_with_metadata_releases_handle(self):
        connection = sqlite3.connect(self.database)
        self.addCleanup(connection.close)
        with patch('spoondev.db.sqlite3.connect', return_value=connection):
            snapshot = db.save_snapshot(self.database, self.payload, metadata=self.metadata)
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute('SELECT 1')
        with closing(sqlite3.connect(self.database)) as reader:
            self.assertEqual(reader.execute('SELECT snapshot_id FROM observation_attempts').fetchone(),
                             (snapshot,))

    def test_failed_snapshot_rolls_back_and_closes_owned_connection(self):
        connection = sqlite3.connect(self.database)
        self.addCleanup(connection.close)
        invalid = dict(self.metadata, broadcaster_id='different-dj')
        with patch('spoondev.db.sqlite3.connect', return_value=connection):
            with self.assertRaisesRegex(ValueError, 'does not match'):
                db.save_snapshot(self.database, self.payload, metadata=invalid)
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute('SELECT 1')
        with closing(sqlite3.connect(self.database)) as reader:
            for table in ('users', 'snapshots', 'names', 'memberships',
                          'user_attributes', 'observation_attempts'):
                self.assertEqual(reader.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)

    def test_caller_transaction_survives_nested_write_and_rollback(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("INSERT INTO users VALUES ('outside','Outside','2026-10-10T00:00:00+00:00')")
            snapshot = db.save_snapshot(connection, self.payload, metadata=self.metadata)
            with self.assertRaisesRegex(ValueError, 'does not match'):
                db.save_snapshot(connection, self.payload,
                                 metadata=dict(self.metadata, room_id='different-room'))
            self.assertTrue(connection.in_transaction)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT snapshot_id FROM observation_attempts').fetchone(),
                             (snapshot,))
            connection.rollback()
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM users').fetchone()[0], 0)

    @unittest.skipIf(sys.platform == 'win32', 'Unix descriptor limits are unavailable')
    def test_repeated_real_observations_fit_a_small_descriptor_budget_without_gc(self):
        # The subprocess makes a low process limit safe for the rest of the test suite.
        # Automatic GC is disabled: correctness cannot depend on its timing to close SQLite.
        script = r'''
import gc, resource, sys
from pathlib import Path
from spoondev import db

database = Path(sys.argv[1])
_, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
small_limit = 64 if hard == resource.RLIM_INFINITY else min(64, hard)
resource.setrlimit(resource.RLIMIT_NOFILE, (small_limit, hard))
gc.collect()
gc.disable()
payload = {'room_id':'room', 'broadcaster':{'id':'dj','name':'DJ'},
           'listeners':[{'id':'listener','name':'Listener','favorite_temperature':50}],
           'complete':True, 'observed_at':'2026-10-10T00:00:00+00:00'}
metadata = {'room_id':'room','broadcaster_id':'dj','started_at':payload['observed_at'],
            'finished_at':'2026-10-10T00:00:01+00:00','page_count':1,'state':'completed'}
for index in range(100):
    db.initialize(database)
    snapshot = db.save_snapshot(database, payload, metadata=metadata)
    db.record_room_attempt(database, metadata)
    assert db.broadcaster_listeners(database, 'dj')[0]['complete_count'] == index + 1
    assert db.listener_broadcasters(database, 'listener')[0]['user_id'] == 'dj'
    assert len(db.temperature_history(database, 'dj', 'listener')) == index + 1
'''
        result = subprocess.run([sys.executable, '-c', script, str(self.database)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
