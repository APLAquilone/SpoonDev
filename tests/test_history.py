import tempfile
import unittest
from pathlib import Path
from spoondev.db import initialize, save_snapshot, temperature_history


class HistoryTests(unittest.TestCase):
    def test_pair_specific_temperature_history(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'test.sqlite3'
            initialize(db)
            for day, host, temperature in [(1,'host',30),(2,'other',90),(3,'host',40)]:
                save_snapshot(db, {'room_id': 'room', 'broadcaster': {'id':host,'name':host},
                    'listeners':[{'id':'listener','name':'Listener','favorite_temperature':temperature}],
                    'complete':True,'observed_at':f'2026-10-0{day}T00:00:00Z'})
            history = temperature_history(db,'host','listener')
            self.assertEqual([row['favorite_temperature'] for row in history],[30,40])
            self.assertEqual(temperature_history(db,'unknown','listener'),[])
