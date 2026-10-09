from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from spoondev import collection_status, db, gifts, insights, profiledb


class InsightsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/"observations.sqlite3"
        db.initialize(self.path)
        profiledb.initialize(self.path)
        self.now = datetime(2026, 10, 10, 6, tzinfo=timezone.utc)

    def observe(self, dj, room, time, users=("200",), complete=True, temperature=20):
        return db.save_snapshot(self.path, {"room_id": room, "broadcaster": {"id": dj, "name": "DJ "+dj},
            "listeners": [{"id": uid, "name": "Listener "+uid, "favorite_temperature": temperature} for uid in users],
            "observed_at": time.isoformat(), "complete": complete})

    def normal_history(self, users=("200",), djs=("100", "900"), interval=300):
        for day in (3, 2, 1):
            for dj in djs:
                start = self.now-timedelta(days=day)
                for i in range(4):
                    self.observe(dj, "room-"+dj, start+timedelta(seconds=i*interval), users)

    def put_gift(self, dj, values, *, minutes=0, complete=True):
        return gifts._save(self.path, dj,
            {uid: (uid, "Listener "+uid, None, value) for uid, value in values.items()},
            complete, (self.now-timedelta(minutes=minutes)).isoformat())

    def get(self, ids=("200",), own="100", now=None):
        return insights.listener_insights(self.path, list(ids), broadcaster_id=own, now=now or self.now)

    def test_material_insufficiency_and_legacy_ids_are_not_zero(self):
        result = self.get(ids=("legacy-id", "200", "200"))
        self.assertEqual(set(result), {"legacy-id", "200"})
        self.assertEqual(result["legacy-id"]["stay"]["state"], "not_collected")
        self.assertIsNone(result["200"]["stay"]["score"])
        self.assertIsNone(result["200"]["support"]["own_spoon"])
        self.observe("100", "room", self.now, users=("legacy-id",))
        stay = self.get(ids=("legacy-id",))["legacy-id"]["stay"]
        self.assertEqual(stay["state"], "insufficient")
        self.assertEqual(stay["observation_count"], 1)
        self.assertIsNone(stay["score"])
        self.assertIn("single_dj_scope", stay["reasons"])

    def test_reference_stay_uses_multiple_djs_days_and_balanced_continuation(self):
        self.normal_history()
        stay = self.get()["200"]["stay"]
        self.assertEqual(stay["state"], "ready")
        self.assertEqual(stay["score"], 100)
        self.assertEqual(stay["pair_count"], 18)
        self.assertEqual(stay["continued_pairs"], 18)
        self.assertEqual(stay["observed_days"], 3)
        self.assertEqual(stay["dj_count"], 2)
        self.assertEqual(stay["longest_chain"], 4)
        self.assertEqual(stay["own"]["observation_count"], 12)
        self.assertEqual(stay["own"]["observed_days"], 3)
        self.assertEqual(stay["own"]["pair_count"], 9)
        self.assertNotIn("minutes", stay)
        self.assertNotIn("probability", stay)

    def test_temperature_spoon_and_duplicate_snapshots_do_not_change_stay(self):
        self.normal_history()
        before = self.get()["200"]["stay"]
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE user_attributes SET favorite_temperature=999")
        self.put_gift("100", {"200": 1000000000})
        self.assertEqual(self.get()["200"]["stay"], before)
        # A duplicate import or rapid repeated request must not create pairs.
        for day in (3, 2, 1):
            for dj in ("100", "900"):
                for i in range(4):
                    self.observe(dj, "room-"+dj, self.now-timedelta(days=day)+timedelta(seconds=300*i))
        self.assertEqual(self.get()["200"]["stay"], before)

    def test_partial_missing_and_long_gaps_do_not_count_as_exits_or_stay_time(self):
        start = self.now-timedelta(hours=6)
        self.observe("100", "room", start)
        self.observe("100", "room", start+timedelta(minutes=5), users=(), complete=False)
        self.observe("100", "room", start+timedelta(minutes=10))
        self.observe("100", "room", start+timedelta(minutes=15))
        self.observe("100", "room", self.now)
        stay = self.get()["200"]["stay"]
        self.assertEqual(stay["pair_count"], 1)
        self.assertEqual(stay["continued_pairs"], 1)
        self.assertEqual(stay["longest_chain"], 2)
        self.assertIsNone(stay["long_chain_ratio"])
        self.assertEqual(stay["observation_count"], 4)
        self.assertIsNone(stay["score"])
        self.assertEqual(stay["own"]["last_seen_at"], self.now.isoformat(timespec="microseconds"))

    def test_same_dj_new_room_and_complete_missing_split_observation_chains(self):
        start = self.now-timedelta(hours=1)
        for i in range(2):self.observe("100", "one", start+timedelta(minutes=5*i))
        for i in range(2):self.observe("100", "two", start+timedelta(minutes=10+5*i))
        stay = self.get()["200"]["stay"]
        self.assertEqual(stay["longest_chain"], 2)
        self.assertEqual(stay["pair_count"], 2)
        self.observe("100", "two", start+timedelta(minutes=20), users=())
        self.observe("100", "two", start+timedelta(minutes=25))
        stay = self.get()["200"]["stay"]
        self.assertEqual(stay["pair_count"], 3)
        self.assertEqual(stay["continued_pairs"], 2)
        self.assertEqual(stay["longest_chain"], 2)
        self.assertEqual(stay["long_chain_ratio"], 0)
        self.assertEqual(stay["dj_count"], 1)

    def test_future_and_older_than_window_are_excluded_and_dates_are_japan(self):
        self.normal_history()
        before = self.get()["200"]["stay"]
        self.observe("777", "future", self.now+timedelta(minutes=1))
        self.observe("888", "old", self.now-timedelta(days=29))
        self.assertEqual(self.get()["200"]["stay"], before)
        at = datetime(2026, 10, 10, 15, 5, tzinfo=timezone.utc)
        self.observe("100", "japan", at-timedelta(minutes=10), users=("201",))
        self.observe("100", "japan", at, users=("201",))
        self.assertEqual(self.get(ids=("201",), now=at)["201"]["stay"]["observed_days"], 2)

    def test_gift_zero_unknown_partial_fallback_and_complete_empty_are_distinct(self):
        self.put_gift("100", {"200": 50, "201": 0}, minutes=20)
        self.put_gift("100", {"202": 10}, minutes=10, complete=False)
        result = self.get(ids=("200", "201", "202"))
        self.assertEqual(result["200"]["support"]["own_spoon"], 50)
        self.assertEqual(result["201"]["support"]["own_spoon"], 0)
        self.assertEqual(result["200"]["support"]["own_entry"]["state"], "previous_complete")
        self.assertEqual(result["200"]["support"]["own_entry"]["latest_state"], "partial")
        self.assertEqual(result["202"]["support"]["own_entry"]["state"], "partial")
        self.assertIsNone(self.get(ids=("999",))["999"]["support"]["own_spoon"])
        self.put_gift("100", {})
        result = self.get(ids=("200", "201"))
        for uid in ("200", "201"):
            self.assertIsNone(result[uid]["support"]["own_spoon"])
            self.assertEqual(result[uid]["support"]["own_entry"]["state"], "not_listed")
            self.assertIsNone(result[uid]["support"]["score"])

    def test_breadth_reference_never_weights_amounts_or_invents_period(self):
        for dj in ("100", "900", "901"):
            self.put_gift(dj, {"200": 1})
        first = self.get()["200"]["support"]
        self.assertEqual(first["known_djs"], 3)
        self.assertEqual(first["covered_djs"], 3)
        self.assertEqual(first["positive_djs"], 3)
        self.assertIsNotNone(first["breadth_score"])
        self.assertEqual(first["state"], "period_unverified")
        self.assertIsNone(first["score"])
        self.assertEqual(first["source_period"], "unspecified")
        self.put_gift("100", {"200": 1000000000})
        second = self.get()["200"]["support"]
        self.assertEqual(second["breadth_score"], first["breadth_score"])
        self.assertEqual(second["own_spoon"], 1000000000)
        self.assertNotIn("total_spent", second)
        self.assertNotIn("hhi", second)
        # An amount from the future must not leak into as-of results.
        gifts._save(self.path, "100", {"200": ("200", "future", None, 2)}, True,
                    (self.now+timedelta(minutes=1)).isoformat())
        self.assertEqual(self.get()["200"]["support"]["own_spoon"], 1000000000)

    def test_single_high_support_and_stale_incomplete_coverage_are_not_high_index(self):
        self.normal_history(djs=("100", "900", "901"))
        self.put_gift("900", {"200": 999999999})
        result = self.get()["200"]["support"]
        self.assertEqual(result["known_djs"], 3)
        self.assertEqual(result["positive_djs"], 1)
        self.assertIsNone(result["breadth_score"])
        self.assertIsNone(result["own_spoon"])
        self.put_gift("100", {"200": 20}, minutes=1500)
        self.put_gift("901", {"200": 10}, complete=False)
        result = self.get()["200"]["support"]
        self.assertEqual(result["own_entry"]["state"], "stale")
        self.assertEqual(result["own_spoon"], 20)
        self.assertEqual(result["covered_djs"], 1)
        self.assertIsNone(result["breadth_score"])

    def test_failed_latest_collection_preserves_normal_value_with_status(self):
        self.put_gift("100", {"200": 20}, minutes=20)
        collection_status.begin(self.path, "gifts", "100", 3600)
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE collection_runs SET started_at=?,finished_at=?,state='failed',details=?",
                ((self.now-timedelta(minutes=10)).isoformat(), (self.now-timedelta(minutes=9)).isoformat(),
                 '{"errors":["network unavailable"]}'))
        entry = self.get()["200"]["support"]["own_entry"]
        self.assertEqual(entry["total_spoon"], 20)
        self.assertEqual(entry["last_complete_spoon"], 20)
        self.assertEqual(entry["latest_state"], "failed")
        self.assertEqual(entry["errors"], ["network unavailable"])
        self.assertIsNotNone(entry["last_complete_at"])

    def test_batch_queries_are_constant_and_readonly_and_input_is_bounded(self):
        users = tuple(str(200+i) for i in range(20))
        self.normal_history(users=users)
        for dj in ("100", "900", "901"):
            self.put_gift(dj, {uid: 1 for uid in users})
        before = self.path.read_bytes()
        conn = sqlite3.connect(self.path.resolve().as_uri()+"?mode=ro", uri=True)
        self.addCleanup(conn.close)
        conn.execute("PRAGMA query_only=ON")
        queries = []
        conn.set_trace_callback(queries.append)
        first = insights.listener_insights(conn, ["200"], broadcaster_id="100", now=self.now)
        single_count = len(queries)
        queries.clear()
        batch = insights.listener_insights(conn, list(users), broadcaster_id="100", now=self.now)
        self.assertEqual(len(queries), single_count)
        self.assertEqual(batch["200"], first["200"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(insights.listener_insights(self.path, []), {})
        for ids in (["200"]*101, [None], [""], ["x"*201]):
            with self.assertRaises(ValueError):insights.listener_insights(self.path, ids)
        with self.assertRaises(ValueError):insights.listener_insights(self.path, ["200"], now=datetime(2026, 10, 10))
        missing = Path(self.temp.name)/"missing.sqlite3"
        with self.assertRaises(sqlite3.OperationalError):insights.listener_insights(missing, ["200"])
        self.assertFalse(missing.exists())

    def test_private_fan_only_id_has_no_public_observations(self):
        from spoondev import fans
        private = Path(self.temp.name)/"private.sqlite3"
        fans.initialize(private); profiledb.initialize(private)
        fans.import_followers(private, {"owner": {"id": "100", "name": "private owner"},
            "followers": [{"id": "321", "name": "private fan only"}], "complete": True})
        result = self.get(ids=("321",))["321"]
        self.assertEqual(result["stay"]["state"], "not_collected")
        self.assertIsNone(result["stay"]["score"])
        self.assertIsNone(result["support"]["own_spoon"])
        self.assertEqual(result["support"]["state"], "not_collected")

    def test_failed_room_and_slow_full_paging_break_chains_without_negative_pairs(self):
        start = self.now-timedelta(hours=2)
        for minute in (0, 10, 20):self.observe("100", "room", start+timedelta(minutes=minute))
        before = self.get()["200"]["stay"]
        self.assertEqual(before["pair_count"], 2)
        db.record_room_attempt(self.path, {"room_id": "room", "broadcaster_id": "100",
            "started_at": (start+timedelta(minutes=4)).isoformat(),
            "finished_at": (start+timedelta(minutes=5)).isoformat(), "page_count": 0, "state": "failed"})
        stay = self.get()["200"]["stay"]
        self.assertEqual(stay["pair_count"], 1)
        self.assertEqual(stay["continued_pairs"], 1)
        self.assertEqual(stay["interrupted_samples"], 1)
        slow = self.observe("100", "room", start+timedelta(minutes=15))
        db.record_room_attempt(self.path, {"room_id": "room", "broadcaster_id": "100",
            "started_at": (start+timedelta(minutes=11)).isoformat(),
            "finished_at": (start+timedelta(minutes=15)).isoformat(), "page_count": 15, "state": "completed"}, snapshot_id=slow)
        stay = self.get()["200"]["stay"]
        self.assertEqual(stay["observation_count"], 4)
        self.assertEqual(stay["pair_count"], 0)
        self.assertEqual(stay["excluded_slow_samples"], 1)
        self.assertIsNone(stay["score"])

    def test_same_collection_run_does_not_create_multiple_observations(self):
        start = self.now-timedelta(hours=2)
        for minute in (0, 5, 10):
            time = start+timedelta(minutes=minute)
            snapshot = self.observe("100", "room", time)
            db.record_room_attempt(self.path, {"room_id": "room", "broadcaster_id": "100",
                "started_at": (time-timedelta(seconds=10)).isoformat(), "finished_at": time.isoformat(),
                "page_count": 1, "state": "completed"}, snapshot_id=snapshot, collection_run_id=42)
        stay = self.get()["200"]["stay"]
        self.assertEqual(stay["observation_count"], 1)
        self.assertEqual(stay["pair_count"], 0)
        self.assertEqual(stay["longest_chain"], 1)

    def test_legacy_database_without_fetch_metadata_still_works(self):
        with sqlite3.connect(self.path) as conn:conn.execute("DROP TABLE observation_attempts")
        self.normal_history()
        self.assertEqual(self.get()["200"]["stay"]["state"], "ready")

    def test_newer_manual_success_supersedes_old_worker_failure(self):
        self.put_gift("100", {"200": 20}, minutes=5)
        with sqlite3.connect(self.path) as conn:
            conn.execute("""CREATE TABLE worker_attempts(id INTEGER PRIMARY KEY,kind TEXT,dj_id TEXT,
                started_at TEXT,finished_at TEXT,state TEXT,summary TEXT)""")
            conn.execute("INSERT INTO worker_attempts VALUES(1,'gifts','100',?,?, 'failed',?)",
                ((self.now-timedelta(minutes=20)).isoformat(), (self.now-timedelta(minutes=19)).isoformat(), '{"errors":["old failure"]}'))
        run = collection_status.begin(self.path, "gifts", "100", 3600)
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE collection_runs SET started_at=?,finished_at=?,state='completed',details='{}' WHERE id=?",
                ((self.now-timedelta(minutes=6)).isoformat(), (self.now-timedelta(minutes=4)).isoformat(), run))
        entry = self.get()["200"]["support"]["own_entry"]
        self.assertEqual(entry["total_spoon"], 20)
        self.assertEqual(entry["latest_state"], "completed")
        self.assertEqual(entry["errors"], [])


if __name__ == "__main__":
    unittest.main()
