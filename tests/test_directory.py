import unittest
from unittest.mock import patch

from spoondev import directory
from spoondev.collector import FetchError
from spoondev.directory import DirectoryError, resolve_user, search_users


USER = {"id": 100, "nickname": "Name", "tag": "1222kii"}


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        directory._cache.clear()

    def test_search_and_cache(self):
        with patch("spoondev.directory.fetch_snapshot", return_value={"results": [USER], "next": None}) as fetch:
            result = search_users("1222kii")
            self.assertEqual(result["users"][0]["id"], "100")
            self.assertEqual(result["source"], "spoon_public_search")
            self.assertFalse(result["has_more"])
            search_users("1222kii")
            self.assertEqual(fetch.call_count, 1)

    def test_cursor_pagination(self):
        next_url = directory.GATEWAY + "/search/user?cursor=next"
        with patch("spoondev.directory.fetch_snapshot", side_effect=[
            {"results": [USER], "next": next_url},
            {"results": [{**USER, "id": 101}], "next": ""}]) as fetch:
            result = search_users("1222kii", offset=1, limit=1)
            self.assertEqual(result["users"][0]["id"], "101")
            self.assertFalse(result["has_more"])
            self.assertEqual(fetch.call_args.args[0], next_url)

    def test_invalid_and_failed_responses(self):
        for value in [FetchError("url", "HTTP 503", 503), {"results": {}},
                      {"status_code": 403, "results": []}, {"results": [{"id": "100", "nickname": "Name"}]}]:
            with patch("spoondev.directory.fetch_snapshot", return_value=value):
                with self.assertRaises(DirectoryError):
                    search_users("1222kii")

    def test_foreign_and_wrong_endpoint_pagination(self):
        for next_url in ["https://example.invalid/search/user", directory.GATEWAY + "/users", "http://jp-gw.spooncast.net/search/user"]:
            with patch("spoondev.directory.fetch_snapshot", return_value={"results": [USER], "next": next_url}):
                with self.assertRaises(DirectoryError):
                    search_users("1222kii")

    def test_cycle(self):
        next_url = directory.GATEWAY + "/search/user?cursor=x"
        with patch("spoondev.directory.fetch_snapshot", return_value={"results": [], "next": next_url}):
            with self.assertRaisesRegex(DirectoryError, "cycle"):
                search_users("1222kii")

    def test_numeric_lookup(self):
        with patch("spoondev.directory.fetch_snapshot", return_value={"status_code": 200, "results": [USER]}) as fetch:
            self.assertEqual(resolve_user("100")["tag"], "1222kii")
            self.assertEqual(fetch.call_args.args[0], "https://jp-api.spooncast.net/users/100/")

    def test_lookup_mismatch_and_invalid_id(self):
        with patch("spoondev.directory.fetch_snapshot", return_value={"results": [USER]}):
            with self.assertRaises(DirectoryError):
                resolve_user("101")
        with self.assertRaises(DirectoryError):
            resolve_user("../100")


if __name__ == "__main__":
    unittest.main()
