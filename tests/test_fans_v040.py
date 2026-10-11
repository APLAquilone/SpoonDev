from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from spoondev import fan_cohorts, fans, profiledb
from spoondev.db import initialize, save_snapshot


class FanRegistrationV040Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'private.sqlite3'
        profiledb.initialize(self.db)
        fans.initialize(self.db)
        self.owner = {'id': '10', 'name': 'owner', 'tag': 'owner-tag'}

    def put(self, users, complete=False, *, database=None, owner=None):
        return fans.import_followers(database or self.db, {
            'owner': owner or self.owner, 'followers': users, 'complete': complete})

    def remove(self, uid, *, owner='10', database=None):
        return fans.remove_follower(database or self.db,
                                    {'owner_id': owner, 'user_id': uid, 'confirm': True})

    def rows(self, table, database=None):
        with sqlite3.connect(database or self.db) as conn:
            return conn.execute('SELECT * FROM ' + table + ' ORDER BY 1,2').fetchall()

    def test_remove_one_preserves_binding_favorites_profiles_and_other_fans(self):
        self.put([{'id': '20', 'name': 'same'}, {'id': '21', 'name': 'same'}], True)
        self.put(['20'], True, owner={'id': '11', 'name': 'other owner'})
        with sqlite3.connect(self.db) as conn:
            conn.executescript('''CREATE TABLE favorites(id TEXT PRIMARY KEY,name TEXT);
                INSERT INTO favorites VALUES('20','same');
                CREATE TABLE account_settings(spoon_id TEXT);
                INSERT INTO account_settings VALUES('10');''')
            old_owner = conn.execute("SELECT name,tag,imported_at FROM fan_owners WHERE id='10'").fetchone()
        profiles = self.rows('profile_users')
        result = self.remove('20')
        self.assertEqual(result['deleted_count'], 1)
        self.assertEqual(result['fan_count'], 1)
        self.assertFalse(result['complete'])
        self.assertIsNotNone(result['removed_at'])
        self.assertEqual(self.rows('registered_fans'), [('10', '21', 'same', None), ('11', '20', None, None)])
        self.assertEqual(self.rows('favorites'), [('20', 'same')])
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT spoon_id FROM account_settings').fetchone(), ('10',))
            self.assertEqual(conn.execute("SELECT name,tag,imported_at FROM fan_owners WHERE id='10'").fetchone(), old_owner)
            self.assertEqual(conn.execute("SELECT complete FROM fan_owners WHERE id='11'").fetchone(), (1,))
        self.assertEqual(self.rows('profile_users'), profiles)

    def test_last_removal_keeps_registration_and_clear_is_separate_reset(self):
        self.put(['20'], True)
        self.assertEqual(self.remove('20')['fan_count'], 0)
        self.assertEqual(len(self.rows('fan_owners')), 1)
        retry = self.remove('20')
        self.assertEqual(retry['deleted_count'], 0)
        self.assertIsNone(retry['removed_at'])
        self.assertFalse(retry['complete'])
        fans.clear_followers(self.db, {'owner_id': '10', 'confirm': True})
        self.assertEqual(self.rows('fan_owners'), [])

    def test_unknown_removal_is_idempotent_and_preserves_completeness(self):
        self.put(['20'], True)
        before = self.rows('fan_owners')
        result = self.remove('999')
        self.assertEqual(result['deleted_count'], 0)
        self.assertTrue(result['complete'])
        self.assertEqual(self.rows('fan_owners'), before)
        self.assertEqual(self.remove('20', owner='999')['fan_count'], 0)
        self.assertEqual(self.rows('fan_owners'), before)

    def test_invalid_delete_does_not_mutate_any_settings(self):
        self.put(['20', '21'], True)
        before = self.db.read_bytes()
        invalid = [None, {}, {'owner_id': '10', 'user_id': '20', 'confirm': 1},
                   {'owner_id': '10', 'user_id': '20', 'confirm': False}]
        for key in ('owner_id', 'user_id'):
            for value in (True, False, 0, -1, 1.5, '０１', '1; DELETE', '1,2', '1'*21, None):
                payload = {'owner_id': '10', 'user_id': '20', 'confirm': True}
                payload[key] = value
                invalid.append(payload)
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                fans.remove_follower(self.db, payload)
            self.assertEqual(self.db.read_bytes(), before)
        result = fans.remove_follower(self.db, {'owner_id': '00010', 'user_id': 20, 'confirm': True})
        self.assertEqual((result['owner_id'], result['user_id']), ('10', '20'))

    def test_two_accounts_and_global_observations_are_untouched(self):
        other = Path(self.temp.name) / 'second-account.sqlite3'
        profiledb.initialize(other)
        fans.initialize(other)
        self.put(['20', '21'], True)
        self.put(['20', '21'], True, database=other)
        public = Path(self.temp.name) / 'public.sqlite3'
        initialize(public)
        save_snapshot(public, {'room_id': 'r', 'broadcaster': self.owner,
            'listeners': [{'id': '20', 'name': 'listener'}], 'complete': True,
            'observed_at': '2026-10-11T01:00:00+00:00'})
        public_before, other_before = public.read_bytes(), other.read_bytes()
        self.remove('20')
        self.assertEqual(public.read_bytes(), public_before)
        self.assertEqual(other.read_bytes(), other_before)

    def test_mobile_partial_additions_merge_without_wiping_existing_list(self):
        self.put([{'id': '20', 'name': 'same', 'tag': 'first'}], True)
        result = self.put([{'id': '21', 'name': 'same', 'tag': 'second'},
                           {'id': '22', 'name': 'new'}, {'id': '22', 'name': 'new'}])
        self.assertEqual(result['fan_count'], 3)
        self.assertEqual(result['imported_count'], 2)
        self.assertFalse(result['complete'])
        self.assertEqual([row[1] for row in self.rows('registered_fans')], ['20', '21', '22'])

    def test_caller_transaction_can_roll_back_remove_and_partial_add(self):
        self.put(['20'], True)
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        conn.execute('BEGIN')
        self.remove('20', database=conn)
        self.put(['21'], database=conn)
        self.assertEqual(conn.execute('SELECT user_id FROM registered_fans').fetchall(), [('21',)])
        conn.rollback()
        self.assertEqual(conn.execute('SELECT user_id FROM registered_fans').fetchall(), [('20',)])
        self.assertEqual(conn.execute('SELECT complete FROM fan_owners').fetchone(), (1,))


class SavedOwnFanCandidatesV040Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'public.sqlite3'
        initialize(self.db)
        profiledb.initialize(self.db)
        fans.initialize(self.db)
        self.private = Path(self.temp.name) / 'private.sqlite3'
        profiledb.initialize(self.private)
        fans.initialize(self.private)
        self.now = datetime(2026, 10, 11, 2, tzinfo=timezone.utc)
        self.owner = {'id': '10', 'name': 'owner'}

    def observe(self, dj, users, stamp=None, complete=True):
        save_snapshot(self.db, {'room_id': 'room-'+dj, 'broadcaster': {'id': dj, 'name': 'DJ'+dj},
            'listeners': [{'id': uid, 'name': name, 'favorite_temperature': temp}
                          for uid, name, temp in users], 'complete': complete,
            'observed_at': (stamp or self.now-timedelta(hours=2)).isoformat()})

    def get(self, **kwargs):
        return fan_cohorts.candidates(self.db, '10', private_database=self.private,
                                     now=self.now, **kwargs)

    def test_own_candidates_work_without_import_and_show_newer_other_room(self):
        self.observe('10', [('20', 'same', 40), ('21', 'same', None)])
        self.observe('30', [('20', 'same', 0)], self.now-timedelta(minutes=2))
        self.observe('99', [('22', 'unrelated', 30)])
        before = self.private.read_bytes()
        result = self.get()
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['total'], 2)
        users = {user['id']: user for user in result['users']}
        self.assertEqual(set(users), {'20', '21'})
        self.assertTrue(users['20']['current'])
        self.assertEqual(users['20']['live'][0]['user_id'], '30')
        self.assertEqual(users['20']['live'][0]['favorite_temperature'], 0)
        self.assertFalse(users['21']['recent'])
        self.assertFalse(any(user['registered_fan'] for user in users.values()))
        self.assertEqual(self.private.read_bytes(), before)

    def test_japan_month_boundary_and_future_samples_are_excluded(self):
        boundary = datetime(2026, 9, 30, 15, tzinfo=timezone.utc)
        self.observe('10', [('20', 'boundary', 0)], boundary)
        self.observe('10', [('21', 'September', 40)], boundary-timedelta(microseconds=1))
        self.observe('10', [('22', 'future', 40)], self.now+timedelta(minutes=1))
        result = self.get()
        self.assertEqual([user['id'] for user in result['users']], ['20'])
        self.assertEqual(result['month'], '2026-10')
        self.assertEqual(result['observed_at'], boundary.isoformat(timespec='microseconds'))

    def test_monthly_latest_positive_members_are_not_automatically_fans(self):
        profiledb.save_dj_ranking(self.db, self.owner,
            [{'user': {'id': '20', 'name': 'stale'}, 'temperature': 50}], '2026-10', True,
            (self.now-timedelta(hours=3)).isoformat())
        profiledb.save_dj_ranking(self.db, self.owner,
            [{'user': {'id': '21', 'name': 'positive'}, 'temperature': 25},
             {'user': {'id': '22', 'name': 'zero'}, 'temperature': 0},
             {'user': {'id': '23', 'name': 'unknown'}, 'temperature': None}],
            '2026-10', False, (self.now-timedelta(hours=1)).isoformat())
        profiledb.save_dj_ranking(self.db, {'id': '30', 'name': 'other'},
            [{'user': {'id': '24', 'name': 'unrelated'}, 'temperature': 90}],
            '2026-10', True, (self.now-timedelta(minutes=30)).isoformat())
        profiledb.save_dj_ranking(self.db, self.owner,
            [{'user': {'id': '25', 'name': 'future'}, 'temperature': 90}],
            '2026-10', True, (self.now+timedelta(minutes=1)).isoformat())
        result = self.get(source='monthly')
        self.assertEqual(result['state'], 'partial')
        self.assertFalse(result['complete'])
        self.assertEqual([user['id'] for user in result['users']], ['21'])
        self.assertFalse(result['users'][0]['registered_fan'])

    def test_uncollected_complete_empty_and_partial_empty_are_distinct(self):
        self.assertEqual(self.get()['state'], 'not_collected')
        self.assertEqual(self.get(source='monthly')['state'], 'not_collected')
        self.observe('10', [])
        self.assertEqual(self.get()['state'], 'empty')
        profiledb.save_dj_ranking(self.db, self.owner, [], '2026-10', False,
                                (self.now-timedelta(minutes=5)).isoformat())
        result = self.get(source='monthly')
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['total'], 0)

    def test_private_registration_does_not_inherit_public_or_other_owner(self):
        self.observe('10', [('20', 'one', 10), ('21', 'two', 20), ('22', 'three', 30)])
        fans.import_followers(self.db, {'owner': self.owner, 'followers': ['20'], 'complete': True})
        fans.import_followers(self.private, {'owner': {'id': '99', 'name': 'other'}, 'followers': ['21']})
        fans.import_followers(self.private, {'owner': self.owner, 'followers': ['22']})
        result = self.get()
        self.assertEqual({user['id']: user['registered_fan'] for user in result['users']},
                         {'20': False, '21': False, '22': True})

    def test_sort_search_and_pagination_count_whole_source(self):
        self.observe('10', [(str(i), f'Name {160-i:02d}', 0) for i in range(100, 155)])
        first, last = self.get(sort='name'), self.get(sort='name', offset=50)
        self.assertEqual(len(first['users']), 50)
        self.assertTrue(first['has_more'])
        self.assertEqual(last['total'], 55)
        self.assertEqual(len(last['users']), 5)
        self.assertFalse(last['has_more'])
        names = [user['name'] for user in first['users']+last['users']]
        self.assertEqual(names, sorted(names))
        far = self.get(offset=100)
        self.assertEqual(far['total'], 55)
        self.assertEqual(far['users'], [])
        search = self.get(q='Name 06')
        self.assertEqual(search['total'], 1)
        self.assertEqual(self.get(q='no match')['state'], 'ready')
        self.assertEqual(self.get(q='%')['users'], [])

    def test_activity_filters_and_existing_sorts_run_before_pagination(self):
        self.observe('10', [(str(i), f'Name {i}', 0) for i in range(100, 160)])
        self.observe('30', [('159', 'Name 159', 0)], self.now-timedelta(minutes=2))
        self.observe('31', [('158', 'Name 158', None)], self.now-timedelta(minutes=20))
        current = self.get(activity='current', sort='oldest', limit=1)
        self.assertEqual([user['id'] for user in current['users']], ['159'])
        self.assertEqual(current['total'], 1)
        self.assertEqual(current['source_count'], 60)
        self.assertEqual(current['activity_counts'], {'current': 1, 'recent': 0, 'registered': 0})
        recent = self.get(activity='recent', sort='activity', limit=1)
        self.assertEqual(recent['total'], 2)
        self.assertEqual(recent['users'][0]['id'], '159')
        self.assertTrue(recent['has_more'])
        self.assertEqual(self.get(activity='recent', offset=1, limit=1)['users'][0]['id'], '158')
        older = self.get(activity='registered', sort='name_desc', limit=1)
        self.assertEqual(older['total'], 58)
        self.assertEqual(older['users'][0]['id'], '157')
        self.assertEqual(self.get(activity='current', q='Name 158')['total'], 0)
        self.assertEqual(self.get(activity='current', q='Name 158')['state'], 'ready')

    def test_invalid_scope_source_and_pagination_are_rejected(self):
        for bad in (True, 0, 'zero', '1;DROP', '１'):
            with self.subTest(owner=bad), self.assertRaises(ValueError):
                fan_cohorts.candidates(self.db, bad)
        for kwargs in ({'source': 'all'}, {'offset': -1}, {'offset': True}, {'limit': 51},
                       {'q': 'x'*201}, {'q': []}, {'sort': 'DROP'}, {'activity': 'other'},
                       {'now': datetime(2026, 10, 11)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                fan_cohorts.candidates(self.db, '10', **kwargs)


if __name__ == '__main__':
    unittest.main()
