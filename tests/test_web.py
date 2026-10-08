import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen, Request

from spoondev.db import initialize, save_snapshot
from spoondev.web import make_server


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name)/'test.sqlite3'
        initialize(self.db)
        save_snapshot(self.db, {'room_id':'room','broadcaster':{'id':'1','name':'Host','tag':'host'},
            'listeners':[{'id':'2','name':'<script>alert(1)</script>','tag':'listener','favorite_temperature':42}],
            'complete':True,'observed_at':'2026-10-09T00:00:00Z'})
        self.server = make_server(self.db,port=0)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()

    def get(self,path):
        with urlopen(self.base+path,timeout=5) as response:
            return json.load(response)

    def test_search_details_and_history_over_http(self):
        self.assertEqual(self.get('/api/stats')['user_count'],2)
        self.assertEqual(self.get('/api/users?q=listener')['users'][0]['id'],'2')
        detail=self.get('/api/users/1')
        self.assertEqual(detail['listeners'][0]['favorite_temperature'],42)
        self.assertEqual(self.get('/api/users/2')['broadcasters'][0]['user_id'],'1')
        self.assertEqual(self.get('/api/history?broadcaster_id=1&listener_id=2')[0]['favorite_temperature'],42)

    def test_html_and_readonly_boundaries(self):
        with urlopen(self.base,timeout=5) as response:
            self.assertIn('text/html',response.headers['Content-Type'])
            self.assertEqual(response.headers['X-Content-Type-Options'],'nosniff')
            self.assertIn(b'<html',response.read().lower())
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(self.base+'/api/users',data=b'{}'),timeout=5)
        self.assertEqual(error.exception.code,405)

    def test_errors_are_structured(self):
        for path,status in [('/api/users?offset=-1',400),('/api/users?offset=no',400),
                            ('/api/history',400),('/api/users/missing',404),
                            ('/api/users?'+'&'.join(f'key{i}=x' for i in range(11)),400),('/missing',404)]:
            with self.subTest(path=path):
                with self.assertRaises(HTTPError) as error:
                    urlopen(self.base+path,timeout=5)
                self.assertEqual(error.exception.code,status)
                self.assertIn('error',json.load(error.exception))
