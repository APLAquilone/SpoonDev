from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest

from spoondev.collection_status import begin,finish,read


class CollectionStatusTests(unittest.TestCase):
    def test_success_partial_and_total_failure_have_distinct_states(self):
        cases=[({'complete':True,'errors':[],'dj_count':1},'completed'),
               ({'complete':False,'errors':['HTTP500'],'dj_count':1},'partial'),
               ({'complete':False,'errors':['HTTP500'],'dj_count':0},'failed'),
               ({'complete':False,'errors':['HTTP500'],'snapshot_id':None},'failed'),
               ({'complete':False,'errors':['HTTP500'],'snapshot_id':1},'partial'),
               ({'complete':False,'errors':['bad snapshot'],'room_count':0},'failed')]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'db.sqlite3'
            for index,(details,state) in enumerate(cases):
                rid=begin(path,'monthly',str(index),3600)
                finish(path,rid,details)
                with sqlite3.connect(path) as conn:
                    status=read(conn)[0]
                self.assertEqual(status['state'],state)

    def test_running_is_unfinished_and_retry_delay_is_a_continuation_hint(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'db.sqlite3'
            rid=begin(path,'gifts','123',3600)
            with sqlite3.connect(path) as conn:
                status=read(conn)[0]
            self.assertEqual(status['state'],'running')
            self.assertIsNone(status['next_eligible_at'])
            finish(path,rid,{'complete':False,'snapshot_id':None,'retry_after':7200,'errors':['HTTP429']})
            with sqlite3.connect(path) as conn:
                status=read(conn)[0]
            elapsed=(datetime.fromisoformat(status['next_eligible_at'])-
                     datetime.fromisoformat(status['finished_at'])).total_seconds()
            self.assertEqual(elapsed,7200)


if __name__=='__main__':unittest.main()
