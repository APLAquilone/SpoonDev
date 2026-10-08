from contextlib import redirect_stdout, redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from spoondev.cli import main
from spoondev.db import broadcaster_listeners
from spoondev.spoon import SpoonRateLimit


class CliTests(unittest.TestCase):
    def test_spoon_collection_persists_temperature(self):
        payload = {'room_id':'room','broadcaster':{'id':'host','name':'Host','tag':'host-tag'},
                   'listeners':[{'id':'listener','name':'Listener','favorite_temperature':37}],
                   'complete':True,'observed_at':'2026-10-09T00:00:00Z'}
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory)/'db.sqlite3')
            with patch('spoondev.spoon.collect_spoon', return_value=([payload],[])), redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--db',db,'collect-spoon','--once']),0)
            self.assertEqual(broadcaster_listeners(db,'host')[0]['favorite_temperature'],37)

    def test_rate_limited_once_reports_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory)/'db.sqlite3')
            with patch('spoondev.spoon.collect_spoon', side_effect=SpoonRateLimit(600)), redirect_stderr(io.StringIO()):
                self.assertEqual(main(['--db',db,'collect-spoon','--once']),1)
