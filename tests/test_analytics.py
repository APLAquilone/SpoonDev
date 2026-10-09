from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from spoondev import db, fans, profiledb
from spoondev.analytics import summary


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "observations.sqlite3"
        db.initialize(self.database)
        profiledb.initialize(self.database)
        self.private = self.make_private("alice", "100")
        self.now = datetime(2026, 10, 9, 3, tzinfo=timezone.utc)  # 12:00 JST.

    def make_private(self, name, spoon_id=None, with_fans=True):
        path = self.root / (name + ".sqlite3")
        if with_fans:
            fans.initialize(path)
        profiledb.initialize(path)
        with sqlite3.connect(path) as conn:
            conn.executescript("""CREATE TABLE favorites(id TEXT PRIMARY KEY,name TEXT,tag TEXT);
                CREATE TABLE account_settings(singleton INTEGER PRIMARY KEY,spoon_id TEXT,
                spoon_name TEXT,spoon_tag TEXT,updated_at TEXT);""")
            if spoon_id:
                conn.execute("INSERT INTO account_settings VALUES(1,?,?,?,?)",
                             (spoon_id, "DJ " + spoon_id, "own-tag", "2026-10-01T00:00:00+00:00"))
        return path

    def sighting(self, ids, at=None, complete=True, dj="100"):
        return db.save_snapshot(self.database, {
            "room_id": "room-" + dj, "broadcaster": {"id": dj, "name": "DJ " + dj},
            "listeners": [{"id": uid, "name": "listener " + uid} for uid in ids],
            "observed_at": (at or self.now).isoformat(), "complete": complete,
        })

    def ranking(self, entries, *, dj="100", month="2026-10", complete=True, at=None):
        return profiledb.save_dj_ranking(self.database, {"id": dj, "name": "DJ " + dj},
            [{"user": {"id": uid, "name": "listener " + uid}, "temperature": temperature}
             for uid, temperature in entries], month, complete, (at or self.now).isoformat())

    def import_fans(self, ids, *, private=None, owner="100"):
        fans.import_followers(private or self.private, {
            "owner": {"id": owner, "name": "DJ " + owner},
            "followers": [{"id": uid} for uid in ids], "complete": True,
        })

    def test_repeated_samples_do_not_inflate_daily_hour_and_period_counts(self):
        self.sighting(["200", "201"], self.now-timedelta(minutes=50))
        self.sighting(["200"], self.now-timedelta(minutes=40))
        self.sighting(["200", "202"], self.now-timedelta(days=1, minutes=50))
        self.sighting(["200"], self.now-timedelta(days=1, minutes=40))
        result = summary(self.database, self.private, now=self.now)
        self.assertEqual(result["totals"]["all"], 3)
        self.assertEqual(result["totals"]["snapshot_count"], 4)
        self.assertEqual(result["totals"]["observed_days"], 2)
        self.assertEqual(result["hourly"][11]["all"], 4)
        self.assertEqual(result["hourly"][11]["observed_days"], 2)
        self.assertEqual(result["daily"][-1]["hours"][11]["all"], 2)
        self.assertEqual(result["daily"][-2]["hours"][11]["all"], 2)
        self.assertEqual(result["state"], "ready")

    def test_fan_and_monthly_cohorts_overlap_and_positive_temperature_only(self):
        self.import_fans(["200", "201"])
        self.ranking([("200", 80), ("202", 25), ("203", 0), ("204", None), ("205", -1)])
        self.sighting(["200", "201", "202", "203", "204", "205"])
        result = summary(self.database, self.private, now=self.now)
        self.assertEqual(result["cohorts"], {
            "registered_fan_count": 2, "monthly_confirmed_count": 2,
            "overlap_count": 1, "classification": "current",
        })
        for collection in (result["totals"], result["hourly"][12], result["daily"][-1]["hours"][12]):
            self.assertEqual({key: collection[key] for key in ("all", "fans", "monthly", "overlap", "other")},
                             {"all": 6, "fans": 2, "monthly": 2, "overlap": 1, "other": 3})
        self.assertEqual(result["monthly"]["listener_count"], 5)
        self.assertEqual(result["monthly"]["confirmed_listener_count"], 2)

    def test_current_month_latest_own_ranking_and_current_import_apply_to_history(self):
        self.import_fans(["200"])
        self.sighting(["200", "201"], self.now-timedelta(days=3))
        self.ranking([("200", 90)], month="2026-09", at=self.now-timedelta(days=8))
        self.ranking([("201", 70)], dj="900")
        unavailable = summary(self.database, self.private, now=self.now)
        self.assertIsNone(unavailable["totals"]["monthly"])
        self.ranking([("201", 30)], at=self.now-timedelta(hours=2))
        self.ranking([("200", 40)], complete=False, at=self.now-timedelta(hours=1))
        result = summary(self.database, self.private, now=self.now)
        self.assertEqual(result["monthly"]["state"], "partial")
        self.assertEqual(result["totals"]["monthly"], 1)
        self.assertEqual(result["totals"]["overlap"], 1)
        self.ranking([], complete=True)
        empty = summary(self.database, self.private, now=self.now)
        self.assertEqual(empty["monthly"]["state"], "empty")
        self.assertEqual(empty["totals"]["monthly"], 0)
        self.assertEqual(empty["cohorts"]["monthly_confirmed_count"], 0)

    def test_account_binding_and_private_fans_are_isolated(self):
        bob = self.make_private("bob", "900")
        self.import_fans(["200"])
        self.import_fans(["201"], owner="900")  # A legacy other-DJ import must not count.
        self.import_fans(["999"], private=bob, owner="900")
        with sqlite3.connect(self.private) as conn:
            conn.execute("INSERT INTO favorites VALUES('201','favorite',NULL)")
        self.sighting(["200", "201"])
        self.sighting(["999"], dj="900")
        self.ranking([("200", 10)])
        self.ranking([("999", 99)], dj="900")
        alice_result = summary(self.database, self.private, now=self.now)
        bob_result = summary(self.database, bob, now=self.now)
        self.assertEqual(alice_result["profile"]["id"], "100")
        self.assertEqual(alice_result["totals"]["all"], 2)
        self.assertEqual(alice_result["totals"]["fans"], 1)
        self.assertEqual(alice_result["cohorts"]["registered_fan_count"], 1)
        self.assertEqual(bob_result["profile"]["id"], "900")
        self.assertEqual(bob_result["totals"]["all"], 1)
        self.assertEqual(bob_result["totals"]["fans"], 1)

    def test_unbound_account_does_not_inherit_fan_owner_or_open_observation_database(self):
        unbound = self.make_private("unbound")
        self.import_fans(["200"], private=unbound)
        missing = self.root / "missing.sqlite3"
        result = summary(missing, unbound, now=self.now)
        self.assertEqual(result["state"], "needs_profile")
        self.assertIsNone(result["profile"])
        self.assertEqual(result["cohorts"]["registered_fan_count"], 0)
        self.assertIsNone(result["totals"]["all"])
        self.assertFalse(missing.exists())
        self.assertEqual(summary(missing, None, now=self.now)["state"], "needs_profile")

    def test_unknown_complete_empty_and_partial_empty_are_distinct(self):
        initial = summary(self.database, self.private, now=self.now)
        self.assertEqual(initial["state"], "not_collected")
        self.assertIsNone(initial["totals"]["all"])
        self.assertIsNone(initial["hourly"][11]["all"])
        self.sighting([], self.now-timedelta(minutes=30), complete=False)
        partial = summary(self.database, self.private, now=self.now)
        self.assertEqual(partial["state"], "partial")
        self.assertIsNone(partial["totals"]["all"])
        self.assertIsNone(partial["hourly"][11]["all"])
        self.assertTrue(partial["hourly"][11]["partial"])
        self.assertEqual(partial["hourly"][11]["snapshot_count"], 1)
        self.sighting([], self.now-timedelta(minutes=20))
        known_empty = summary(self.database, self.private, now=self.now)
        self.assertEqual(known_empty["totals"]["all"], 0)
        self.assertEqual(known_empty["hourly"][11]["all"], 0)
        self.assertIsNone(known_empty["hourly"][11]["monthly"])
        self.assertIsNone(known_empty["hourly"][10]["all"])

    def test_partial_positive_cells_are_lower_bounds_and_keep_coverage(self):
        self.sighting(["200"], complete=False)
        result = summary(self.database, self.private, now=self.now)
        self.assertEqual(result["totals"]["all"], 1)
        self.assertEqual(result["totals"]["complete_snapshot_count"], 0)
        self.assertEqual(result["totals"]["partial_snapshot_count"], 1)
        self.assertEqual(result["totals"]["positive_days"], 1)
        self.assertTrue(result["hourly"][12]["partial"])
        self.assertEqual(result["hourly"][12]["all"], 1)
        self.assertEqual(result["hourly"][12]["other"], 1)
        self.assertIsNone(result["hourly"][12]["monthly"])

    def test_average_denominator_excludes_unknown_partial_empty_days(self):
        self.sighting(["200", "201"], self.now-timedelta(days=2))
        self.sighting([], self.now-timedelta(days=1), complete=False)
        self.sighting([])
        result = summary(self.database, self.private, now=self.now)
        hour = result["hourly"][12]
        self.assertEqual(hour["all"], 2)
        self.assertEqual(hour["observed_days"], 3)
        self.assertEqual(hour["known_days"], 2)
        self.assertEqual(result["daily"][-2]["hours"][12]["known_days"], 0)
        self.assertEqual(result["daily"][-1]["hours"][12]["known_days"], 1)

    def test_first_observed_and_repeat_use_own_history_not_other_dj(self):
        self.sighting(["200"], self.now-timedelta(days=10))
        self.sighting(["201"], self.now-timedelta(days=10), dj="900")
        self.sighting(["200", "201"], self.now-timedelta(days=1))
        self.sighting(["200", "201", "202"], self.now-timedelta(minutes=40))
        self.sighting(["202"], self.now-timedelta(minutes=20))
        result = summary(self.database, self.private, now=self.now)
        yesterday = result["daily"][-2]["hours"][12]
        today = result["daily"][-1]["hours"][11]
        self.assertEqual((yesterday["first"], yesterday["repeat"]), (1, 1))
        self.assertEqual((today["first"], today["repeat"]), (1, 2))
        self.assertEqual((result["totals"]["first"], result["totals"]["repeat"]), (2, 1))

    def test_jst_boundary_window_start_and_future_are_respected(self):
        now = datetime(2026, 10, 31, 15, 5, tzinfo=timezone.utc)  # Nov 1 00:05 JST.
        start = datetime(2026, 10, 25, 15, tzinfo=timezone.utc)  # Oct 26 00:00 JST.
        self.sighting(["199"], start-timedelta(microseconds=1))
        self.sighting(["200"], start)
        self.sighting(["201"], now-timedelta(minutes=10))
        self.sighting(["202"], now-timedelta(minutes=1))
        self.sighting(["999"], now+timedelta(minutes=1))
        self.ranking([("200", 30)], month="2026-10", at=now-timedelta(days=1))
        self.ranking([("202", 50)], month="2026-11", at=now-timedelta(minutes=1))
        self.ranking([("999", 100)], month="2026-11", at=now+timedelta(minutes=1))
        result = summary(self.database, self.private, now=now)
        self.assertEqual(result["window"], {"days": 7, "start_date": "2026-10-26",
                                           "end_date": "2026-11-01", "timezone": "Asia/Tokyo"})
        self.assertEqual(result["monthly"]["month"], "2026-11")
        self.assertEqual(result["totals"]["all"], 3)
        self.assertEqual(result["totals"]["monthly"], 1)
        self.assertEqual(result["daily"][0]["hours"][0]["all"], 1)
        self.assertEqual(result["daily"][-2]["hours"][23]["all"], 1)
        self.assertEqual(result["daily"][-1]["hours"][0]["all"], 1)
        self.assertIsNone(result["daily"][-1]["hours"][1]["all"])

    def test_28_day_range_and_strict_validation(self):
        self.sighting(["200"], self.now-timedelta(days=14))
        self.assertIsNone(summary(self.database, self.private, now=self.now)["totals"]["all"])
        result = summary(self.database, self.private, days=28, now=self.now)
        self.assertEqual(len(result["daily"]), 28)
        self.assertEqual(len(result["hourly"]), 24)
        self.assertEqual(result["totals"]["all"], 1)
        for value in (0, 1, 29, True, 7.0, "7", None):
            with self.subTest(days=value), self.assertRaises(ValueError):
                summary(self.database, self.private, days=value, now=self.now)
        with self.assertRaises(ValueError):
            summary(self.database, self.private, now=datetime(2026, 10, 9))

    def test_reads_are_read_only_and_missing_files_are_never_created(self):
        self.import_fans(["200"])
        self.sighting(["200"])
        self.ranking([("200", 10)])
        before_main = self.database.read_bytes()
        before_private = self.private.read_bytes()
        summary(self.database, self.private, now=self.now)
        self.assertEqual(self.database.read_bytes(), before_main)
        self.assertEqual(self.private.read_bytes(), before_private)
        missing = self.root / "missing.sqlite3"
        with self.assertRaises(sqlite3.OperationalError):
            summary(missing, self.private, now=self.now)
        self.assertFalse(missing.exists())
        with self.assertRaises(sqlite3.OperationalError):
            summary(self.database, missing, now=self.now)
        self.assertFalse(missing.exists())

    def test_old_databases_without_monthly_or_private_fan_tables_remain_readable(self):
        private = self.make_private("old-private", "100", with_fans=False)
        with sqlite3.connect(self.database) as conn:
            conn.execute("DROP TABLE monthly_dj_listeners")
            conn.execute("DROP TABLE monthly_dj_snapshots")
        self.sighting(["200"])
        result = summary(self.database, private, now=self.now)
        self.assertEqual(result["totals"]["all"], 1)
        self.assertEqual(result["totals"]["fans"], 0)
        self.assertIsNone(result["totals"]["monthly"])
        self.assertEqual(result["monthly"]["state"], "not_collected")


if __name__ == "__main__":
    unittest.main()
