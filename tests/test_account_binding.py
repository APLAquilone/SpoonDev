"""Private first-registration policy and atomic profile changes."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from spoondev import accounts, fans


class AccountBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.auth = Path(self.tmp.name) / 'accounts.sqlite3'
        self.uid = accounts.create(self.auth, 'listener', 'example-password-123', must_change=False)
        self.other_id = accounts.create(self.auth, 'another', 'other-password-123', must_change=False)
        self.database = accounts.private(self.auth, self.uid)
        self.other = accounts.private(self.auth, self.other_id)
        self.actor = {'id': self.uid, 'username': 'listener', 'role': 'user'}
        self.admin = {'id': 'c'*32, 'username': 'operator', 'role': 'admin'}
        self.first = {'spoon_profile': {'id': '000123', 'name': 'My DJ', 'tag': 'my-dj'}}
        self.second = {'spoon_profile': {'id': '456', 'name': 'Another DJ'}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_confirmation_cancellation_and_first_registration_lock(self):
        before = accounts.account_settings(self.database)
        self.assertFalse(before['binding_locked'])
        self.assertIsNone(before['spoon_profile'])
        for confirmed in (False, None, 'true', 1):
            with self.assertRaises(accounts.BindingConfirmationRequired):
                accounts.account_settings(self.database, self.first, confirmed=confirmed, actor=self.actor)
            self.assertEqual(accounts.account_settings(self.database), before)
        result = accounts.account_settings(self.database, self.first, confirmed=True, actor=self.actor)
        self.assertEqual(result['spoon_profile']['id'], '123')
        self.assertTrue(result['binding_locked'])
        self.assertTrue(result['binding_confirmed_at'])
        self.assertEqual(result['binding_revision'], 1)
        self.assertIsNone(accounts.account_settings(self.other)['spoon_profile'])
        for payload in (self.second, self.first, {'spoon_profile': None}):
            with self.assertRaises(accounts.BindingLockedError):
                accounts.account_settings(self.database, payload, confirmed=True, actor=self.actor)
            self.assertEqual(accounts.account_settings(self.database), result)
        audit = accounts.binding_audit(self.database)
        self.assertEqual(len(audit), 1)
        self.assertEqual((audit[0]['action'], audit[0]['target_account_id'], audit[0]['after_id']),
                         ('initial_bind', self.uid, '123'))

    def test_admin_override_is_explicit_and_audited_without_other_data_changes(self):
        accounts.account_settings(self.database, self.first, confirmed=True, actor=self.actor)
        accounts.favorites(self.database, {'users': [{'id': '777', 'name': 'Favorite'}], 'revision': 0})
        fans.import_followers(self.database, {'owner': {'id': '123', 'name': 'My DJ'},
             'followers': [{'id': '888', 'name': 'Fan'}], 'complete': True})
        for actor in (None, self.actor, {'role': 'admin'}, {**self.admin, 'id': 'unsafe'}):
            with self.assertRaises(accounts.BindingLockedError):
                accounts.account_settings(self.database, self.second, allow_change=True,
                                          confirmed=True, actor=actor)
        with self.assertRaises(accounts.BindingConfirmationRequired):
            accounts.account_settings(self.database, self.second, allow_change=True, actor=self.admin)
        replaced = accounts.account_settings(self.database, self.second, allow_change=True,
                                              confirmed=True, actor=self.admin)
        self.assertEqual(replaced['spoon_profile']['id'], '456')
        self.assertTrue(replaced['binding_locked'])
        cleared = accounts.account_settings(self.database, {'spoon_profile': None},
                          allow_change=True, confirmed=True, actor=self.admin)
        self.assertIsNone(cleared['spoon_profile'])
        self.assertFalse(cleared['binding_locked'])
        self.assertEqual(cleared['binding_revision'], 3)
        audits = accounts.binding_audit(self.database)
        self.assertEqual([row['action'] for row in audits], ['admin_clear', 'admin_replace', 'initial_bind'])
        self.assertEqual((audits[0]['before_id'], audits[0]['after_id']), ('456', None))
        self.assertEqual((audits[1]['actor_id'], audits[1]['actor_username']), ('c'*32, 'operator'))
        self.assertEqual(len(accounts.favorites(self.database)['users']), 1)
        with sqlite3.connect(self.database) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM registered_fans').fetchone()[0], 1)
        self.assertIsNotNone(accounts.login(self.auth, 'listener', 'example-password-123'))
        self.assertIsNone(accounts.account_settings(self.other)['spoon_profile'])

    def test_request_fields_cannot_grant_administrator_override(self):
        accounts.account_settings(self.database, self.first, confirmed=True)
        forged = {**self.second, 'allow_change': True, 'role': 'admin', 'actor': self.admin}
        with self.assertRaises(accounts.BindingLockedError):
            accounts.account_settings(self.database, forged, confirmed=True, actor=self.actor)
        self.assertEqual(accounts.account_settings(self.database)['spoon_profile']['id'], '123')

    def test_atomic_first_registration_has_one_winner_and_one_conflict(self):
        rendezvous = threading.Barrier(2)
        original = sqlite3.connect

        class RacingConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                cursor = super().execute(sql, *args, **kwargs)
                if sql.startswith('SELECT spoon_id,spoon_name') and not self.in_transaction:
                    rendezvous.wait(timeout=5)
                return cursor

        def connect(*args, **kwargs):
            return original(*args, factory=RacingConnection, **kwargs)

        def register(payload):
            try:
                return accounts.account_settings(self.database, payload, confirmed=True, actor=self.actor)
            except accounts.BindingConflict as exc:
                return exc

        with patch('spoondev.accounts.sqlite3.connect', side_effect=connect):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(register, (self.first, self.second)))
        self.assertEqual(sum(isinstance(r, accounts.BindingConflict) for r in results), 1)
        winner = next(r for r in results if isinstance(r, dict))
        self.assertEqual(accounts.account_settings(self.database), winner)
        self.assertEqual(len(accounts.binding_audit(self.database)), 1)

    def test_existing_binding_migrates_once_and_private_data_survives(self):
        with sqlite3.connect(self.database) as c:
            c.execute('DROP TABLE account_settings')
            c.execute('''CREATE TABLE account_settings(singleton INTEGER PRIMARY KEY,
                         spoon_id TEXT,spoon_name TEXT,spoon_tag TEXT,updated_at TEXT)''')
            c.execute('INSERT INTO account_settings VALUES(1,?,?,?,?)',
                      ('999', 'Existing DJ', 'existing', '2026-10-09T00:00:00.000000+00:00'))
        accounts.favorites(self.database, {'users': [{'id': '777'}], 'revision': 0})
        for _ in range(2):
            accounts.private(self.auth, self.uid)
        migrated = accounts.account_settings(self.database)
        self.assertEqual(migrated['spoon_profile']['id'], '999')
        self.assertTrue(migrated['binding_locked'])
        self.assertIsNone(migrated['binding_confirmed_at'])
        self.assertEqual(migrated['binding_revision'], 0)
        self.assertEqual(len(accounts.favorites(self.database)['users']), 1)
        self.assertEqual(accounts.binding_audit(self.database), [])
        with self.assertRaises(accounts.BindingLockedError):
            accounts.account_settings(self.database, self.first, confirmed=True)
        # Two initial requests migrating the same private file do not race ALTER.
        with sqlite3.connect(self.other) as c:
            c.execute('DROP TABLE account_settings')
            c.execute('''CREATE TABLE account_settings(singleton INTEGER PRIMARY KEY,
                         spoon_id TEXT,spoon_name TEXT,spoon_tag TEXT,updated_at TEXT)''')
            c.execute('INSERT INTO account_settings(singleton) VALUES(1)')
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: accounts.private(self.auth, self.other_id), range(2)))
        self.assertFalse(accounts.account_settings(self.other)['binding_locked'])


if __name__ == '__main__':
    unittest.main()
