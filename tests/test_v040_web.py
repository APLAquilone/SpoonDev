"""Exercise v0.4 APIs through real authenticated HTTP and private databases."""
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from zoneinfo import ZoneInfo

from spoondev import accounts, db, fans, profiledb, web


class V040WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / 'observations.sqlite3'
        self.auth = Path(self.temp.name) / 'accounts.sqlite3'
        db.initialize(self.database)
        profiledb.initialize(self.database)
        self.owner = {'id': '123', 'name': 'Own DJ', 'tag': 'own'}
        other = {'id': '124', 'name': 'Other DJ', 'tag': 'other'}
        self.listener = {'id': '456', 'name': 'Duplicate name', 'tag': 'first'}
        self.member = {'id': '457', 'name': 'Duplicate name', 'tag': 'second'}
        profiledb.cache_users(self.database, [self.owner, other, self.listener, self.member])
        self.private = {}
        for username in ('alice', 'bob', 'unbound'):
            uid = accounts.create(self.auth, username, 'fixture-password-123', must_change=False)
            self.private[username] = accounts.private(self.auth, uid)
            if username != 'unbound':
                accounts.account_settings(self.private[username], {'spoon_profile': self.owner}, confirmed=True)
        self.now = datetime.now(timezone.utc)
        self.own_at = (self.now - timedelta(minutes=45)).isoformat()
        self.latest_at = (self.now - timedelta(minutes=2)).isoformat()
        db.save_snapshot(self.database, {'room_id': 'own-room', 'broadcaster': self.owner,
            'listeners': [{**self.listener, 'favorite_temperature': 12}],
            'complete': True, 'observed_at': self.own_at})
        db.save_snapshot(self.database, {'room_id': 'other-room', 'broadcaster': other,
            'listeners': [{**self.listener, 'favorite_temperature': 0}],
            'complete': True, 'observed_at': self.latest_at})
        month = self.now.astimezone(ZoneInfo('Asia/Tokyo')).strftime('%Y-%m')
        profiledb.save_dj_ranking(self.database, self.owner,
            [{'user': self.member, 'temperature': 22}], month, True, observed_at=self.latest_at)
        self.server = web.make_server(self.database, port=0, auth=True, auth_database=self.auth)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = 'http://127.0.0.1:' + str(self.server.server_port)
        self.sessions = {}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, *, username=None, body=None, csrf=True, origin=None, extra_headers=None):
        cookie, token = self.login(username) if username else ('', '')
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=15)
        headers = {'Origin': self.origin if origin is None else origin,
            'Content-Type': 'application/json', 'Cookie': cookie,
            'X-CSRF-Token': token if csrf else ''}
        headers.update(extra_headers or {})
        connection.request('GET' if body is None else 'POST', path,
            None if body is None else json.dumps(body), headers)
        response = connection.getresponse()
        status, result_headers, raw = response.status, dict(response.getheaders()), response.read()
        connection.close()
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            result = raw
        return status, result_headers, result

    def login(self, username):
        if username not in self.sessions:
            status, headers, _ = self.request('/api/login', body={
                'username': username, 'password': 'fixture-password-123'})
            self.assertEqual(status, 200)
            cookie = headers['Set-Cookie'].split(';', 1)[0]
            actor = accounts.session(self.auth, cookie.split('=', 1)[1])
            self.sessions[username] = cookie, actor['csrf']
        return self.sessions[username]

    def register(self, username):
        fans.import_followers(self.private[username], {'owner': self.owner,
            'followers': [self.listener, self.member], 'complete': True})

    def test_candidates_work_before_import_and_do_not_create_followers(self):
        for source, expected in (('observed', '456'), ('monthly', '457')):
            with self.subTest(source=source):
                status, _, result = self.request('/api/fans/candidates?owner_id=123&source=' + source,
                    username='alice')
                self.assertEqual(status, 200, result)
                self.assertEqual([row['id'] for row in result['users']], [expected])
                self.assertFalse(result['users'][0]['registered_fan'])
        self.assertEqual(self.request('/api/fans?owner_id=123', username='alice')[2]['total'], 0)
        self.assertEqual(self.request('/api/fan-owners', username='alice')[2], [])

    def test_candidates_scope_and_flags_are_private(self):
        self.register('alice')
        alice = self.request('/api/fans/candidates?owner_id=123', username='alice')[2]
        bob = self.request('/api/fans/candidates?owner_id=123', username='bob')[2]
        self.assertTrue(alice['users'][0]['registered_fan'])
        self.assertFalse(bob['users'][0]['registered_fan'])
        for username in ('alice', 'unbound'):
            self.assertEqual(self.request('/api/fans/candidates?owner_id=124', username=username)[0], 403)

    def test_remove_one_preserves_other_account_binding_favorites_and_observations(self):
        self.register('alice')
        self.register('bob')
        accounts.favorites(self.private['alice'], {'users': [self.listener], 'revision': 0})
        before_binding = accounts.account_settings(self.private['alice'])
        before_favorites = accounts.favorites(self.private['alice'])
        payload = {'owner_id': '123', 'user_id': '456', 'confirm': True}
        status, _, result = self.request('/api/fans/remove', username='alice', body=payload)
        self.assertEqual(status, 200, result)
        self.assertEqual(result['deleted_count'], 1)
        self.assertEqual(result['fan_count'], 1)
        self.assertFalse(result['complete'])
        self.assertEqual(self.request('/api/fans?owner_id=123', username='bob')[2]['total'], 2)
        self.assertEqual(accounts.account_settings(self.private['alice']), before_binding)
        self.assertEqual(accounts.favorites(self.private['alice']), before_favorites)
        self.assertEqual(self.request('/api/dashboard', username='alice')[2]['live']['listeners'][0]['id'], '456')
        self.assertEqual(self.request('/api/fans/remove', username='alice', body=payload)[2]['deleted_count'], 0)

    def test_remove_requires_confirmation_origin_csrf_and_bound_owner(self):
        self.register('alice')
        payload = {'owner_id': '123', 'user_id': '456', 'confirm': True}
        self.assertEqual(self.request('/api/fans/remove', body=payload)[0], 401)
        self.assertEqual(self.request('/api/fans/remove', username='alice', body=payload, csrf=False)[0], 403)
        self.assertEqual(self.request('/api/fans/remove', username='alice', body=payload,
            origin='https://other.example')[0], 403)
        for invalid, expected in (({**payload, 'confirm': False}, 400),
                ({**payload, 'user_id': '0'}, 400), ({**payload, 'owner_id': '124'}, 403)):
            self.assertEqual(self.request('/api/fans/remove', username='alice', body=invalid)[0], expected)
        self.assertEqual(self.request('/api/fans?owner_id=123', username='alice')[2]['total'], 2)

    def test_dashboard_latest_and_followup_use_all_djs_and_keep_own_baseline(self):
        result = self.request('/api/dashboard', username='alice')[2]
        row = result['live']['listeners'][0]
        self.assertEqual(row['own_observation']['user_id'], '123')
        self.assertEqual(row['latest_observation']['user_id'], '124')
        self.assertEqual(row['latest_observation']['favorite_temperature'], 0)
        status, _, followup = self.request('/api/dashboard/listener-followup?user_id=456', username='alice')
        self.assertEqual(status, 200, followup)
        self.assertEqual(followup['baseline']['user_id'], '123')
        self.assertEqual([row['user_id'] for row in followup['observations']], ['124'])
        unbound = self.request('/api/dashboard/listener-followup?user_id=456', username='unbound')[2]
        self.assertEqual(unbound['state'], 'needs_profile')
        self.assertEqual(unbound['observations'], [])
        never_seen = self.request('/api/dashboard/listener-followup?user_id=457', username='alice')[2]
        self.assertEqual(never_seen['state'], 'not_observed')
        self.assertEqual(never_seen['observations'], [])

    def test_new_reads_require_login_and_reject_parameter_overrides(self):
        for path in ('/api/fans/candidates?owner_id=123', '/api/dashboard/listener-followup?user_id=456'):
            self.assertEqual(self.request(path)[0], 401)
        invalid_paths = ('/api/fans/candidates?owner_id=123&source=other',
            '/api/fans/candidates?owner_id=123&owner_id=124',
            '/api/fans/candidates?owner_id=123&limit=0',
            '/api/fans/candidates?owner_id=123&account=bob',
            '/api/dashboard/listener-followup?user_id=0',
            '/api/dashboard/listener-followup?user_id=456&user_id=457',
            '/api/dashboard/listener-followup?user_id=456&limit=0',
            '/api/dashboard/listener-followup?user_id=456&before_id=0',
            '/api/dashboard/listener-followup?user_id=456&owner_id=124')
        for path in invalid_paths:
            with self.subTest(path=path):
                self.assertEqual(self.request(path, username='alice')[0], 400)

    def test_ocr_static_paths_do_not_escape_and_policy_allows_only_self_worker(self):
        for path in ('/ocr/../accounts.py', '/ocr/%2e%2e%2faccounts.py', '/ocr/unknown.js', '/ocr/LICENSE.md'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, username='alice')[0], 404)
        status, headers, _ = self.request('/theme.css', username='alice')
        self.assertEqual(status, 200)
        policy = headers['Content-Security-Policy']
        self.assertIn("worker-src 'self' blob:", policy)
        self.assertIn("connect-src 'self'", policy)
        self.assertNotIn('https://', policy)

    def test_ocr_assets_revalidate_without_retransmitting_and_still_require_login(self):
        path='/ocr/worker.min.js'
        status, headers, content=self.request(path,username='alice')
        self.assertEqual(status,200)
        self.assertGreater(len(content),1000)
        self.assertEqual(headers['Cache-Control'],'private, no-cache')
        status, repeated_headers, content=self.request(path,username='alice',
            extra_headers={'If-None-Match':headers['ETag']})
        self.assertEqual(status,304)
        self.assertEqual(content,b'')
        self.assertEqual(repeated_headers['ETag'],headers['ETag'])
        self.assertEqual(self.request(path,extra_headers={'If-None-Match':headers['ETag']})[0],401)


if __name__ == '__main__':
    unittest.main()
