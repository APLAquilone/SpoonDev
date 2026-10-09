"""Fetch boundaries and positives survive partial/rate-limited live rounds."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spoondev import db
from spoondev.collector import FetchError
from spoondev.spoon import collect_spoon, SpoonRateLimit


BASE = 'https://jp-api.spooncast.net'
HOST = {'id': 10, 'nickname': 'DJ'}
USER = {'id': 20, 'nickname': 'Listener'}


def page(rows, next_url=''):
    return {'status_code': 200, 'results': rows, 'next': next_url}


class ObservationMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / 'observations.sqlite3'
        db.initialize(self.database)

    def callback(self, snapshot, metadata):
        if snapshot is None:
            db.record_room_attempt(self.database, metadata, collection_run_id=42)
        else:
            db.save_snapshot(self.database, snapshot, metadata=metadata, collection_run_id=42)

    def records(self):
        with sqlite3.connect(self.database) as conn:
            return conn.execute('''SELECT room_id,state,page_count,snapshot_id,
                collection_run_id,started_at,finished_at FROM observation_attempts ORDER BY id''').fetchall()

    def test_completed_room_survives_later_room_rate_limit(self):
        responses = {
            BASE+'/lives/': page([{'id': 1, 'author': HOST}, {'id': 2, 'author': HOST}]),
            BASE+'/lives/1/listeners/': page([USER]),
            BASE+'/lives/2/listeners/': FetchError('url', 'HTTP429', 429, 60),
        }
        with patch('spoondev.spoon.fetch_snapshot', side_effect=lambda url, **kw: responses[url]), \
                self.assertRaises(SpoonRateLimit):
            collect_spoon(concurrency=1, room_callback=self.callback)
        rows = self.records()
        self.assertEqual([(r[0], r[1], r[2]) for r in rows], [('1', 'completed', 1), ('2', 'failed', 0)])
        self.assertIsNotNone(rows[0][3])
        self.assertIsNone(rows[1][3])
        self.assertEqual(rows[0][4], 42)
        self.assertLessEqual(rows[0][5], rows[0][6])
        with sqlite3.connect(self.database) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM memberships WHERE listener_id=?', ('20',)).fetchone()[0], 1)

    def test_positive_partial_page_survives_its_own_rate_limit(self):
        next_url = BASE+'/lives/1/listeners/?cursor=next'
        responses = {
            BASE+'/lives/': page([{'id': 1, 'author': HOST}]),
            BASE+'/lives/1/listeners/': page([USER], next_url),
            next_url: FetchError(next_url, 'HTTP429', 429, 120),
        }
        with patch('spoondev.spoon.fetch_snapshot', side_effect=lambda url, **kw: responses[url]), \
                self.assertRaises(SpoonRateLimit):
            collect_spoon(room_callback=self.callback)
        row = self.records()[0]
        self.assertEqual(row[:3], ('1', 'partial', 1))
        with sqlite3.connect(self.database) as conn:
            self.assertEqual(conn.execute('SELECT complete FROM snapshots').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT listener_id FROM memberships').fetchone()[0], '20')

    def test_failed_first_page_records_attempt_without_empty_snapshot(self):
        responses = {
            BASE+'/lives/': page([{'id': 1, 'author': HOST}]),
            BASE+'/lives/1/listeners/': FetchError('url', 'HTTP503', 503),
        }
        with patch('spoondev.spoon.fetch_snapshot', side_effect=lambda url, **kw: responses[url]):
            snapshots, errors = collect_spoon(room_callback=self.callback)
        self.assertEqual(snapshots, [])
        self.assertTrue(errors)
        self.assertEqual(self.records()[0][:4], ('1', 'failed', 0, None))

    def test_invalid_metadata_rolls_back_the_observation(self):
        snapshot = {'room_id': '1', 'broadcaster': {'id': '10', 'name': 'DJ'},
                    'listeners': [{'id': '20', 'name': 'Listener'}], 'complete': True,
                    'observed_at': '2026-10-09T03:00:00+00:00'}
        metadata = {'room_id': '1', 'broadcaster_id': '10', 'page_count': 1, 'state': 'completed',
                    'started_at': '2026-10-09T03:01:00+00:00', 'finished_at': '2026-10-09T03:00:00+00:00'}
        with self.assertRaises(ValueError):
            db.save_snapshot(self.database, snapshot, metadata=metadata)
        with sqlite3.connect(self.database) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM observation_attempts').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM users').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
