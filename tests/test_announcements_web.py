"""Announcements are administrator-managed text for actual user-role accounts."""
from datetime import datetime, timedelta
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from zoneinfo import ZoneInfo

from spoondev import accounts, db, fans, web


class AnnouncementsWebTests(unittest.TestCase):
    password = "announcements-fixture-password-123"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.database = root / "public.sqlite3"
        self.auth = root / "accounts.sqlite3"
        db.initialize(self.database)
        self.ids = {}
        for name, label, forced in (("kitomoya", "利用者", False),
                                    ("alice", "管理者", False),
                                    ("tester", "テスト", False),
                                    ("firstuser", "利用者", True)):
            self.ids[name] = accounts.create(self.auth, name, self.password,
                                            label=label, must_change=forced)
        self.server = web.make_server(self.database, port=0, auth=True,
                                      auth_database=self.auth)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
        self.message = {"date": self.today.isoformat(), "importance": "important",
                        "content": "メンテナンスのお知らせ\n収集を再開しました。"}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, body=None, cookie="", csrf="", *, origin=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port,
                                                 timeout=10)
        headers = {"Origin": self.origin if origin is None else origin,
                   "Content-Type": "application/json", "Cookie": cookie,
                   "X-CSRF-Token": csrf}
        connection.request("GET" if body is None else "POST", path,
                           None if body is None else json.dumps(body), headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), json.loads(response.read())
        connection.close()
        return result

    def login(self, username):
        status, headers, _ = self.request("/api/login", {"username": username,
                                          "password": self.password})
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        session = accounts.session(self.auth, cookie.split("=", 1)[1])
        return cookie, session["csrf"]

    def save(self, message=None):
        cookie, csrf = self.login("kitomoya")
        result = self.request("/api/admin/announcements/save",
                              self.message if message is None else message, cookie, csrf)
        self.assertEqual(result[0], 200, result)
        return result[2]["announcement"]

    def test_authentication_role_and_password_change_gates(self):
        self.assertEqual(self.request("/api/announcements")[0], 401)
        self.assertEqual(self.request("/api/admin/announcements")[0], 401)
        self.assertEqual(self.request("/api/admin/announcements/save", self.message)[0], 401)
        cookie, csrf = self.login("alice")
        # A display label named 管理者 grants neither management nor write access.
        self.assertEqual(self.request("/api/admin/announcements", cookie=cookie)[0], 403)
        self.assertEqual(self.request("/api/admin/announcements/save", self.message,
                                      cookie, csrf)[0], 403)
        self.assertEqual(self.request("/api/admin/announcements/delete", {"id": 1,
                                      "confirm": True}, cookie, csrf)[0], 403)
        forced, forced_csrf = self.login("firstuser")
        response = self.request("/api/announcements", cookie=forced)
        self.assertEqual(response[0], 403)
        self.assertTrue(response[2]["password_change_required"])
        self.assertEqual(self.request("/api/admin/announcements/save", self.message,
                                      forced, forced_csrf)[0], 403)

    def test_actual_user_role_receives_announcements_independent_of_labels(self):
        saved = self.save()
        for username in ("alice", "tester"):
            with self.subTest(username=username):
                cookie, _ = self.login(username)
                result = self.request("/api/announcements", cookie=cookie)
                self.assertEqual(result[0], 200, result)
                self.assertEqual([item["id"] for item in result[2]["announcements"]],
                                 [saved["id"]])
                self.assertEqual(result[2]["announcements"][0]["content"],
                                 self.message["content"])
        admin, _ = self.login("kitomoya")
        # The admin's 利用者 label does not make them a user-role recipient.
        self.assertEqual(self.request("/api/announcements", cookie=admin)[2],
                         {"announcements": []})
        management = self.request("/api/admin/announcements", cookie=admin)
        self.assertEqual(management[0], 200)
        self.assertEqual(management[2]["announcements"][0]["id"], saved["id"])

    def test_write_requires_csrf_and_same_origin(self):
        cookie, csrf = self.login("kitomoya")
        for token, origin in (("", self.origin), ("invalid-token", self.origin),
                              (csrf, "https://other.example")):
            with self.subTest(token=token, origin=origin):
                self.assertEqual(self.request("/api/admin/announcements/save", self.message,
                                              cookie, token, origin=origin)[0], 403)
        self.assertEqual(self.request("/api/admin/announcements", cookie=cookie)[2],
                         {"announcements": []})
        saved = self.request("/api/admin/announcements/save", self.message, cookie, csrf)
        self.assertEqual(saved[0], 200)
        item_id = saved[2]["announcement"]["id"]
        delete = {"id": item_id, "confirm": True}
        self.assertEqual(self.request("/api/admin/announcements/delete", delete, cookie)[0], 403)
        self.assertEqual(self.request("/api/admin/announcements/delete", delete, cookie,
                                      csrf, origin="https://other.example")[0], 403)
        self.assertEqual(len(self.request("/api/admin/announcements", cookie=cookie)[2]
                             ["announcements"]), 1)

    def test_edit_and_confirmed_delete_take_effect_for_users(self):
        saved = self.save()
        admin, csrf = self.login("kitomoya")
        alice, _ = self.login("alice")
        edit = {**self.message, "id": saved["id"], "importance": "urgent",
                "content": "変更したお知らせ"}
        response = self.request("/api/admin/announcements/save", edit, admin, csrf)
        self.assertEqual(response[0], 200, response)
        self.assertEqual(response[2]["announcement"]["id"], saved["id"])
        visible = self.request("/api/announcements", cookie=alice)[2]["announcements"]
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]["content"], edit["content"])
        self.assertEqual(visible[0]["importance"], "urgent")
        self.assertEqual(self.request("/api/admin/announcements/delete",
                                      {"id": saved["id"]}, admin, csrf)[0], 400)
        self.assertEqual(len(self.request("/api/announcements", cookie=alice)[2]
                             ["announcements"]), 1)
        removed = self.request("/api/admin/announcements/delete",
                               {"id": saved["id"], "confirm": True}, admin, csrf)
        self.assertEqual(removed[0], 200, removed)
        self.assertEqual(removed[2], {"deleted": saved["id"]})
        self.assertEqual(self.request("/api/announcements", cookie=alice)[2],
                         {"announcements": []})

    def test_future_date_is_hidden_and_invalid_inputs_do_not_publish(self):
        future = {**self.message, "date": (self.today + timedelta(days=1)).isoformat()}
        saved = self.save(future)
        admin, csrf = self.login("kitomoya")
        alice, _ = self.login("alice")
        self.assertEqual(self.request("/api/announcements", cookie=alice)[2],
                         {"announcements": []})
        self.assertEqual(self.request("/api/admin/announcements", cookie=admin)[2]
                         ["announcements"][0]["id"], saved["id"])
        malformed = ({**self.message, "date": "2026-02-30"},
                     {**self.message, "importance": "administrator"},
                     {**self.message, "content": "   "},
                     {**self.message, "content": ["not text"]},
                     {**self.message, "id": True},
                     {**self.message, "id": -1})
        for message in malformed:
            with self.subTest(message=message):
                self.assertEqual(self.request("/api/admin/announcements/save", message,
                                              admin, csrf)[0], 400)
        self.assertEqual(len(self.request("/api/admin/announcements", cookie=admin)[2]
                             ["announcements"]), 1)
        self.assertEqual(self.request("/api/admin/announcements/save",
                                      {**self.message, "id": saved["id"] + 99999},
                                      admin, csrf)[0], 404)
        self.assertEqual(self.request("/api/admin/announcements/delete",
                                      {"id": saved["id"] + 99999, "confirm": True},
                                      admin, csrf)[0], 404)

    def test_plain_text_content_preserved_and_accounts_untouched(self):
        private = accounts.private(self.auth, self.ids["alice"])
        profile = {"id": "123", "name": "Own DJ"}
        accounts.account_settings(private, {"spoon_profile": profile}, confirmed=True)
        accounts.favorites(private, {"users": [{"id": "456", "name": "Favorite"}],
                                    "revision": 0})
        fans.import_followers(private, {"owner": profile,
                                      "followers": [{"id": "789", "name": "Private fan"}],
                                      "complete": True})
        with sqlite3.connect(self.auth) as connection:
            before_accounts = connection.execute("SELECT id,username,salt,password,label,role,"
                                                 "must_change FROM accounts ORDER BY id").fetchall()
        before_settings = accounts.account_settings(private)
        before_favorites = accounts.favorites(private)
        content = '<script>window.announcementXss = true</script>\n<img src=x onerror="alert(1)"> & hello'
        saved = self.save({**self.message, "content": content})
        alice, _ = self.login("alice")
        visible = self.request("/api/announcements", cookie=alice)[2]["announcements"]
        self.assertEqual(visible[0]["content"], content)
        self.assertEqual(visible[0]["id"], saved["id"])
        with sqlite3.connect(self.auth) as connection:
            self.assertEqual(connection.execute("SELECT id,username,salt,password,label,role,"
                                                "must_change FROM accounts ORDER BY id").fetchall(),
                             before_accounts)
        self.assertEqual(accounts.account_settings(private), before_settings)
        self.assertEqual(accounts.favorites(private), before_favorites)
        self.assertEqual(self.request("/api/fans?owner_id=123", cookie=alice)[2]["total"], 1)


if __name__ == "__main__":
    unittest.main()
