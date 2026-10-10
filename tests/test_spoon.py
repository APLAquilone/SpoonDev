import unittest
import threading
from unittest.mock import patch

from spoondev.collector import FetchError
from spoondev.spoon import SpoonRateLimit, collect_spoon


BASE = "https://jp-api.spooncast.net"
HOST = {"id": 10, "nickname": "Host", "tag": "host_handle"}
USER = {"id": 20, "nickname": "Same Name", "tag": "listener_handle", "favorite_temperature": 46.0}


def response(results, next_url=""):
    return {"status_code": 200, "results": results, "next": next_url}


class SpoonTests(unittest.TestCase):
    def run_collect(self, endpoints, **kwargs):
        with patch("spoondev.spoon.fetch_snapshot", side_effect=lambda url, **kw: endpoints[url]):
            return collect_spoon(**kwargs)

    def endpoints(self, listeners):
        return {BASE + "/lives/": response([{"id": 1, "author": HOST}]),
                BASE + "/lives/1/listeners/": listeners}

    def test_success_and_numeric_identity(self):
        snapshots, errors = self.run_collect(self.endpoints(response([USER])))
        self.assertFalse(errors)
        self.assertEqual(snapshots[0]["listeners"], [{"id": "20", "name": "Same Name", "tag": "listener_handle", "favorite_temperature": 46.0}])
        self.assertTrue(snapshots[0]["complete"])
        self.assertEqual(snapshots[0]["broadcaster"]["id"], "10")

    def test_pagination_deduplicates(self):
        next_url = BASE + "/lives/1/listeners/?cursor=next"
        endpoints = self.endpoints(response([USER], next_url))
        endpoints[next_url] = response([USER, {"id": 21, "nickname": "Same Name"}])
        snapshots, errors = self.run_collect(endpoints)
        self.assertFalse(errors)
        self.assertEqual(len(snapshots[0]["listeners"]), 2)

    def test_failed_first_page_is_not_zero_listeners(self):
        snapshots, errors = self.run_collect(self.endpoints(FetchError("url", "HTTP 403", 403)))
        self.assertFalse(snapshots)
        self.assertTrue(errors)

    def test_failed_later_page_retains_partial(self):
        next_url = BASE + "/lives/1/listeners/?cursor=next"
        endpoints = self.endpoints(response([USER], next_url))
        endpoints[next_url] = FetchError(next_url, "HTTP 503", 503)
        snapshots, errors = self.run_collect(endpoints)
        self.assertFalse(snapshots[0]["complete"])
        self.assertEqual(len(snapshots[0]["listeners"]), 1)
        self.assertTrue(errors)

    def test_cycle_and_foreign_host(self):
        for next_url in (BASE + "/lives/1/listeners/", "https://example.invalid/lives/1/listeners/"):
            snapshots, errors = self.run_collect(self.endpoints(response([USER], next_url)))
            self.assertTrue(errors)
            self.assertFalse(snapshots[0]["complete"])

    def test_page_limit(self):
        snapshots, errors = self.run_collect(self.endpoints(response([USER], BASE + "/lives/1/listeners/?cursor=x")), max_pages=1)
        self.assertTrue(errors)
        self.assertFalse(snapshots[0]["complete"])

    def test_valid_empty_page(self):
        snapshots, errors = self.run_collect(self.endpoints(response([])))
        self.assertFalse(errors)
        self.assertEqual(snapshots[0]["listeners"], [])
        self.assertTrue(snapshots[0]["complete"])

    def test_all_rooms_discovery_pagination(self):
        next_url = BASE + "/lives/?cursor=next"
        endpoints = self.endpoints(response([USER]))
        endpoints[BASE + "/lives/"] = response([{"id": 1, "author": HOST}], next_url)
        endpoints[next_url] = response([{"id": 2, "author": HOST}])
        endpoints[BASE + "/lives/2/listeners/"] = response([])
        snapshots, errors = self.run_collect(endpoints, max_rooms=0)
        self.assertFalse(errors)
        self.assertEqual([snapshot["room_id"] for snapshot in snapshots], ["1", "2"])

    def test_discovery_invalid_status(self):
        endpoints = {BASE + "/lives/": {"status_code": 403, "results": []}}
        snapshots, errors = self.run_collect(endpoints)
        self.assertFalse(snapshots)
        self.assertTrue(errors)

    def test_rate_limit_aborts_round(self):
        endpoints = self.endpoints(FetchError("url", "HTTP 429", 429, 120))
        with self.assertRaises(SpoonRateLimit) as caught:
            self.run_collect(endpoints)
        self.assertEqual(caught.exception.retry_after, 120)

    def test_discovery_rate_limit(self):
        with self.assertRaises(SpoonRateLimit) as caught:
            self.run_collect({BASE + "/lives/": FetchError("url", "HTTP 429", 429)})
        self.assertEqual(caught.exception.retry_after, 60)

    def test_unlimited_pages_follow_all_pages_without_truncation(self):
        following=BASE+'/lives/1/listeners/?cursor=second'
        endpoints=self.endpoints(response([USER],following))
        endpoints[following]=response([{'id':21,'nickname':'Second'}])
        snapshots,errors=self.run_collect(endpoints,max_pages=0)
        self.assertFalse(errors)
        self.assertTrue(snapshots[0]['complete'])
        self.assertEqual(len(snapshots[0]['listeners']),2)

    def test_stop_before_collection_makes_no_request_or_empty_snapshot(self):
        stop=threading.Event();stop.set()
        with patch('spoondev.spoon.fetch_snapshot') as fetch:
            snapshots,errors=collect_spoon(stopped_event=stop)
        fetch.assert_not_called();self.assertEqual(snapshots,[])
        self.assertTrue(errors)

    def test_missing_temperature_is_kept_unknown(self):
        listener=dict(USER,favorite_temperature=None)
        snapshots,errors=self.run_collect(self.endpoints(response([listener])))
        self.assertFalse(errors)
        self.assertNotIn('favorite_temperature',snapshots[0]['listeners'][0])


if __name__ == "__main__":
    unittest.main()
