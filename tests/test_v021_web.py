"""v0.2.1 HTTP contracts across private settings and shared observations.

The tests use saved public profiles and never contact Spoon. Authentication,
CSRF, private database selection and binding checks run through real HTTP.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from spoondev import accounts, db, gifts, profiledb, web


class V021WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.database = root / 'observations.sqlite3'
        self.auth = root / 'accounts.sqlite3'
        db.initialize(self.database)
        self.alice_id = accounts.create(self.auth, 'alice', 'alice-password-123', must_change=False)
        self.bob_id = accounts.create(self.auth, 'bob', 'bob-password-123', must_change=False)
        self.admin_id = accounts.create(self.auth, 'kitomoya', 'admin-password-123', must_change=False)
        profiledb.initialize(self.database)
        profiledb.cache_users(self.database, [
            {'id': '123', 'name': 'Alice DJ', 'tag': 'alice-dj'},
            {'id': '124', 'name': 'Other DJ', 'tag': 'other-dj'},
            {'id': '125', 'name': 'Third DJ', 'tag': 'third-dj'},
        ])
        now = datetime.now(timezone.utc)
        self.observed_at = (now - timedelta(minutes=5)).isoformat()
        db.save_snapshot(self.database, {
            'room_id': 'own-room', 'broadcaster': {'id': '123', 'name': 'Alice DJ', 'tag': 'alice-dj'},
            'listeners': [{'id': '456', 'name': 'Listener', 'tag': 'listener', 'favorite_temperature': 42}],
            'complete': True, 'observed_at': self.observed_at,
        })
        gifts._save(self.database, '123', {'456': ('456', 'Listener', 'listener', 75)},
                    True, (now - timedelta(minutes=4)).isoformat(timespec='microseconds'))
        self.server = web.make_server(self.database, port=0, auth=True, auth_database=self.auth)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.alice, self.alice_csrf = self.login('alice', 'alice-password-123')
        self.bob, self.bob_csrf = self.login('bob', 'bob-password-123')
        self.admin, self.admin_csrf = self.login('kitomoya', 'admin-password-123')

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, body=None, cookie='', csrf=''):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        headers = {'Origin': self.origin, 'Content-Type': 'application/json',
                   'Cookie': cookie, 'X-CSRF-Token': csrf}
        connection.request('GET' if body is None else 'POST', path,
                           None if body is None else json.dumps(body), headers)
        response = connection.getresponse()
        status, response_headers, raw = response.status, dict(response.getheaders()), response.read()
        connection.close()
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw.decode()
        return status, response_headers, value

    def login(self, username, password):
        status, headers, _ = self.request('/api/login', {'username': username, 'password': password})
        self.assertEqual(status, 200)
        cookie = headers['Set-Cookie'].split(';')[0]
        actor = accounts.session(self.auth, cookie.split('=', 1)[1])
        return cookie, actor['csrf']

    def bind(self, uid='123', cookie=None, csrf=None):
        return self.request('/api/account/settings', {'spoon_id': uid, 'confirmed': True},
                            cookie or self.alice, csrf or self.alice_csrf)

    def test_preview_and_confirmation_do_not_preemptively_save_or_enqueue(self):
        from spoondev import worker
        with patch.object(worker, 'enqueue', wraps=worker.enqueue) as enqueue:
            preview = self.request('/api/account/profile-preview?spoon_id=123', cookie=self.alice)
            self.assertEqual(preview[0], 200)
            self.assertEqual(preview[2], {'id': '123', 'name': 'Alice DJ', 'tag': 'alice-dj'})
            self.assertIsNone(self.request('/api/account/settings', cookie=self.alice)[2]['spoon_profile'])
            self.assertFalse(self.request('/api/account/settings', cookie=self.alice)[2]['binding_locked'])
            self.assertEqual(enqueue.call_count, 0)
            for confirmation in (None, False, 'true', 1):
                body = {'spoon_id': '123'}
                if confirmation is not None:
                    body['confirmed'] = confirmation
                self.assertEqual(self.request('/api/account/settings', body, self.alice, self.alice_csrf)[0], 400)
            result = self.bind()
            self.assertEqual(result[0], 200)
            self.assertTrue(result[2]['binding_locked'])
            self.assertIsNotNone(result[2]['binding_confirmed_at'])
            self.assertEqual({(call.args[1], call.args[2]) for call in enqueue.call_args_list},
                             {('monthly', '123'), ('gifts', '123')})
        self.assertIsNone(self.request('/api/account/settings', cookie=self.bob)[2]['spoon_profile'])
        self.assertEqual(self.request('/api/dashboard?dj_id=124', cookie=self.alice)[2]['profile']['id'], '123')
        self.assertIsNone(self.request('/api/dashboard?dj_id=123', cookie=self.bob)[2]['profile'])

    def test_bound_user_cannot_change_unlink_or_forge_administrator_fields(self):
        self.assertEqual(self.bind()[0], 200)
        forged = {'allow_change': True, 'role': 'admin', 'username': 'kitomoya',
                  'account_id': self.admin_id, 'actor': {'role': 'admin'}, 'confirmed': True}
        for target in ('124', None, '123'):
            with self.subTest(target=target):
                result = self.request('/api/account/settings', {**forged, 'spoon_id': target},
                                      self.alice, self.alice_csrf)
                self.assertEqual(result[0], 403)
        settings = self.request('/api/account/settings', cookie=self.alice)[2]
        self.assertEqual(settings['spoon_profile']['id'], '123')
        self.assertFalse(settings['can_change'])
        private = accounts.private(self.auth, self.alice_id)
        self.assertEqual(len(accounts.binding_audit(private)), 1)

    def test_concurrent_initial_registration_has_one_winner(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(self.bind, ('123', '124')))
        statuses = [result[0] for result in outcomes]
        self.assertEqual(statuses.count(200), 1, outcomes)
        self.assertIn(next(status for status in statuses if status != 200), (403, 409))
        settings = self.request('/api/account/settings', cookie=self.alice)[2]
        winner = next(result[2]['spoon_profile']['id'] for result in outcomes if result[0] == 200)
        self.assertEqual(settings['spoon_profile']['id'], winner)
        self.assertEqual(settings['binding_revision'], 1)

    def test_admin_override_is_authorized_audited_and_reschedules_sources(self):
        from spoondev import worker
        self.assertEqual(self.bind()[0], 200)
        payload = {'id': self.alice_id, 'spoon_id': '124', 'confirmed': True}
        self.assertEqual(self.request('/api/admin/users/spoon-profile', payload, self.bob, self.bob_csrf)[0], 403)
        self.assertEqual(self.request('/api/admin/users/spoon-profile', payload, self.admin)[0], 403)
        with patch.object(worker, 'enqueue', wraps=worker.enqueue) as enqueue:
            changed = self.request('/api/admin/users/spoon-profile', payload, self.admin, self.admin_csrf)
            self.assertEqual(changed[0], 200)
            self.assertEqual(changed[2]['spoon_profile']['id'], '124')
            self.assertEqual({(call.args[1], call.args[2]) for call in enqueue.call_args_list},
                             {('monthly', '124'), ('gifts', '124')})
        self.assertIsNone(self.request('/api/account/settings', cookie=self.admin)[2]['spoon_profile'])
        private = accounts.private(self.auth, self.alice_id)
        audit = accounts.binding_audit(private)[0]
        self.assertEqual((audit['action'], audit['before_id'], audit['after_id'], audit['actor_username']),
                         ('admin_replace', '123', '124', 'kitomoya'))
        self.assertEqual(self.request('/api/account/settings', {'spoon_id': '125', 'confirmed': True},
                                      self.alice, self.alice_csrf)[0], 403)
        clear = self.request('/api/admin/users/spoon-profile', {**payload, 'spoon_id': None},
                             self.admin, self.admin_csrf)
        self.assertEqual(clear[0], 200)
        self.assertIsNone(clear[2]['spoon_profile'])
        self.assertEqual(self.bind('125')[0], 200)

    def test_fan_scope_and_clear_preserve_binding_and_other_accounts(self):
        payload = {'owner': {'id': '123', 'name': 'Alice DJ'},
                   'followers': [{'id': '456', 'name': 'Listener'}], 'complete': True}
        self.assertEqual(self.request('/api/fans/import', payload, self.alice, self.alice_csrf)[0], 403)
        self.assertEqual(self.bind()[0], 200)
        self.assertEqual(self.bind(cookie=self.bob, csrf=self.bob_csrf)[0], 200)
        for cookie, csrf in ((self.alice, self.alice_csrf), (self.bob, self.bob_csrf)):
            self.assertEqual(self.request('/api/fans/import', payload, cookie, csrf)[0], 200)
            other = {**payload, 'owner': {'id': '124', 'name': 'Other DJ'}}
            self.assertEqual(self.request('/api/fans/import', other, cookie, csrf)[0], 403)
            self.assertEqual(self.request('/api/fans?owner_id=124', cookie=cookie)[0], 403)
        self.assertEqual(self.request('/api/favorites', {'users': [{'id': '456', 'name': 'Listener'}],
                                                       'revision': 0}, self.alice, self.alice_csrf)[0], 200)
        cleared = self.request('/api/fans/clear', {'owner_id': '123', 'confirm': True},
                               self.alice, self.alice_csrf)
        self.assertEqual(cleared[0], 200)
        self.assertEqual(cleared[2]['deleted_count'], 1)
        self.assertEqual(self.request('/api/fans?owner_id=123', cookie=self.bob)[2]['total'], 1)
        settings = self.request('/api/account/settings', cookie=self.alice)[2]
        self.assertEqual(settings['spoon_profile']['id'], '123')
        self.assertTrue(settings['binding_locked'])
        self.assertEqual(len(self.request('/api/favorites', cookie=self.alice)[2]['users']), 1)

    def test_selectable_tags_never_grant_administrator_permission(self):
        self.assertEqual(self.request('/api/admin/labels', cookie=self.alice)[0], 403)
        labels = self.request('/api/admin/labels', cookie=self.admin)[2]['labels']
        self.assertTrue({'利用者', '管理者', 'テスト'}.issubset(labels))
        unknown = {'id': self.alice_id, 'label': 'New plan'}
        self.assertEqual(self.request('/api/admin/users/label', unknown, self.admin, self.admin_csrf)[0], 400)
        self.assertEqual(self.request('/api/admin/labels', {'label': 'New plan'}, self.admin, self.admin_csrf)[0], 200)
        self.assertEqual(self.request('/api/admin/users/label', unknown, self.admin, self.admin_csrf)[0], 200)
        self.assertEqual(self.request('/api/admin/users/label', {'id': self.alice_id, 'label': '管理者'},
                                      self.admin, self.admin_csrf)[0], 200)
        self.assertEqual(self.request('/api/admin/users', cookie=self.alice)[0], 403)
        record = next(user for user in self.request('/api/admin/users', cookie=self.admin)[2]
                      if user['id'] == self.alice_id)
        self.assertEqual((record['label'], record['role']), ('管理者', 'user'))
        self.assertEqual(self.request('/api/stats', cookie=self.alice)[0], 403)
        create = {'username': 'label-only', 'password': 'initial-password-123',
                  'label': 'Unregistered', 'role': 'admin'}
        self.assertEqual(self.request('/api/admin/users/create', create, self.admin, self.admin_csrf)[0], 400)
        created = self.request('/api/admin/users/create', {**create, 'label': '管理者'},
                               self.admin, self.admin_csrf)
        self.assertEqual(created[0], 200)
        user = next(user for user in self.request('/api/admin/users', cookie=self.admin)[2]
                    if user['id'] == created[2]['id'])
        self.assertEqual((user['role'], user['label'], user['must_change']), ('user', '管理者', 1))

    def test_listener_insights_are_shared_across_every_display_surface(self):
        self.assertEqual(self.bind()[0], 200)
        fans = {'owner': {'id': '123', 'name': 'Alice DJ'},
                'followers': [{'id': '456', 'name': 'Listener'}], 'complete': True}
        self.assertEqual(self.request('/api/fans/import', fans, self.alice, self.alice_csrf)[0], 200)
        fixed_now = datetime.now(timezone.utc)
        from spoondev import insights
        implementation = insights.listener_insights
        def frozen(database, ids, **kwargs):
            kwargs['now'] = fixed_now
            return implementation(database, ids, **kwargs)
        with patch.object(insights, 'listener_insights', side_effect=frozen):
            dashboard = self.request('/api/dashboard', cookie=self.alice)
            detail = self.request('/api/users/456', cookie=self.alice)
            search = self.request('/api/users?q=listener', cookie=self.alice)
            favorites = self.request('/api/favorites/activity?ids=456', cookie=self.alice)
            fan_list = self.request('/api/fans?owner_id=123', cookie=self.alice)
        for response in (dashboard, detail, search, favorites, fan_list):
            self.assertEqual(response[0], 200, response)
        own = dashboard[2]['live']['listeners'][0]
        self.assertEqual(own['total_spoon'], 75)
        expected = own['insights']
        self.assertIsInstance(expected, dict)
        self.assertTrue(expected)
        for listener in (detail[2], search[2]['users'][0], favorites[2]['users'][0], fan_list[2]['fans'][0]):
            self.assertEqual(listener['id'], '456')
            self.assertEqual(listener['insights'], expected)


if __name__ == '__main__':
    unittest.main()
