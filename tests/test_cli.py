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
    def read_status(self,path):
        from spoondev.collection_status import read
        import sqlite3
        with sqlite3.connect(path) as conn:
            return read(conn)

    def test_spoon_collection_persists_temperature(self):
        payload = {'room_id':'room','broadcaster':{'id':'host','name':'Host','tag':'host-tag'},
                   'listeners':[{'id':'listener','name':'Listener','favorite_temperature':37}],
                   'complete':True,'observed_at':'2026-10-09T00:00:00Z'}
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory)/'db.sqlite3')
            with patch('spoondev.spoon.collect_spoon', return_value=([payload],[])), redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--db',db,'collect-spoon','--once']),0)
            self.assertEqual(broadcaster_listeners(db,'host')[0]['favorite_temperature'],37)

    def test_gift_collection_writes_round_status(self):
        from spoondev.collection_status import read
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/'db.sqlite3')
            summary={'errors':[],'complete':True,'user_count':2,'retry_after':0}
            with patch('spoondev.gifts.collect_gifts',return_value=summary) as fetch,redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--db',path,'collect-gifts','--dj-id','123','--once']),0)
            self.assertEqual(fetch.call_args.args,(path,'123'))
            with sqlite3.connect(path) as c:
                status=read(c)[0]
                self.assertEqual(status['state'],'completed');self.assertEqual(status['target'],'123')

    def test_rate_limited_once_reports_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory)/'db.sqlite3')
            with patch('spoondev.spoon.collect_spoon', side_effect=SpoonRateLimit(600)), redirect_stderr(io.StringIO()):
                self.assertEqual(main(['--db',db,'collect-spoon','--once']),1)

    def test_invalid_target_or_nonfinite_interval_has_no_running_record_or_db(self):
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/'db.sqlite3')
            for argv in [['collect-gifts','--dj-id','../123'],['collect-monthly','--dj-id','0'],
                         ['collect-gifts','--dj-id','123','--interval','nan'],
                         ['collect-monthly','--interval','inf']]:
                with self.subTest(argv=argv),redirect_stderr(io.StringIO()),self.assertRaises(SystemExit) as exc:
                    main(['--db',path,*argv,'--once'])
                self.assertEqual(exc.exception.code,2)
                self.assertFalse(Path(path).exists())

    def test_gift_multi_targets_are_normalized_and_rate_limit_stops_next(self):
        summary={'errors':['HTTP429'],'complete':False,'snapshot_id':None,
                 'user_count':0,'retry_after':6000}
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/'db.sqlite3')
            with patch('spoondev.gifts.collect_gifts',return_value=summary) as fetch,redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--db',path,'collect-gifts','--dj-id','00123','--dj-id','123','--dj-id','456','--once']),1)
            fetch.assert_called_once()
            self.assertEqual(fetch.call_args.args,(path,'123'))
            statuses=self.read_status(path)
            self.assertEqual(len(statuses),1)
            self.assertEqual(statuses[0]['state'],'failed')
            self.assertEqual(statuses[0]['target'],'123')
            from datetime import datetime
            self.assertEqual((datetime.fromisoformat(statuses[0]['next_eligible_at'])-
                              datetime.fromisoformat(statuses[0]['finished_at'])).total_seconds(),6000)

    def test_monthly_target_scope_and_failed_round(self):
        summary={'errors':['upstream failure'],'complete':False,'dj_count':0,'retry_after':0}
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/'db.sqlite3')
            with patch('spoondev.monthly.collect_monthly',return_value=summary) as fetch,redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--db',path,'collect-monthly','--dj-id','00123','--dj-id','123','--dj-id','456','--once']),1)
            self.assertEqual(fetch.call_args.kwargs['dj_ids'],['123','456'])
            status=self.read_status(path)[0]
            self.assertEqual(status['state'],'failed')
            self.assertEqual(status['target'],'123,456')

    def test_unexpected_collection_exit_finalizes_failed_run(self):
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/'db.sqlite3')
            with patch('spoondev.gifts.collect_gifts',side_effect=RuntimeError('unexpected')), self.assertRaises(RuntimeError):
                main(['--db',path,'collect-gifts','--dj-id','123','--once'])
            status=self.read_status(path)[0]
            self.assertEqual(status['state'],'failed')
            self.assertIsNotNone(status['finished_at'])
