from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from spoondev import announcements


class AnnouncementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "accounts.sqlite3"
        self.actor = {"id": "a" * 32, "role": "admin", "username": "fixtureadmin"}
        announcements.initialize(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, **updates):
        return {"date": "2026-10-10", "importance": "info", "content": "収集処理を更新しました。", **updates}

    def save(self, **updates):
        return announcements.save(self.database, self.payload(**updates), self.actor)["announcement"]

    def test_scheduled_visibility_uses_japan_midnight_and_no_editor_leaks(self):
        old = self.save(date="2026-10-09", content="previous day")
        today = self.save(content="today")
        future = self.save(date="2026-10-11", content="scheduled")
        before = datetime(2026, 10, 9, 14, 59, 59, tzinfo=timezone.utc)
        midnight = datetime(2026, 10, 9, 15, tzinfo=timezone.utc)
        self.assertEqual([r["id"] for r in announcements.list_for_user(self.database, before)["announcements"]], [old["id"]])
        public = announcements.list_for_user(self.database, midnight)["announcements"]
        self.assertEqual([r["id"] for r in public], [today["id"], old["id"]])
        self.assertEqual(set(public[0]), {"id", "date", "importance", "content", "updated_at"})
        admin = announcements.list_admin(self.database)["announcements"]
        self.assertEqual(admin[0]["id"], future["id"])
        self.assertEqual(admin[0]["created_by"], self.actor["id"])

    def test_importance_order_and_plain_text(self):
        notice = self.save(content=" <script>alert('x')</script>\nMessage ")
        urgent = self.save(date="2026-10-08", importance="urgent")
        important = self.save(importance="important")
        public = announcements.list_for_user(self.database, datetime(2026, 10, 10, tzinfo=timezone.utc))["announcements"]
        self.assertEqual([r["id"] for r in public], [urgent["id"], important["id"], notice["id"]])
        self.assertEqual(public[-1]["content"], "<script>alert('x')</script>\nMessage")

    def test_edit_retains_creation_metadata_and_schedule_and_delete(self):
        old = self.save(date="2027-01-01")
        editor = "b" * 32
        edited = announcements.save(self.database,
            {"id": old["id"], "importance": "important", "content": "Updated"}, editor)["announcement"]
        self.assertEqual(edited["id"], old["id"])
        self.assertEqual(edited["date"], old["date"])
        self.assertEqual(edited["created_at"], old["created_at"])
        self.assertEqual(edited["created_by"], old["created_by"])
        self.assertEqual(edited["updated_by"], editor)
        self.assertGreaterEqual(edited["updated_at"], old["updated_at"])
        self.assertEqual(announcements.list_for_user(self.database, datetime(2026, 10, 10, tzinfo=timezone.utc))["announcements"], [])
        self.assertEqual(announcements.delete(self.database, old["id"]), {"deleted": old["id"]})
        self.assertEqual(announcements.list_admin(self.database), {"announcements": []})
        with self.assertRaises(LookupError):
            announcements.delete(self.database, old["id"])
        with self.assertRaises(LookupError):
            announcements.save(self.database, self.payload(id=old["id"]), self.actor)
        replacement = self.save(content="new notice")
        self.assertGreater(replacement["id"], old["id"])
        with self.assertRaises(LookupError):
            announcements.save(self.database, self.payload(id=old["id"]), self.actor)

    def test_initialization_preserves_existing_accounts_and_is_idempotent(self):
        with sqlite3.connect(self.database) as conn:
            conn.execute("CREATE TABLE accounts(id TEXT PRIMARY KEY,password TEXT,must_change INTEGER)")
            conn.execute("INSERT INTO accounts VALUES('existing','untouched',0)")
        saved = self.save()
        announcements.initialize(self.database)
        announcements.initialize(self.database)
        self.assertEqual(announcements.list_admin(self.database)["announcements"], [saved])
        with sqlite3.connect(self.database) as conn:
            self.assertEqual(conn.execute("SELECT * FROM accounts").fetchall(), [("existing", "untouched", 0)])
        self.assertEqual(self.database.stat().st_mode & 0o777, 0o600)

    def test_creation_date_defaults_to_jst_today(self):
        fixed = datetime(2026, 10, 9, 15, tzinfo=timezone.utc)
        with patch.object(announcements, "_now", return_value=fixed):
            saved = announcements.save(self.database, {"importance": "info", "content": "default date"}, self.actor)
        self.assertEqual(saved["announcement"]["date"], "2026-10-10")

    def test_invalid_payloads_and_actor_do_not_change_notice(self):
        valid = self.save()
        invalid = [None, [], {}, self.payload(id=None), self.payload(id=True),
                   self.payload(id=0), self.payload(id="1"), self.payload(id=-1),
                   self.payload(id=9223372036854775808), self.payload(date="20261010"),
                   self.payload(date="2026-W41-6"), self.payload(date="2026-02-29"),
                   self.payload(date="2026-10-10T00:00:00Z"), self.payload(date=True),
                   self.payload(date="2026-10-10 "), self.payload(importance="normal"),
                   self.payload(importance=True), self.payload(content="\n \t"),
                   self.payload(content=123), self.payload(content="a" * 5001),
                   self.payload(account_id="client-supplied"), self.payload(created_by="forged")]
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    announcements.save(self.database, payload, self.actor)
        for actor in [None, True, "not-an-account", {}, {"id": "Z" * 32}]:
            with self.subTest(actor=actor):
                with self.assertRaises(ValueError):
                    announcements.save(self.database, self.payload(), actor)
        self.assertEqual(announcements.list_admin(self.database)["announcements"], [valid])
        for value in [None, True, False, 0, -1, "1", 1.0, 9223372036854775808]:
            with self.subTest(delete_id=value):
                with self.assertRaises(ValueError):
                    announcements.delete(self.database, value)

    def test_date_validation_accepts_leap_day_but_rejects_naive_now(self):
        saved = self.save(date="2028-02-29", content="a" * 5000)
        self.assertEqual(len(saved["content"]), 5000)
        for value in [datetime(2026, 10, 10), "2026-10-10", True]:
            with self.subTest(now=value):
                with self.assertRaises(ValueError):
                    announcements.list_for_user(self.database, value)


if __name__ == "__main__":
    unittest.main()
