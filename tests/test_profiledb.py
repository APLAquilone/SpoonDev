from pathlib import Path
import sqlite3
import tempfile
import unittest
from spoondev import profiledb

class ProfileDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'db.sqlite'
        profiledb.initialize(self.path)
        self.listener={'id':'1','name':'same','tag':'listener'}
        self.destinations=[{'id':'2','name':'same','tag':'broadcaster','temperature':46.0}]

    def test_cache_identity_and_out_of_order(self):
        profiledb.cache_users(self.path,[self.listener,{'id':'2','name':'same'}],'2026-10-09T00:00:00Z')
        self.assertEqual([r['id'] for r in profiledb.search_cached(self.path,'same')],['1','2'])
        profiledb.cache_users(self.path,[{'id':'1','name':'old'}],'2026-10-08T00:00:00Z')
        self.assertEqual(profiledb.profile_user(self.path,'1')['name'],'same')
        self.assertIsNone(profiledb.profile_user(self.path,'unknown'))
        self.assertIsNone(profiledb.latest_profile(self.path,'1'))

    def test_monthly_history_separate_from_live_schema(self):
        profiledb.save_profile(self.path,self.listener,self.destinations,'2026-09',True,'2026-10-09T09:00:00+09:00')
        self.destinations[0]['temperature']=50
        profiledb.save_profile(self.path,self.listener,self.destinations,'2026-10',False,'2026-10-10T00:00:00Z')
        result=profiledb.latest_profile(self.path,'1')
        self.assertEqual(result['month'],'2026-10'); self.assertFalse(result['complete'])
        self.assertEqual(result['appearances'][0]['temperature'],50)
        self.assertEqual(result['appearances'][0]['source'],'monthly_profile')
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT month FROM profile_snapshots ORDER BY id').fetchall(),[('2026-09',),('2026-10',)])
            self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE name='snapshots'").fetchall(),[])

    def test_atomic_rollback(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TRIGGER fail BEFORE INSERT ON profile_destinations BEGIN SELECT RAISE(ABORT,'fail'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            profiledb.save_profile(self.path,self.listener,self.destinations,'2026-10',True)
        with sqlite3.connect(self.path) as conn:
            for table in ('profile_users','profile_snapshots','profile_destinations'):
                self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],0)

    def test_validation_and_nullable_temperature(self):
        for bad in (True,'46',float('nan'),float('inf')):
            with self.assertRaises(ValueError):
                profiledb.save_profile(self.path,self.listener,[dict(self.destinations[0],temperature=bad)],'2026-10',True)
        for month in ('2026-13','2026-00','2026-1','October'):
            with self.assertRaises(ValueError): profiledb.save_profile(self.path,self.listener,[] ,month,True)
        with self.assertRaises(ValueError): profiledb.save_profile(self.path,self.listener,[],'2026-10',1)
        with self.assertRaises(ValueError): profiledb.cache_users(self.path,[self.listener],'2026-10-09')
        profiledb.save_profile(self.path,self.listener,[{'id':'2','name':'same'}],'2026-10',True)
        self.assertIsNone(profiledb.latest_profile(self.path,'1')['appearances'][0]['temperature'])

    def test_per_dj_reverse_latest_empty_and_out_of_order(self):
        dj={'id':'2','name':'DJ','tag':'dj'}
        entries=[{'user':self.listener,'temperature':46}]
        profiledb.save_dj_ranking(self.path,dj,entries,'2026-10',True,'2026-10-10T00:00:00Z')
        profile=profiledb.latest_profile(self.path,'1')
        self.assertEqual(profile['appearances'][0]['user_id'],'2')
        self.assertEqual(profile['coverage'],'known_broadcasters')
        self.assertFalse(profile['complete'])
        self.assertTrue(profile['ranking_complete'])
        profiledb.save_dj_ranking(self.path,dj,[],'2026-10',True,'2026-10-09T00:00:00Z')
        self.assertIsNotNone(profiledb.latest_profile(self.path,'1'))
        profiledb.save_dj_ranking(self.path,dj,[],'2026-10',True,'2026-10-11T00:00:00Z')
        self.assertEqual(profiledb.latest_profile(self.path,'1')['appearances'],[])

    def test_per_dj_failure_rolls_back(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TRIGGER fail_dj BEFORE INSERT ON monthly_dj_listeners BEGIN SELECT RAISE(ABORT,'fail'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            profiledb.save_dj_ranking(self.path,{'id':'2','name':'DJ'},
                                     [{'user':self.listener,'temperature':46}],'2026-10',True)
        with sqlite3.connect(self.path) as conn:
            for table in ('profile_users','monthly_dj_snapshots','monthly_dj_listeners'):
                self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],0)

    def test_literal_search_and_dedup(self):
        user={'id':'3','name':'literal%_\\','tag':'tag'}
        profiledb.cache_users(self.path,[self.listener,user,user])
        for q in ('%','_','\\'):
            self.assertEqual([r['id'] for r in profiledb.search_cached(self.path,q)],['3'])
        with self.assertRaises(ValueError): profiledb.cache_users(self.path,[user,dict(user,name='conflict')])

if __name__=='__main__': unittest.main()
