from pathlib import Path
import sqlite3
import tempfile
import unittest
from spoondev.db import initialize, save_snapshot
from spoondev.webdata import search_users, user_details, stats, history

class WebDataTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'observations.sqlite'
        initialize(self.path)
        self.payload={'room_id':'room','broadcaster':{'id':'100','name':'same','tag':'host'},
                      'listeners':[{'id':'200','name':'same','tag':'old-tag','favorite_temperature':46},
                                   {'id':'201','name':'percent%under_score\\name','tag':'other'}],
                      'complete':True,'observed_at':'2026-10-09T00:00:00Z'}
        save_snapshot(self.path,self.payload)

    def test_duplicate_names_keep_identity(self):
        result=search_users(self.path,'same')
        self.assertEqual([r['id'] for r in result['users']],['100','200'])
        self.assertFalse(result['has_more'])
        self.assertEqual([r['id'] for r in search_users(self.path,'20')['users']],['200','201'])

    def test_literal_wildcards_and_injection(self):
        for q in ('%','_','\\'):
            self.assertEqual([r['id'] for r in search_users(self.path,q)['users']],['201'])
        self.assertEqual(search_users(self.path,"' OR 1=1 --")['users'],[])

    def test_latest_tag_and_last_seen(self):
        self.payload['observed_at']='2026-10-10T00:00:00Z'
        self.payload['listeners'][0]['tag']='new-tag'
        save_snapshot(self.path,self.payload)
        self.assertEqual(search_users(self.path,'old-tag')['users'],[])
        row=search_users(self.path,'new-tag')['users'][0]
        self.assertEqual(row['id'],'200')
        self.assertEqual(row['last_seen_at'],'2026-10-10T00:00:00.000000+00:00')

    def test_pagination(self):
        first=search_users(self.path,'',limit=2)
        last=search_users(self.path,'',limit=2,offset=2)
        self.assertTrue(first['has_more'])
        self.assertFalse(last['has_more'])
        self.assertEqual([r['id'] for r in first['users']+last['users']],['100','200','201'])
        for kwargs in ({'limit':0},{'limit':True},{'offset':-1}):
            with self.assertRaises(ValueError): search_users(self.path,'',**kwargs)

    def test_details_stats_and_history(self):
        listener=user_details(self.path,'200')
        self.assertEqual(listener['broadcasters'][0]['user_id'],'100')
        self.assertEqual(listener['listeners'],[])
        self.assertEqual(len(user_details(self.path,'100')['listeners']),2)
        self.assertIsNone(user_details(self.path,'missing'))
        self.assertEqual(stats(self.path),{'user_count':3,'snapshot_count':1,'last_observed_at':'2026-10-09T00:00:00.000000+00:00','monthly_indexed_djs':0})
        self.assertEqual(history(self.path,'100','200')[0]['favorite_temperature'],46)
        self.assertEqual(history(self.path,'other','200'),[])

    def test_missing_database_never_created(self):
        missing=Path(self.temp.name)/'missing.sqlite'
        for operation in (lambda: search_users(missing,''),lambda: user_details(missing,'100'),
                          lambda: stats(missing),lambda: history(missing,'100','200')):
            with self.assertRaises(sqlite3.OperationalError): operation()
            self.assertFalse(missing.exists())

    def test_queries_do_not_modify_database(self):
        before=self.path.read_bytes()
        search_users(self.path,'')
        user_details(self.path,'200')
        stats(self.path)
        history(self.path,'100','200')
        self.assertEqual(self.path.read_bytes(),before)

if __name__=='__main__': unittest.main()
