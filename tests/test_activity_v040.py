from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from spoondev import fans, profiledb, webdata, worker
from spoondev.db import initialize, record_room_attempt, save_snapshot


class ActivityCadenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / 'observations.sqlite3'
        self.private = Path(self.temp.name) / 'private.sqlite3'
        initialize(self.database)
        profiledb.initialize(self.database)
        worker.initialize(self.database)
        profiledb.initialize(self.private)
        fans.initialize(self.private)
        fans.import_followers(self.private, {
            'owner': {'id': '10', 'name': 'own DJ'},
            'followers': [{'id': '20', 'name': 'listener'}], 'complete': True,
        })
        self.now = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)

    def stamp(self, seconds=0):
        return (self.now - timedelta(seconds=seconds)).isoformat(timespec='microseconds')

    def observe(self, age, *, temperature=25, complete=True, room='room', dj='30', users=None):
        return save_snapshot(self.database, {
            'room_id': room, 'broadcaster': {'id': dj, 'name': 'another DJ'},
            'listeners': users if users is not None else [
                {'id': '20', 'name': 'listener', 'favorite_temperature': temperature}],
            'complete': complete, 'observed_at': self.stamp(age),
        })

    def cadence(self, *, duration=1200, interval=300):
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.executescript('''CREATE TABLE IF NOT EXISTS collection_runs (
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT, state TEXT NOT NULL,
                interval_seconds REAL NOT NULL, details TEXT NOT NULL);''')
            run_id = conn.execute('''INSERT INTO collection_runs
                (kind,target,started_at,finished_at,state,interval_seconds,details)
                VALUES('live','',?,?,'completed',?,'{}')''',
                (self.stamp(duration + interval), self.stamp(interval), interval)).lastrowid
            attempt_id = conn.execute('''INSERT INTO worker_attempts
                (kind,dj_id,started_at,finished_at,state,run_id)
                VALUES('live','',?,?,'completed',?)''',
                (self.stamp(duration + interval), self.stamp(interval), run_id)).lastrowid
            conn.execute('''INSERT INTO worker_jobs
                (kind,dj_id,priority,created_at,requested_at,due_at,state,last_attempt_id)
                VALUES('live','',90,?,?,?,'completed',?)''',
                (self.now.timestamp() - 3600, self.now.timestamp() - 3600,
                 self.now.timestamp(), attempt_id))
            conn.execute('''UPDATE worker_state SET state='running',started_at=?,
                heartbeat_at=?,stopped_at=NULL,cooldown_until=0 WHERE singleton=1''',
                (self.stamp(max(3600, duration + interval + 600)), self.stamp(2)))

    def change(self, sql, values=()):
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute(sql, values)

    def attempt(self, *, age=60, state='failed', room='new-room', dj='30'):
        record_room_attempt(self.database, {
            'room_id': room, 'broadcaster_id': dj, 'started_at': self.stamp(age + 30),
            'finished_at': self.stamp(age), 'page_count': 1, 'state': state,
        })

    def response(self):
        with patch.object(webdata, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = self.now
            return webdata.fan_destinations(
                self.database, '10', private_database=self.private)

    def test_normal_twenty_minute_scan_and_five_minute_wait_keep_its_recent_presence(self):
        # The first room's saved observation ages beyond ten minutes while the
        # rest of a normal scan is still being fetched, before the next scan.
        self.cadence(duration=1200, interval=300)
        self.observe(23 * 60)
        result = self.response()
        self.assertEqual(result['current_seconds'], 1500)
        self.assertEqual(result['activity_counts'], {'current': 1, 'recent': 0, 'registered': 0})
        self.assertTrue(result['fans'][0]['recent'])
        self.assertEqual(result['current_window_basis'], 'observed_scan_cadence')
        self.assertEqual(result['checked_at'], self.now.isoformat())
        self.assertEqual(result['fans'][0]['last_live_at'], self.stamp(23 * 60))

    def test_current_window_never_extends_beyond_the_recent_window(self):
        self.cadence(duration=2400, interval=300)
        self.observe(1800)
        result = self.response()
        self.assertEqual(result['current_seconds'], 1800)
        self.assertTrue(result['fans'][0]['current'])
        self.assertTrue(result['fans'][0]['recent'])
        self.observe(1799, users=[])
        self.assertEqual(self.response()['fans'][0]['activity_state'], 'recent')

    def test_expiry_boundaries_and_fan_favorite_parity(self):
        self.cadence(duration=1200, interval=300)
        for age, expected in ((1501, 'recent'), (1500, 'current'), (1499, 'current')):
            with self.subTest(age=age):
                self.observe(age, dj=str(age), room=str(age))
                # Isolate the relevant ID because a single fresh sighting in a
                # second DJ must legitimately preserve a current classification.
                with closing(sqlite3.connect(self.database)) as conn, conn:
                    conn.execute('DELETE FROM memberships WHERE snapshot_id NOT IN '
                                 '(SELECT MAX(id) FROM snapshots)')
                fan_result = self.response()
                with patch.object(webdata, 'datetime', wraps=datetime) as clock:
                    clock.now.return_value = self.now
                    favorite_result = webdata.favorite_activity(self.database, ['20'])
                self.assertEqual(fan_result['fans'][0]['activity_state'], expected)
                self.assertEqual(fan_result['current_seconds'], favorite_result['current_seconds'])
                for key in ('current', 'recent', 'last_live_at', 'activity_state'):
                    self.assertEqual(fan_result['fans'][0][key], favorite_result['users'][0][key])

    def test_stopped_failed_partial_cooldown_and_stale_workers_do_not_extend(self):
        self.cadence()
        self.observe(20 * 60)
        changes = [
            ("UPDATE worker_state SET state='stopped'", ()),
            ("UPDATE worker_state SET state='unconfirmed'", ()),
            ("UPDATE worker_state SET stopped_at=?", (self.stamp(1),)),
            ("UPDATE worker_state SET heartbeat_at=?", (self.stamp(31),)),
            ("UPDATE worker_state SET heartbeat_at=?", (self.stamp(-1),)),
            ("UPDATE worker_state SET heartbeat_at='not a date'", ()),
            ("UPDATE worker_state SET started_at=?", (self.stamp(60),)),
            ("UPDATE worker_state SET cooldown_until=?", (self.now.timestamp() + 60,)),
            ("UPDATE worker_jobs SET state='failed'", ()),
            ("UPDATE worker_jobs SET state='partial'", ()),
            ("UPDATE worker_jobs SET state='cooldown'", ()),
            ("UPDATE worker_attempts SET state='partial'", ()),
            ("UPDATE collection_runs SET state='partial'", ()),
            ("UPDATE collection_runs SET interval_seconds=5", ()),
            ("UPDATE collection_runs SET interval_seconds=?", (float('inf'),)),
        ]
        for sql, values in changes:
            with self.subTest(sql=sql, values=values):
                self.change("UPDATE worker_state SET state='running',stopped_at=NULL,"
                            "heartbeat_at=?,started_at=?,cooldown_until=0",
                            (self.stamp(2), self.stamp(3600)))
                self.change("UPDATE worker_jobs SET state='completed'")
                self.change("UPDATE worker_attempts SET state='completed'")
                self.change("UPDATE collection_runs SET state='completed',interval_seconds=300")
                self.change(sql, values)
                result = self.response()
                self.assertEqual(result['current_seconds'], 600)
                self.assertEqual(result['fans'][0]['activity_state'], 'recent')
                self.assertEqual(result['current_window_basis'], 'fixed_window')

    def test_healthy_next_scan_can_use_the_last_completed_scan_cadence(self):
        self.cadence()
        self.observe(20 * 60)
        with closing(sqlite3.connect(self.database)) as conn, conn:
            run_id = conn.execute('''INSERT INTO collection_runs
                (kind,target,started_at,state,interval_seconds,details)
                VALUES('live','',?,'running',300,'{}')''', (self.stamp(120),)).lastrowid
            attempt_id = conn.execute('''INSERT INTO worker_attempts
                (kind,dj_id,started_at,state,run_id) VALUES('live','',?,'running',?)''',
                (self.stamp(120), run_id)).lastrowid
            conn.execute("UPDATE worker_jobs SET state='running',last_attempt_id=?", (attempt_id,))
        result = self.response()
        self.assertEqual(result['current_seconds'], 1500)
        self.assertTrue(result['fans'][0]['current'])

    def test_stalled_live_job_heartbeat_alone_does_not_extend(self):
        self.cadence()
        self.observe(20 * 60)
        self.change('UPDATE worker_attempts SET started_at=?,finished_at=?',
                    (self.stamp(3000), self.stamp(1800)))
        self.assertEqual(self.response()['current_seconds'], 600)
        with closing(sqlite3.connect(self.database)) as conn, conn:
            attempt_id = conn.execute('''INSERT INTO worker_attempts
                (kind,dj_id,started_at,state) VALUES('live','',?,'running')''',
                (self.stamp(1700),)).lastrowid
            conn.execute("UPDATE worker_jobs SET state='running',last_attempt_id=?", (attempt_id,))
        self.assertEqual(self.response()['current_seconds'], 600)

    def test_latest_missing_temperature_absence_and_room_failure_downgrade(self):
        self.cadence()
        self.observe(20 * 60)
        self.observe(15 * 60, temperature=None)
        self.assertEqual(self.response()['fans'][0]['activity_state'], 'recent')
        self.observe(14 * 60, temperature=0)
        self.assertTrue(self.response()['fans'][0]['current'])
        self.observe(13 * 60, users=[])
        self.assertFalse(self.response()['fans'][0]['current'])
        self.observe(12 * 60, temperature=30)
        self.assertTrue(self.response()['fans'][0]['current'])
        # A failed new room for the same DJ has no snapshot to invalidate the
        # old room. The saved attempt supplies the required negative evidence.
        self.attempt(age=60, room='changed-room')
        result = self.response()
        self.assertFalse(result['fans'][0]['current'])
        self.assertEqual(result['fans'][0]['live'][0]['favorite_temperature'], 30)
        self.observe(30, temperature=31, room='changed-room')
        self.assertTrue(self.response()['fans'][0]['current'])

    def test_equal_fetch_timestamps_use_saved_attempt_order(self):
        snapshot_id = self.observe(300)
        def record_positive(sid):
            record_room_attempt(self.database, {
                'room_id': 'room', 'broadcaster_id': '30', 'started_at': self.stamp(330),
                'finished_at': self.stamp(300), 'page_count': 1, 'state': 'completed',
            }, snapshot_id=sid)
        record_positive(snapshot_id)
        self.attempt(age=300)
        self.assertFalse(self.response()['fans'][0]['current'])
        snapshot_id = self.observe(300, temperature=30)
        record_positive(snapshot_id)
        self.assertTrue(self.response()['fans'][0]['current'])

    def test_partial_snapshot_itself_does_not_get_extra_lifetime(self):
        self.cadence()
        self.observe(20 * 60, complete=False)
        self.assertFalse(self.response()['fans'][0]['current'])
        self.observe(5 * 60, complete=False)
        self.assertTrue(self.response()['fans'][0]['current'])
        self.observe(60, users=[], complete=False)
        self.assertFalse(self.response()['fans'][0]['current'])

    def test_future_failure_or_sighting_cannot_invalidate_or_create_presence(self):
        self.cadence()
        self.observe(20 * 60)
        self.attempt(age=-60)
        self.observe(-120, users=[])
        self.assertTrue(self.response()['fans'][0]['current'])
        self.observe(-30, dj='40', users=[
            {'id': '99', 'name': 'future only', 'favorite_temperature': 99}])
        with patch.object(webdata, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = self.now
            result = webdata.favorite_activity(self.database, ['99'])
        self.assertFalse(result['users'][0]['current'])
        self.assertIsNone(result['users'][0]['last_live_at'])

    def test_missing_or_previous_worker_metadata_keeps_default_window(self):
        self.observe(20 * 60)
        self.assertEqual(self.response()['current_seconds'], 600)
        self.cadence()
        self.change('UPDATE worker_state SET started_at=?', (self.stamp(60),))
        self.assertEqual(self.response()['current_seconds'], 600)
        self.change('DROP TABLE collection_runs')
        self.assertEqual(self.response()['current_seconds'], 600)

    def test_other_account_same_owner_and_same_name_do_not_mix_fan_registration(self):
        self.cadence()
        self.observe(20 * 60, users=[
            {'id': '20', 'name': 'same', 'favorite_temperature': 25},
            {'id': '21', 'name': 'same', 'favorite_temperature': 30}])
        sibling = Path(self.temp.name) / 'sibling-private.sqlite3'
        profiledb.initialize(sibling)
        fans.initialize(sibling)
        fans.import_followers(sibling, {
            'owner': {'id': '10', 'name': 'own DJ'},
            'followers': [{'id': '21', 'name': 'same'}], 'complete': True,
        })
        before = (self.database.read_bytes(), self.private.read_bytes(), sibling.read_bytes())
        own = self.response()
        with patch.object(webdata, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = self.now
            other = webdata.fan_destinations(self.database, '10', private_database=sibling)
        self.assertEqual([user['id'] for user in own['fans']], ['20'])
        self.assertEqual([user['id'] for user in other['fans']], ['21'])
        self.assertEqual(before, (self.database.read_bytes(), self.private.read_bytes(), sibling.read_bytes()))

    def test_legacy_observations_without_attempt_table_are_still_readable(self):
        self.observe(60)
        self.change('DROP TABLE observation_attempts')
        self.assertTrue(self.response()['fans'][0]['current'])

    def test_saved_cohort_candidates_share_the_cadence_without_becoming_fans(self):
        from spoondev.fan_cohorts import candidates
        self.cadence()
        self.observe(3600, dj='10', room='own-room')
        self.observe(23 * 60)
        month = self.now.astimezone(webdata.ZoneInfo('Asia/Tokyo')).strftime('%Y-%m')
        profiledb.save_dj_ranking(self.database, {'id': '10', 'name': 'own DJ'}, [
            {'user': {'id': '20', 'name': 'listener'}, 'temperature': 50}],
            month, True, self.stamp(60))
        # A separate account has not imported a fan list. These sources are
        # saved visits/ranking membership, never verified follows.
        empty_private = Path(self.temp.name) / 'empty-private.sqlite3'
        profiledb.initialize(empty_private)
        fans.initialize(empty_private)
        for source in ('observed', 'monthly'):
            with self.subTest(source=source):
                result = candidates(self.database, '10', private_database=empty_private,
                                    source=source, now=self.now)
                self.assertEqual(result['current_seconds'], 1500)
                self.assertEqual(result['current_window_basis'], 'observed_scan_cadence')
                self.assertEqual(result['activity_counts']['current'], 1)
                self.assertEqual([user['id'] for user in result['users']], ['20'])
                self.assertTrue(result['users'][0]['current'])
                self.assertFalse(result['users'][0]['registered_fan'])


if __name__ == '__main__':
    unittest.main()
