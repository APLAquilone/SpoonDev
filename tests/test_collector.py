import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest

from spoondev.collector import FetchError, fetch_many, fetch_snapshot


SNAPSHOT = {"room_id": "room1", "broadcaster": {"id": "host1", "name": "Host"},
            "listeners": [{"id": "listener1", "name": "Listener"}],
            "complete": True, "observed_at": "2026-10-09T00:00:00+00:00"}


class Handler(BaseHTTPRequestHandler):
    active = 0
    peak = 0
    lock = threading.Lock()

    def do_GET(self):
        if self.path == "/error":
            self.send_error(503)
            return
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://example.invalid/data")
            self.end_headers()
            return
        if self.path == "/slow":
            with self.lock:
                Handler.active += 1
                Handler.peak = max(Handler.peak, Handler.active)
            time.sleep(0.03)
            with self.lock:
                Handler.active -= 1
        body = b"not json" if self.path == "/invalid" else json.dumps(SNAPSHOT).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class CollectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_canonical_success(self):
        self.assertEqual(fetch_snapshot(self.base + "/ok"), SNAPSHOT)

    def test_errors_are_explicit_not_empty_snapshots(self):
        results = fetch_many([self.base + "/ok", self.base + "/error", self.base + "/invalid"])
        self.assertEqual(results[0], SNAPSHOT)
        self.assertIsInstance(results[1], Exception)
        self.assertEqual(results[1].status, 503)
        self.assertIsInstance(results[2], FetchError)

    def test_size_limit(self):
        self.assertIsInstance(fetch_snapshot(self.base + "/ok", max_response_bytes=8), FetchError)

    def test_plain_remote_http_rejected(self):
        self.assertIsInstance(fetch_snapshot("http://example.invalid/data"), FetchError)

    def test_cross_host_redirect_rejected(self):
        self.assertIsInstance(fetch_snapshot(self.base + "/redirect"), FetchError)

    def test_bounded_concurrency(self):
        Handler.peak = 0
        results = fetch_many([self.base + "/slow"] * 8, concurrency=2)
        self.assertTrue(all(result == SNAPSHOT for result in results))
        self.assertLessEqual(Handler.peak, 2)
        self.assertGreater(Handler.peak, 1)

    def test_invalid_concurrency(self):
        with self.assertRaises(ValueError):
            fetch_many([], concurrency=0)


if __name__ == "__main__":
    unittest.main()
