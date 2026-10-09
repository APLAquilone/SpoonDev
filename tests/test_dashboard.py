from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from spoondev import db, fans, profiledb
from spoondev.dashboard import summary


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "observations.sqlite3"
        db.initialize(self.database)
        profiledb.initialize(self.database)
        self.private = self.make_private("alice", "100")
        self.now = datetime(2026, 10, 9, 3, tzinfo=timezone.utc)

    def make_private(self, name, spoon_id=None):
        path = self.root / (name + ".sqlite3")
        fans.initialize(path)
        profiledb.initialize(path)
        with sqlite3.connect(path) as conn:
            conn.executescript("""CREATE TABLE favorites(id TEXT PRIMARY KEY,name TEXT,tag TEXT);
                CREATE TABLE account_settings(singleton INTEGER PRIMARY KEY,spoon_id TEXT,
                  spoon_name TEXT,spoon_tag TEXT,updated_at TEXT);""")
            if spoon_id:
                conn.execute("INSERT INTO account_settings VALUES(1,?,?,?,?)",
                             (spoon_id, "my broadcaster", "my-tag", "2026-10-01T00:00:00+00:00"))
        return path

    def sighting(self, broadcaster, listeners, at, complete=True):
        return db.save_snapshot(self.database, {
            "room_id": "room-" + broadcaster, "broadcaster": {"id": broadcaster, "name": "DJ " + broadcaster},
            "listeners": listeners, "observed_at": at.isoformat(), "complete": complete,
        })

    def test_unlinked_account_never_inherits_imported_owner_or_shared_stats(self):
        unlinked = self.make_private("unlinked")
        fans.import_followers(unlinked, {"owner": {"id": "100", "name": "legacy owner"},
                                        "followers": [{"id": "200"}], "complete": True})
        self.sighting("100", [{"id": "200", "name": "shared listener"}], self.now)
        result = summary(self.database, unlinked, now=self.now)
        self.assertEqual(result["state"], "needs_profile")
        self.assertIsNone(result["profile"])
        self.assertEqual(result["live"]["listeners"], [])
        self.assertEqual(result["counts"]["observed_listeners"], 0)
        self.assertEqual(result["counts"]["registered_fans"], 0)
        self.assertEqual(result["counts"]["fan_lists"], 1)
        self.assertEqual(summary(self.database, None, now=self.now)["counts"]["fan_lists"], 0)

    def test_listener_average_and_initial_observation_are_broadcaster_scoped(self):
        self.sighting("100", [{"id": "200", "name": "returner", "favorite_temperature": 10}],
                      self.now - timedelta(days=9))
        self.sighting("100", [{"id": "200", "name": "returner", "favorite_temperature": 50}],
                      self.now - timedelta(days=4))
        self.sighting("100", [{"id": "201", "name": "new observed", "favorite_temperature": 20}],
                      self.now - timedelta(days=1))
        self.sighting("100", [{"id": "200", "name": "returner", "tag": "returner-tag"},
                              {"id": "201", "name": "new observed", "favorite_temperature": 60}],
                      self.now - timedelta(minutes=10), complete=False)
        self.sighting("900", [{"id": "200", "name": "renamed elsewhere", "favorite_temperature": 100},
                              {"id": "999", "name": "other-only"}], self.now)
        result = summary(self.database, self.private, now=self.now)
        self.assertEqual(result["live"]["state"], "partial")
        self.assertTrue(result["live"]["recent"])
        older = summary(self.database, self.private, now=self.now + timedelta(hours=1))
        self.assertEqual(older["live"]["state"], "partial")
        self.assertFalse(older["live"]["recent"])
        self.assertEqual(result["live"]["latest_listener_count"], 2)
        self.assertEqual(result["counts"]["observed_listeners"], 2)
        self.assertEqual(result["counts"]["recent_listeners"], 2)
        self.assertEqual(result["counts"]["new_listeners_7d"], 1)
        users = {user["id"]: user for user in result["live"]["listeners"]}
        self.assertEqual(users["200"]["name"], "returner")
        self.assertEqual(users["200"]["tag"], "returner-tag")
        self.assertIsNone(users["200"]["latest_temperature"])
        self.assertEqual(users["200"]["observed_average_temperature"], 30)
        self.assertEqual(users["200"]["temperature_observation_count"], 2)
        self.assertEqual(users["200"]["observation_count"], 3)
        self.assertFalse(users["200"]["newly_observed"])
        self.assertEqual(users["201"]["observed_average_temperature"], 40)
        self.assertTrue(users["201"]["newly_observed"])
        self.assertIsNone(users["201"]["total_spoon"])
        self.assertEqual([row["id"] for row in result["newly_observed_listeners"]], ["201"])
        self.assertEqual(sum(day["new_listener_count"] for day in result["daily"]), 1)
        self.assertEqual(result["daily"][-1]["listener_count"], 2)
        self.assertEqual(result["daily"][-1]["snapshot_count"], 1)

    def test_private_counts_and_binding_isolate_accounts(self):
        bob = self.make_private("bob", "900")
        for private, owner, users in ((self.private, "100", ["200", "201"]), (bob, "900", ["999"])):
            fans.import_followers(private, {"owner": {"id": owner, "name": "owner"},
                                           "followers": [{"id": uid} for uid in users], "complete": True})
        with sqlite3.connect(self.private) as conn:
            conn.execute("INSERT INTO favorites VALUES('200','favorite',NULL)")
        self.sighting("100", [{"id": "200", "name": "alice listener"}], self.now)
        self.sighting("900", [{"id": "999", "name": "bob listener"}], self.now)
        alice_result = summary(self.database, self.private, now=self.now)
        bob_result = summary(self.database, bob, now=self.now)
        self.assertEqual(alice_result["counts"]["favorites"], 1)
        self.assertEqual(bob_result["counts"]["favorites"], 0)
        self.assertEqual(alice_result["counts"]["registered_fans"], 2)
        self.assertEqual(bob_result["counts"]["registered_fans"], 1)
        self.assertEqual([row["id"] for row in alice_result["live"]["listeners"]], ["200"])
        self.assertEqual([row["id"] for row in bob_result["live"]["listeners"]], ["999"])

    def test_monthly_state_is_current_month_and_latest_own_ranking_only(self):
        def ranking(dj, temperature, month, days=0, complete=True):
            profiledb.save_dj_ranking(self.database, {"id": dj, "name": "DJ"},
                [{"user": {"id": "200", "name": "monthly user"}, "temperature": temperature}],
                month, complete, (self.now - timedelta(days=days)).isoformat())
        ranking("100", 90, "2026-09", days=9)
        ranking("900", 999, "2026-10")
        self.assertEqual(summary(self.database, self.private, now=self.now)["monthly"]["state"], "not_collected")
        ranking("100", 40, "2026-10", days=1)
        ranking("100", 55, "2026-10", complete=False)
        monthly = summary(self.database, self.private, now=self.now)["monthly"]
        self.assertEqual(monthly["state"], "partial")
        self.assertEqual(monthly["listener_count"], 1)
        self.assertEqual(monthly["listeners"][0]["temperature"], 55)
        # A later successful empty ranking replaces previous membership.
        profiledb.save_dj_ranking(self.database, {"id": "100", "name": "DJ"}, [],
                                 "2026-10", True, self.now.isoformat())
        monthly = summary(self.database, self.private, now=self.now)["monthly"]
        self.assertEqual(monthly["state"], "empty")
        self.assertEqual(monthly["listeners"], [])
        self.assertIsNotNone(monthly["observed_at"])

    def test_japan_day_and_month_and_future_sightings(self):
        now = datetime(2026, 10, 31, 15, 5, tzinfo=timezone.utc)  # Nov 1 00:05 JST.
        self.sighting("100", [{"id": "200", "name": "yesterday"}], now-timedelta(minutes=10))
        self.sighting("100", [{"id": "201", "name": "today"}], now-timedelta(minutes=1))
        self.sighting("100", [{"id": "999", "name": "future"}], now+timedelta(minutes=10))
        result = summary(self.database, self.private, now=now)
        self.assertEqual(result["month"], "2026-11")
        self.assertEqual(result["daily"][-1]["date"], "2026-11-01")
        self.assertEqual(result["daily"][-2]["listener_count"], 1)
        self.assertEqual(result["daily"][-1]["listener_count"], 1)
        self.assertEqual(result["counts"]["observed_listeners"], 2)
        self.assertEqual([row["id"] for row in result["live"]["listeners"]], ["201"])

    def test_empty_latest_snapshot_and_bounded_lists_keep_precise_counts(self):
        listeners = [{"id": str(i), "name": "listener"} for i in range(200, 225)]
        self.sighting("100", listeners, self.now-timedelta(minutes=20))
        result = summary(self.database, self.private, now=self.now)
        self.assertEqual(len(result["live"]["listeners"]), 20)
        self.assertEqual(result["live"]["latest_listener_count"], 25)
        self.assertEqual(result["counts"]["recent_listeners"], 25)
        self.sighting("100", [], self.now-timedelta(minutes=5))
        result = summary(self.database, self.private, now=self.now)
        self.assertEqual(result["live"]["listeners"], [])
        self.assertEqual(result["live"]["latest_listener_count"], 0)
        # Positive earlier sightings remain evidence; empty snapshots do not infer absence.
        self.assertEqual(result["counts"]["recent_listeners"], 25)

    def test_reads_do_not_write_and_missing_databases_are_not_created(self):
        self.sighting("100", [{"id": "200", "name": "listener"}], self.now)
        before_main = self.database.read_bytes()
        before_private = self.private.read_bytes()
        summary(self.database, self.private, now=self.now)
        self.assertEqual(self.database.read_bytes(), before_main)
        self.assertEqual(self.private.read_bytes(), before_private)
        missing = self.root / "missing.sqlite3"
        with self.assertRaises(sqlite3.OperationalError):
            summary(missing, self.private, now=self.now)
        self.assertFalse(missing.exists())
        with self.assertRaises(ValueError):
            summary(self.database, self.private, now=datetime(2026, 10, 9))

    def test_gift_amounts_keep_dj_scope_and_unknown_distinct_from_zero(self):
        from spoondev import gifts
        self.sighting("100", [{"id": "200", "name": "amount"}, {"id": "201", "name": "zero"},
                              {"id": "202", "name": "missing"}], self.now)
        gifts._save(self.database, "100", {"200": ("200", "amount", None, 123),
                                          "201": ("201", "zero", None, 0)}, False, self.now.isoformat())
        gifts._save(self.database, "900", {"202": ("202", "other dj amount", None, 999)},
                    True, self.now.isoformat())
        result = summary(self.database, self.private, now=self.now)
        users = {user["id"]: user for user in result["live"]["listeners"]}
        self.assertEqual(users["200"]["total_spoon"], 123)
        self.assertEqual(users["201"]["total_spoon"], 0)
        self.assertIsNone(users["202"]["total_spoon"])
        self.assertEqual(result["gifts"]["state"], "partial")
        self.assertEqual(result["gifts"]["source_period"], "unspecified")
        self.assertEqual(result["gifts"]["listener_count"], 2)
        self.assertNotIn("total_spent", result["gifts"])
        self.assertIn("月間・生涯", result["notes"]["gifts"])

    def test_gift_future_snapshots_and_unknown_rows_do_not_invent_total(self):
        from spoondev import gifts
        gifts._save(self.database,"100",{"200":("200","known",None,123),
                                         "201":("201","unknown",None,None)},True,self.now.isoformat())
        gifts._save(self.database,"100",{"202":("202","future",None,999)},
                    True,(self.now+timedelta(minutes=1)).isoformat())
        result=summary(self.database,self.private,now=self.now)
        self.assertIsNone(result['gifts']['observed_total_spoon'])
        self.assertEqual([row['id'] for row in result['gifts']['listeners']],['200','201'])
        self.assertEqual(result['gifts']['listeners'][0]['total_spoon'],123)


if __name__ == "__main__":
    unittest.main()
