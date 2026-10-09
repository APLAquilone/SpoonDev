"""Managed display tags are independent from authentication roles."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest

from spoondev import accounts


class LabelCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.auth = Path(self.tmp.name) / 'accounts.sqlite3'
        accounts.initialize(self.auth)

    def tearDown(self):
        self.tmp.cleanup()

    def test_seed_add_and_selected_labels_do_not_change_role(self):
        self.assertEqual([l['name'] for l in accounts.list_labels(self.auth)], ['利用者', '管理者', 'テスト'])
        uid = accounts.create(self.auth, 'ordinary', 'initial-password-123', label='管理者')
        row = accounts.list_users(self.auth)[0]
        self.assertEqual((row['role'], row['label']), ('user', '管理者'))
        first = accounts.add_label(self.auth, 'プラン1')
        self.assertEqual(accounts.add_label(self.auth, ' プラン1 '), first)
        accounts.set_label(self.auth, uid, 'プラン1')
        self.assertEqual(next(l for l in accounts.list_labels(self.auth) if l['name']=='プラン1')['user_count'], 1)
        accounts.set_role(self.auth, 'ordinary', 'admin')
        accounts.set_label(self.auth, uid, '利用者')
        row = accounts.list_users(self.auth)[0]
        self.assertEqual((row['role'], row['label']), ('admin', '利用者'))
        for action in (lambda: accounts.set_label(self.auth, uid, '未登録'),
                       lambda: accounts.create(self.auth, 'unlisted', 'initial-password-123', label='未登録')):
            with self.assertRaises(ValueError): action()
        self.assertEqual(len(accounts.list_users(self.auth)), 1)
        for invalid in ('', 'x'*65, 'bad\nlabel', None):
            with self.assertRaises(ValueError): accounts.add_label(self.auth, invalid)

    def test_legacy_labels_are_preserved_and_migration_is_repeatable(self):
        old = Path(self.tmp.name) / 'legacy.sqlite3'
        with sqlite3.connect(old) as c:
            c.execute('''CREATE TABLE accounts(id TEXT PRIMARY KEY,username TEXT UNIQUE NOT NULL,
                         salt TEXT NOT NULL,password TEXT NOT NULL,label TEXT NOT NULL,role TEXT NOT NULL)''')
            c.executemany('INSERT INTO accounts VALUES(?,?,?,?,?,?)',
              [('a'*32, 'kitomoya', '00'*16, 'original-hash', '保守', 'user'),
               ('b'*32, 'olduser', '11'*16, 'other-hash', '独自プラン', 'user')])
        accounts.initialize(old)
        original = accounts.list_labels(old)
        accounts.initialize(old)
        self.assertEqual(accounts.list_labels(old), original)
        self.assertTrue({'利用者','管理者','テスト','保守','独自プラン'} <= {l['name'] for l in original})
        with sqlite3.connect(old) as c:
            rows = c.execute('SELECT username,password,label,role FROM accounts ORDER BY username').fetchall()
        self.assertEqual(rows, [('kitomoya','original-hash','保守','admin'), ('olduser','other-hash','独自プラン','user')])
        accounts.set_label(old, 'b'*32, '保守')
        self.assertEqual(next(l for l in accounts.list_labels(old) if l['name']=='保守')['user_count'], 2)

    def test_parallel_catalog_addition_is_idempotent(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: accounts.add_label(self.auth, '共同タグ'), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(sum(l['name']=='共同タグ' for l in accounts.list_labels(self.auth)), 1)


if __name__ == '__main__':
    unittest.main()
