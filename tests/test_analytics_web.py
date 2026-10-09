"""Authenticated analytics HTTP boundaries and account-specific scope."""
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from spoondev import accounts, db, fans, profiledb, web


class AnalyticsWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.database, self.auth = root / 'public.sqlite3', root / 'accounts.sqlite3'
        db.initialize(self.database)
        profiledb.initialize(self.database)
        self.private = {}
        for name, dj in (('alice', '123'), ('bob', '124'), ('kitomoya', '125'), ('unbound', None)):
            uid = accounts.create(self.auth, name, name + '-password-123', must_change=False)
            private = accounts.private(self.auth, uid)
            self.private[name] = private
            if dj:
                profile = {'id': dj, 'name': name + ' DJ', 'tag': name + '-dj'}
                profiledb.cache_users(self.database, [profile])
                accounts.account_settings(private, {'spoon_profile': profile}, confirmed=True)
        accounts.create(self.auth, 'firstuser', 'initial-password-123')
        stamp = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        for dj, name, listeners in (
            ('123', 'alice DJ', [{'id': '456', 'name': 'Alice favorite'}, {'id': '789', 'name': 'Alice fan'}]),
            ('124', 'bob DJ', [{'id': '999', 'name': 'Bob-only listener'}]),
            ('125', 'kitomoya DJ', [{'id': '777', 'name': 'Admin-only listener'}]),
        ):
            db.save_snapshot(self.database, {'room_id': 'room-' + dj,
                'broadcaster': {'id': dj, 'name': name}, 'listeners': listeners,
                'complete': True, 'observed_at': stamp})
        accounts.favorites(self.private['alice'], {'users': [{'id': '456', 'name': 'Alice favorite'}], 'revision': 0})
        fans.import_followers(self.private['alice'], {'owner': {'id': '123', 'name': 'alice DJ'},
            'followers': [{'id': '789', 'name': 'Alice fan'}], 'complete': True})
        # Private favorites must not influence the registered-fan cohort.
        accounts.favorites(self.private['bob'], {'users': [{'id': '789', 'name': 'Bob favorite'}], 'revision': 0})
        self.server = web.make_server(self.database, port=0, auth=True, auth_database=self.auth)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, cookie='', body=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        headers = {'Origin': self.origin, 'Content-Type': 'application/json', 'Cookie': cookie}
        conn.request('GET' if body is None else 'POST', path,
                     None if body is None else json.dumps(body), headers)
        result = conn.getresponse()
        status, headers, raw = result.status, dict(result.getheaders()), result.read()
        conn.close()
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw.decode()
        return status, headers, value

    def login(self, username):
        password = 'initial-password-123' if username == 'firstuser' else username + '-password-123'
        status, headers, _ = self.request('/api/login', body={'username': username, 'password': password})
        self.assertEqual(status, 200)
        return headers['Set-Cookie'].split(';', 1)[0]

    def test_analytics_requires_login_and_initial_password_change(self):
        self.assertEqual(self.request('/api/analytics')[0], 401)
        cookie = self.login('firstuser')
        result = self.request('/api/analytics?days=7', cookie)
        self.assertEqual(result[0], 403)
        self.assertTrue(result[2]['password_change_required'])
        self.assertEqual(self.request('/password', cookie)[0], 200)
        unbound = self.request('/api/analytics', self.login('unbound'))
        self.assertEqual(unbound[0], 200)
        self.assertEqual(unbound[2]['state'], 'needs_profile')
        self.assertIsNone(unbound[2]['profile'])
        self.assertIsNone(unbound[2]['totals']['all'])

    def test_supported_windows_and_invalid_queries_are_structured(self):
        cookie = self.login('alice')
        for path in ('/api/analytics', '/api/analytics?days=7', '/api/analytics?days=28'):
            with self.subTest(path=path):
                result = self.request(path, cookie)
                self.assertEqual(result[0], 200, result)
                self.assertIsInstance(result[2], dict)
                days = 28 if path.endswith('28') else 7
                self.assertEqual(result[2]['window']['days'], days)
                self.assertEqual(result[2]['window']['timezone'], 'Asia/Tokyo')
                self.assertEqual(len(result[2]['daily']), days)
                self.assertTrue(all(len(day['hours']) == 24 for day in result[2]['daily']))
        for query in ('days=', 'days=0', 'days=1', 'days=29', 'days=no', 'days=7&days=28',
                      'days=7&dj_id=124', 'dj_id=124', 'account_id=bob', 'owner_id=124',
                      'username=kitomoya', 'scope=global'):
            with self.subTest(query=query):
                result = self.request('/api/analytics?' + query, cookie)
                self.assertEqual(result[0], 400)
                self.assertIn('error', result[2])
        self.assertEqual(self.request('/api/analytics?' + 'q' * 4100, cookie)[0], 414)

    def test_each_account_including_administrator_uses_its_own_binding(self):
        for name, expected, count, fan_count in (
            ('alice', '123', 2, 1), ('bob', '124', 1, 0), ('kitomoya', '125', 1, 0),
        ):
            with self.subTest(account=name):
                result = self.request('/api/analytics?days=7', self.login(name))
                self.assertEqual(result[0], 200, result)
                self.assertEqual(result[2]['profile']['id'], expected)
                self.assertEqual(result[2]['totals']['all'], count)
                self.assertEqual(result[2]['cohorts']['registered_fan_count'], fan_count)
                for key in ('user_count', 'snapshot_count', 'monthly_indexed_djs', 'global_counts', 'accounts'):
                    self.assertNotIn(key, result[2])

    def test_static_assets_keep_authentication_and_correct_content_types(self):
        for path, mime, marker in (('/theme.css', 'text/css', '--bg'),
                                   ('/analytics.js', 'javascript', 'analytics')):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 401)
                forced = self.request(path, self.login('firstuser'))
                self.assertEqual(forced[0], 303)
                self.assertEqual(forced[1]['Location'], '/password')
                status, headers, value = self.request(path, self.login('alice'))
                self.assertEqual(status, 200)
                self.assertIn(mime, headers['Content-Type'])
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
                self.assertIn(marker, value)
        self.assertEqual(self.request('/../theme.css', self.login('alice'))[0], 404)


if __name__ == '__main__':
    unittest.main()
