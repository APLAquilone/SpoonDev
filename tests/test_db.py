import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from spoondev.db import initialize, save_snapshot, broadcaster_listeners, listener_broadcasters

class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'test.sqlite'
        initialize(self.path)
        self.payload = dict(room_id='room', broadcaster=dict(id='host',name='same'),
                            listeners=[dict(id='listener',name='same')], complete=True,
                            observed_at='2026-10-09T09:00:00+09:00')

    def test_identity_dedup_and_utc(self):
        self.payload['listeners'] *= 2
        save_snapshot(self.path,self.payload)
        rows = broadcaster_listeners(self.path,'host')
        self.assertEqual(rows[0]['user_id'],'listener')
        self.assertEqual(rows[0]['complete_count'],1)
        self.assertEqual(rows[0]['first_seen_at'],'2026-10-09T00:00:00.000000+00:00')
        self.assertEqual(listener_broadcasters(self.path,'listener')[0]['user_id'],'host')

    def test_history_and_incomplete_counts(self):
        save_snapshot(self.path,self.payload)
        self.payload.update(complete=False,observed_at='2026-10-10T00:00:00Z')
        self.payload['listeners'][0]['name']='new'
        save_snapshot(self.path,self.payload)
        row = broadcaster_listeners(self.path,'host')[0]
        self.assertEqual((row['name'],row['complete_count'],row['incomplete_count']),('new',1,1))
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT name FROM names WHERE user_id='listener' ORDER BY snapshot_id").fetchall(),[('same',),('new',)])
        older = copy.deepcopy(self.payload)
        older['observed_at']='2026-10-08T00:00:00Z'
        older['listeners'][0]['name']='old'
        save_snapshot(self.path,older)
        self.assertEqual(broadcaster_listeners(self.path,'host')[0]['name'],'new')

    def test_validation_does_not_write(self):
        cases = []
        for field,value in [('observed_at','2026-10-09'),('complete',1),('room_id','')]:
            item=copy.deepcopy(self.payload); item[field]=value; cases.append(item)
        item=copy.deepcopy(self.payload)
        item['listeners'].append(dict(id='listener',name='conflict')); cases.append(item)
        for item in cases:
            with self.assertRaises(ValueError): save_snapshot(self.path,item)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM users').fetchone()[0],0)

    def test_database_failure_rolls_back_entire_snapshot(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TRIGGER reject_members BEFORE INSERT ON memberships BEGIN SELECT RAISE(ABORT,'fail'); END")
        with self.assertRaises(sqlite3.IntegrityError): save_snapshot(self.path,self.payload)
        with sqlite3.connect(self.path) as conn:
            for table in ('users','snapshots','names','memberships'):
                self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],0)

    def test_empty_snapshot_does_not_infer_departure(self):
        save_snapshot(self.path,self.payload)
        self.payload.update(listeners=[],complete=False,observed_at='2026-10-10T00:00:00Z')
        save_snapshot(self.path,self.payload)
        self.assertEqual(broadcaster_listeners(self.path,'host')[0]['complete_count'],1)

if __name__ == '__main__': unittest.main()
