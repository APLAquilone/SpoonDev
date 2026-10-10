"""Activity endpoints use public sightings and the caller's private cohort only."""
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from spoondev import accounts, db, fans, profiledb, web


class ActivityWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.database = root / "public.sqlite3"
        self.auth = root / "accounts.sqlite3"
        db.initialize(self.database)
        profiledb.initialize(self.database)
        self.profiles = {
            "123": {"id": "123", "name": "Alice DJ", "tag": "alice-dj"},
            "124": {"id": "124", "name": "Bob DJ", "tag": "bob-dj"},
            "125": {"id": "125", "name": "Elsewhere DJ", "tag": "elsewhere"},
        }
        self.listeners = {
            "456": {"id": "456", "name": "Alice registered fan"},
            "457": {"id": "457", "name": "Alice this-month visitor"},
            "458": {"id": "458", "name": "Alice monthly member"},
            "459": {"id": "459", "name": "Bob-only private fan"},
            "460": {"id": "460", "name": "Unrelated public listener"},
        }
        profiledb.cache_users(self.database, list(self.profiles.values()) + list(self.listeners.values()))
        self.private = {}
        for name, dj in (("alice", "123"), ("bob", "124"), ("unbound", None), ("firstuser", None)):
            uid = accounts.create(self.auth, name, name + "-password-123", must_change=name == "firstuser")
            self.private[name] = accounts.private(self.auth, uid)
            if dj:
                accounts.account_settings(self.private[name], {"spoon_profile": self.profiles[dj]}, confirmed=True)
        fans.import_followers(self.private["alice"], {"owner": self.profiles["123"],
            "followers": [self.listeners["456"]], "complete": True})
        fans.import_followers(self.private["bob"], {"owner": self.profiles["124"],
            "followers": [self.listeners["459"]], "complete": True})
        self.now = datetime.now(timezone.utc)
        stamp = (self.now - timedelta(minutes=5)).isoformat()
        # Only the visit to Alice's own room establishes the visitor cohort.
        db.save_snapshot(self.database, {"room_id": "alice-room", "broadcaster": self.profiles["123"],
            "listeners": [self.listeners["457"]], "complete": True, "observed_at": stamp})
        # Fans and confirmed monthly members need not have been sampled at Alice's
        # room to contribute their saved activity at another broadcaster.
        db.save_snapshot(self.database, {"room_id": "elsewhere-room", "broadcaster": self.profiles["125"],
            "listeners": list(self.listeners.values()), "complete": True, "observed_at": stamp})
        month = self.now.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m")
        profiledb.save_dj_ranking(self.database, self.profiles["123"],
            [{"user": self.listeners["458"], "temperature": 42}], month, True, observed_at=stamp)
        self.server = web.make_server(self.database, port=0, auth=True, auth_database=self.auth)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, cookie="", body=None, server=None):
        server = server or self.server
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        headers = {"Origin": self.origin, "Content-Type": "application/json", "Cookie": cookie}
        conn.request("GET" if body is None else "POST", path,
                     None if body is None else json.dumps(body), headers)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), json.loads(response.read())
        conn.close()
        return result

    def login(self, username):
        status, headers, _ = self.request("/api/login", body={"username": username,
            "password": username + "-password-123"})
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";", 1)[0]

    def test_cross_dj_cohort_is_private_to_each_account(self):
        alice = self.request("/api/analytics?days=7", self.login("alice"))
        bob = self.request("/api/analytics?days=7", self.login("bob"))
        self.assertEqual(alice[0], 200, alice)
        self.assertEqual(bob[0], 200, bob)
        self.assertEqual(alice[2]["profile"]["id"], "123")
        self.assertEqual(alice[2]["totals"]["all"], 3)
        self.assertEqual(alice[2]["totals"]["fans"], 1)
        self.assertEqual(alice[2]["totals"]["monthly"], 1)
        self.assertEqual(bob[2]["profile"]["id"], "124")
        self.assertEqual(bob[2]["totals"]["all"], 1)
        self.assertEqual(bob[2]["totals"]["fans"], 1)
        # No public unrelated listener or another account's registered fan joins
        # Alice's audience just because they share the same observed room.
        cells = [hour for day in alice[2]["daily"] for hour in day["hours"]]
        self.assertEqual(max(hour["all"] or 0 for hour in cells), 3)
        self.assertTrue(any(hour["all"] is None for hour in cells))

    def test_personal_activity_authentication_and_password_change(self):
        self.assertEqual(self.request("/api/users/456/activity")[0], 401)
        forced = self.request("/api/users/456/activity", self.login("firstuser"))
        self.assertEqual(forced[0], 403)
        self.assertTrue(forced[2]["password_change_required"])
        # A linked DJ is needed for private cohort analytics, but a saved public
        # listener's activity remains usable for an unbound logged-in account.
        unbound = self.request("/api/users/456/activity", self.login("unbound"))
        self.assertEqual(unbound[0], 200, unbound)
        self.assertEqual(unbound[2]["window"]["days"], 7)

    def test_personal_activity_all_djs_and_valid_windows(self):
        cookie = self.login("alice")
        for query, expected in (("", 7), ("?days=7", 7), ("?days=28", 28)):
            with self.subTest(window=expected):
                result = self.request("/api/users/456/activity" + query, cookie)
                self.assertEqual(result[0], 200, result)
                value = result[2]
                self.assertEqual(value["window"]["days"], expected)
                self.assertEqual(value["window"]["timezone"], "Asia/Tokyo")
                self.assertEqual(len(value["daily"]), expected)
                self.assertTrue(all(len(day["hours"]) == 24 for day in value["daily"]))
                cells = [hour for day in value["daily"] for hour in day["hours"]]
                self.assertTrue(any(hour["all"] == 1 for hour in cells))
                self.assertTrue(any(hour["all"] is None for hour in cells))
                self.assertNotIn("cohorts", value)

    def test_personal_activity_rejects_bad_ids_and_scope_overrides(self):
        cookie = self.login("alice")
        for user_id in ("", "0", "0456", "456x", "-456", "456%2f789", "1" * 21):
            with self.subTest(user_id=user_id):
                result = self.request("/api/users/" + user_id + "/activity", cookie)
                self.assertEqual(result[0], 400, result)
                self.assertIn("error", result[2])
        for query in ("days=", "days=1", "days=29", "days=no", "days=7&days=28",
                      "days=7&owner_id=124", "dj_id=124", "account_id=bob", "scope=global"):
            with self.subTest(query=query):
                result = self.request("/api/users/456/activity?" + query, cookie)
                self.assertEqual(result[0], 400, result)
                self.assertIn("error", result[2])
        with patch("spoondev.directory.resolve_user") as resolve:
            unknown = self.request("/api/users/999999999/activity", cookie)
            self.assertEqual(unknown[0], 404, unknown)
            resolve.assert_not_called()

    def test_local_personal_activity_works_without_accounts(self):
        server = web.make_server(self.database, port=0, auth=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = self.request("/api/users/456/activity?days=7", server=server)
            self.assertEqual(result[0], 200, result)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_compact_list_refreshes_do_not_compute_full_insights(self):
        cookie = self.login("alice")
        with patch("spoondev.insights.listener_insights") as metrics:
            for path in ("/api/users?q=Alice", "/api/fans?owner_id=123", "/api/favorites/activity?ids=456"):
                with self.subTest(path=path):
                    result = self.request(path, cookie)
                    self.assertEqual(result[0], 200, result)
            metrics.assert_not_called()
        with patch("spoondev.insights.listener_insights", return_value={"456": {"fixture": True}}) as metrics:
            result = self.request("/api/users/456", cookie)
            self.assertEqual(result[0], 200, result)
            self.assertEqual(result[2]["insights"], {"fixture": True})
            metrics.assert_called_once()


if __name__ == "__main__":
    unittest.main()
