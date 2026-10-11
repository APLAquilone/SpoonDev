from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from spoondev import dashboard, db, fans, profiledb
from spoondev.webdata import _read


class DashboardFollowupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "observations.sqlite3"
        db.initialize(self.database)
        profiledb.initialize(self.database)
        self.now = datetime(2026, 10, 11, 15, 5, tzinfo=timezone.utc)
        self.private = self.private_for("alice", "100")

    def private_for(self, name, dj):
        path = self.root / (name + ".sqlite3")
        fans.initialize(path)
        with sqlite3.connect(path) as conn:
            conn.execute("""CREATE TABLE account_settings(singleton INTEGER PRIMARY KEY,
                spoon_id TEXT,spoon_name TEXT,spoon_tag TEXT,updated_at TEXT)""")
            if dj is not None:
                conn.execute("INSERT INTO account_settings VALUES(1,?,?,NULL,?)",
                             (dj, "DJ " + dj, self.now.isoformat()))
        return path

    def sighting(self, dj, at, listeners, *, complete=True, room=None):
        return db.save_snapshot(self.database, {
            "room_id": room or "room-" + dj,
            "broadcaster": {"id": dj, "name": "DJ " + dj, "tag": "dj-" + dj},
            "listeners": listeners, "observed_at": at.isoformat(), "complete": complete,
        })

    def result_users(self):
        result = dashboard.summary(self.database, self.private, now=self.now)
        return result, {user["id"]: user for user in result["live"]["listeners"]}

    def test_old_own_visit_does_not_hide_new_elsewhere_observation(self):
        own_time = self.now - timedelta(days=2)
        elsewhere_time = self.now - timedelta(minutes=2)
        own = self.sighting("100", own_time,
                            [{"id": "200", "name": "same name", "favorite_temperature": 71}])
        foreign = self.sighting("900", elsewhere_time,
                                [{"id": "200", "name": "same name", "favorite_temperature": 0}])
        result, users = self.result_users()
        user = users["200"]
        self.assertEqual(result["live"]["state"], "stale")
        self.assertFalse(user["own_recent"])
        self.assertTrue(user["recent"])
        self.assertTrue(user["current"])
        self.assertEqual(user["activity_state"], "current")
        self.assertEqual(user["latest_temperature"], 71)
        self.assertEqual(user["observed_average_temperature"], 71)
        self.assertEqual(user["own_observation"]["snapshot_id"], own)
        self.assertEqual(user["own_observation"]["user_id"], "100")
        self.assertEqual(user["latest_observation"]["snapshot_id"], foreign)
        self.assertEqual(user["latest_observation"]["user_id"], "900")
        self.assertEqual(user["latest_observation"]["name"], "DJ 900")
        self.assertEqual(user["latest_observation"]["favorite_temperature"], 0)
        self.assertEqual(user["last_live_at"], elsewhere_time.isoformat(timespec="microseconds"))
        self.assertEqual(user["live"][0]["user_id"], "900")

    def test_names_ids_future_and_missing_temperature_stay_separate(self):
        self.sighting("100", self.now - timedelta(hours=1), [
            {"id": "200", "name": "same", "favorite_temperature": 54},
            {"id": "201", "name": "same", "favorite_temperature": 99},
        ])
        current = self.sighting("900", self.now - timedelta(minutes=2),
                                [{"id": "200", "name": "renamed"}], complete=False)
        self.sighting("901", self.now + timedelta(minutes=1),
                      [{"id": "200", "name": "future", "favorite_temperature": 100}])
        self.sighting("902", self.now,
                      [{"id": "999", "name": "same", "favorite_temperature": 88}])
        result, users = self.result_users()
        self.assertEqual(set(users), {"200", "201"})
        self.assertEqual(users["200"]["latest_observation"]["snapshot_id"], current)
        self.assertIsNone(users["200"]["latest_observation"]["favorite_temperature"])
        self.assertFalse(users["200"]["latest_observation"]["complete"])
        self.assertFalse(users["200"]["current"])
        self.assertTrue(users["200"]["recent"])
        self.assertEqual(users["201"]["latest_observation"]["user_id"], "100")
        self.assertFalse(users["201"]["recent"])
        self.assertEqual(result["counts"]["observed_listeners"], 2)

    def test_latest_own_observation_remains_latest_if_no_followup(self):
        at = self.now - timedelta(minutes=1)
        self.sighting("100", at, [{"id": "200", "name": "listener", "favorite_temperature": 0}])
        _, users = self.result_users()
        self.assertEqual(users["200"]["latest_observation"], users["200"]["own_observation"])
        followup = dashboard.listener_followup(self.database, self.private, "200", now=self.now)
        self.assertEqual(followup["state"], "ready")
        self.assertEqual(followup["observations"], [])
        self.assertFalse(followup["has_more"])
        self.assertIsNone(followup["next_before_id"])

    def test_followup_strictly_after_latest_own_baseline_and_japan_midnight(self):
        early = self.now - timedelta(hours=1)
        baseline_at = self.now - timedelta(minutes=10)  # 23:55 JST.
        later_at = self.now - timedelta(minutes=1)     # 00:04 JST.
        self.sighting("100", early, [{"id": "200", "name": "listener"}])
        self.sighting("900", self.now - timedelta(minutes=20), [{"id": "200", "name": "listener"}])
        own = self.sighting("100", baseline_at, [{"id": "200", "name": "listener"}])
        self.sighting("901", baseline_at, [{"id": "200", "name": "same time"}])
        after = self.sighting("902", later_at,
                              [{"id": "200", "name": "listener", "favorite_temperature": 0}], complete=False)
        self.sighting("903", self.now + timedelta(seconds=1), [{"id": "200", "name": "future"}])
        self.sighting("904", later_at, [{"id": "201", "name": "listener"}])
        result = dashboard.listener_followup(self.database, self.private, "200", now=self.now)
        self.assertEqual(result["baseline"]["snapshot_id"], own)
        self.assertEqual([row["snapshot_id"] for row in result["observations"]], [after])
        self.assertEqual(result["observations"][0]["favorite_temperature"], 0)
        self.assertFalse(result["observations"][0]["complete"])
        japan = ZoneInfo("Asia/Tokyo")
        self.assertEqual(datetime.fromisoformat(result["baseline"]["observed_at"]).astimezone(japan).day, 11)
        self.assertEqual(datetime.fromisoformat(result["observations"][0]["observed_at"]).astimezone(japan).day, 12)

    def test_pagination_uses_time_and_id_not_insertion_order(self):
        baseline_at = self.now - timedelta(hours=2)
        self.sighting("100", baseline_at, [{"id": "200", "name": "listener"}])
        ids = []
        # Insert older points after newer points. Last inserted is not latest.
        for minutes in (1, 2, 2, 3, 4):
            ids.append(self.sighting("900", self.now - timedelta(minutes=minutes),
                                     [{"id": "200", "name": "listener"}]))
        first = dashboard.listener_followup(self.database, self.private, "200", limit=2, now=self.now)
        self.assertEqual([row["snapshot_id"] for row in first["observations"]], [ids[0], ids[2]])
        self.assertTrue(first["has_more"])
        second = dashboard.listener_followup(self.database, self.private, "200", limit=2,
                                             before_id=first["next_before_id"], now=self.now)
        self.assertEqual([row["snapshot_id"] for row in second["observations"]], [ids[1], ids[3]])
        last = dashboard.listener_followup(self.database, self.private, "200", limit=2,
                                           before_id=second["next_before_id"], now=self.now)
        self.assertEqual([row["snapshot_id"] for row in last["observations"]], [ids[4]])
        self.assertFalse(last["has_more"])

    def test_private_binding_controls_cohort_and_followup_baseline(self):
        bob = self.private_for("bob", "900")
        blank = self.private_for("blank", None)
        self.sighting("100", self.now - timedelta(hours=2), [{"id": "200", "name": "listener"}])
        self.sighting("900", self.now - timedelta(hours=1), [{"id": "999", "name": "same"}])
        self.sighting("901", self.now - timedelta(minutes=1), [{"id": "200", "name": "listener"}])
        self.assertEqual([user["id"] for user in dashboard.summary(self.database, bob, now=self.now)["live"]["listeners"]], ["999"])
        unauthorized = dashboard.listener_followup(self.database, bob, "200", now=self.now)
        self.assertEqual(unauthorized["state"], "not_observed")
        self.assertIsNone(unauthorized["baseline"])
        self.assertEqual(unauthorized["observations"], [])
        unlinked = dashboard.listener_followup(self.database, blank, "200", now=self.now)
        self.assertEqual(unlinked["state"], "needs_profile")
        self.assertEqual(unlinked["observations"], [])

    def test_followup_readonly_and_invalid_cursor_does_not_cross_listener(self):
        self.sighting("100", self.now - timedelta(hours=1), [{"id": "200", "name": "listener"}])
        foreign = self.sighting("900", self.now - timedelta(minutes=1), [{"id": "201", "name": "other"}])
        before = self.database.read_bytes()
        private_before = self.private.read_bytes()
        dashboard.listener_followup(self.database, self.private, "200", now=self.now)
        self.assertEqual(before, self.database.read_bytes())
        self.assertEqual(private_before, self.private.read_bytes())
        with self.assertRaises(ValueError):
            dashboard.listener_followup(self.database, self.private, "200", before_id=foreign, now=self.now)
        for kwargs in ({"limit": 0}, {"limit": 101}, {"limit": True}, {"before_id": -1}, {"before_id": True}):
            with self.assertRaises(ValueError):
                dashboard.listener_followup(self.database, self.private, "200", now=self.now, **kwargs)
        with self.assertRaises(ValueError):
            dashboard.listener_followup(self.database, self.private, "not numeric", now=self.now)
        missing = self.root / "missing.sqlite3"
        with self.assertRaises(sqlite3.OperationalError):
            dashboard.listener_followup(missing, self.private, "200", now=self.now)
        self.assertFalse(missing.exists())

    def test_latest_location_queries_are_batched_and_cohort_is_bounded(self):
        users = [{"id": str(i), "name": "listener"} for i in range(200, 225)]
        self.sighting("100", self.now - timedelta(hours=1), users)
        self.sighting("900", self.now - timedelta(minutes=1), users)
        queries = []

        @contextmanager
        def traced_read(*args, **kwargs):
            with _read(*args, **kwargs) as conn:
                conn.set_trace_callback(queries.append)
                yield conn

        with patch.object(dashboard, "_read", traced_read):
            result = dashboard.summary(self.database, self.private, now=self.now)
        self.assertEqual(len(result["live"]["listeners"]), 20)
        self.assertEqual(result["live"]["latest_listener_count"], 25)
        self.assertEqual(sum("WITH targets(id) AS" in query for query in queries), 1)
        self.assertEqual(sum("SELECT * FROM activity_rows" in query for query in queries), 1)
        self.assertTrue(all(user["latest_observation"]["user_id"] == "900"
                            for user in result["live"]["listeners"]))

    def test_failed_room_attempt_downgrades_activity_without_erasing_history(self):
        self.sighting("100", self.now - timedelta(hours=1), [{"id": "200", "name": "listener"}])
        sighting = self.sighting("900", self.now - timedelta(minutes=3),
                                 [{"id": "200", "name": "listener", "favorite_temperature": 0}])
        with sqlite3.connect(self.database) as conn:
            stamp = (self.now - timedelta(minutes=1)).isoformat(timespec="microseconds")
            conn.execute("""INSERT INTO observation_attempts
                (room_id,broadcaster_id,started_at,finished_at,page_count,state)
                VALUES('room-900','900',?,?,0,'failed')""", (stamp, stamp))
        _, users = self.result_users()
        self.assertFalse(users["200"]["current"])
        self.assertEqual(users["200"]["activity_state"], "recent")
        self.assertEqual(users["200"]["latest_observation"]["snapshot_id"], sighting)
        self.assertEqual(users["200"]["latest_observation"]["favorite_temperature"], 0)
        followup = dashboard.listener_followup(self.database, self.private, "200", now=self.now)
        self.assertEqual([row["snapshot_id"] for row in followup["observations"]], [sighting])

    def test_dashboard_uses_shared_cadence_window_for_all_frame_activity(self):
        self.sighting("100", self.now - timedelta(days=1), [{"id": "200", "name": "listener"}])
        self.sighting("900", self.now - timedelta(minutes=23),
                      [{"id": "200", "name": "listener", "favorite_temperature": 0}])
        from spoondev import webdata
        with patch.object(webdata, "_current_observation_window", return_value=1500) as window:
            _, users = self.result_users()
        self.assertEqual(window.call_count, 1)
        self.assertEqual(window.call_args.args[1], self.now)
        self.assertEqual(users["200"]["current_seconds"], 1500)
        self.assertTrue(users["200"]["current"])
        self.assertFalse(users["200"]["own_recent"])


if __name__ == "__main__":
    unittest.main()
