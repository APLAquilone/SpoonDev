from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
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

    def test_cached_profile_tag_and_timestamp_are_not_swapped(self):
        from spoondev import profiledb
        profiledb.initialize(self.path)
        profiledb.cache_users(self.path,[{'id':'999','name':'cached','tag':'cached-tag'}],
                              '2026-10-10T00:00:00Z')
        result=search_users(self.path,'cached-tag')['users'][0]
        self.assertEqual(result['tag'],'cached-tag')
        self.assertEqual(result['last_seen_at'],'2026-10-10T00:00:00.000000+00:00')
        detail=user_details(self.path,'999')
        self.assertEqual(detail['tag'],'cached-tag')
        self.assertEqual(detail['last_seen_at'],result['last_seen_at'])

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

    def test_favorite_activity_only_uses_listener_observations(self):
        from spoondev.webdata import favorite_activity
        from spoondev import profiledb
        now = datetime.now(timezone.utc)
        self.payload['observed_at'] = (now-timedelta(minutes=5)).isoformat()
        self.payload['complete'] = False  # A positively observed listener still counts.
        self.payload['listeners'] = [{'id':'200','name':'recent'}]
        save_snapshot(self.path,self.payload)
        self.payload['observed_at'] = (now-timedelta(minutes=40)).isoformat()
        self.payload['listeners'] = [{'id':'201','name':'old'}]
        save_snapshot(self.path,self.payload)
        profiledb.initialize(self.path)
        profiledb.cache_users(self.path,[{'id':'201','name':'refreshed-profile'},
                                       {'id':'999','name':'monthly-only'}],now.isoformat())
        before = self.path.read_bytes()
        result = favorite_activity(self.path,['200','201','100','999','404','200'])
        users = {row['id']:row for row in result['users']}
        self.assertTrue(users['200']['recent'])
        self.assertFalse(users['201']['recent'])
        self.assertIsNotNone(users['201']['last_live_at'])
        for user_id in ('100','999','404'):
            self.assertFalse(users[user_id]['recent'])
            self.assertIsNone(users[user_id]['last_live_at'])
        self.assertEqual(len(result['users']),5)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(favorite_activity(self.path,[])['users'],[])
        for invalid in (['bad'],['1']*101,['1 OR 1=1'],[1]):
            with self.assertRaises(ValueError): favorite_activity(self.path,invalid)

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

    def test_fan_search_filters_entire_list_before_sort_and_pagination(self):
        from spoondev import fans, profiledb, webdata
        profiledb.initialize(self.path); fans.initialize(self.path)
        followers=[{'id':str(i),'name':f'Name {159-i:02d}'} for i in range(100,160)]
        for i in range(125,160): followers[i-100]['tag']='matching-tag'
        fans.import_followers(self.path,{'owner':{'id':'10','name':'owner'},
            'followers':followers,'complete':True})
        first=webdata.fan_destinations(self.path,'10',q='matching',sort='name',limit=20)
        last=webdata.fan_destinations(self.path,'10',q='matching',sort='name',limit=20,offset=20)
        self.assertEqual(first['registered_count'],60)
        self.assertEqual(first['total'],35)
        self.assertEqual(last['total'],35)
        self.assertTrue(first['has_more']);self.assertFalse(last['has_more'])
        rows=first['fans']+last['fans']
        self.assertEqual(len(rows),35)
        self.assertEqual([row['name'] for row in rows],sorted(row['name'] for row in rows))
        self.assertEqual(webdata.fan_destinations(self.path,'10',q='159')['fans'][0]['id'],'159')
        self.assertIn('159',[row['id'] for row in webdata.fan_destinations(self.path,'10',q='59')['fans']])
        missing=webdata.fan_destinations(self.path,'404',q='matching',activity='recent')
        self.assertEqual(missing['registered_count'],0);self.assertEqual(missing['total'],0)

    def test_fan_search_literal_wildcards_and_invalid_filters(self):
        from spoondev import fans, profiledb, webdata
        profiledb.initialize(self.path);fans.initialize(self.path)
        fans.import_followers(self.path,{'owner':{'id':'10','name':'owner'},'followers':[
            {'id':'300','name':'literal%under_score\\name','tag':'literal-tag'},
            {'id':'301','name':'ordinary','tag':'plain'}],'complete':True})
        before=self.path.read_bytes()
        for q in ('%','_','\\','literal-tag'):
            rows=webdata.fan_destinations(self.path,'10',q=q)
            self.assertEqual([row['id'] for row in rows['fans']],['300'])
            self.assertEqual(rows['registered_count'],2)
            self.assertEqual(rows['total'],1)
        self.assertEqual(webdata.fan_destinations(self.path,'10',q="' OR 1=1 --")['total'],0)
        for kwargs in ({'q':None},{'q':True},{'q':'x'*201},{'activity':'recent OR 1=1'}):
            with self.assertRaises(ValueError):webdata.fan_destinations(self.path,'10',**kwargs)
        self.assertEqual(self.path.read_bytes(),before)

    def test_fan_recent_filter_and_counts_stay_with_private_account(self):
        from spoondev import fans, profiledb, webdata
        profiledb.initialize(self.path);fans.initialize(self.path)
        private=Path(self.temp.name)/'alice.sqlite3';other=Path(self.temp.name)/'bob.sqlite3'
        for path in (private,other):fans.initialize(path);profiledb.initialize(path)
        owner={'id':'10','name':'same owner'}
        fans.import_followers(private,{'owner':owner,'followers':[
            {'id':'200','name':'alice recent'}, {'id':'201','name':'alice old'},
            {'id':'299','name':'future alice'}],'complete':True})
        for path in (self.path,other):
            fans.import_followers(path,{'owner':owner,'followers':[{'id':'999','name':'bob secret'}],'complete':True})
        now=datetime.now(timezone.utc)
        for uid,name,minutes in [('200','alice recent',5),('201','alice old',40),
                                 ('299','future alice',-2),('999','bob secret',5)]:
            save_snapshot(self.path,{'room_id':'room','broadcaster':{'id':'100','name':'host'},
                'listeners':[{'id':uid,'name':name}],'complete':True,
                'observed_at':(now-timedelta(minutes=minutes)).isoformat()})
        before=(self.path.read_bytes(),private.read_bytes(),other.read_bytes())
        result=webdata.fan_destinations(self.path,'10',private_database=private,activity='recent',q='alice')
        self.assertEqual(result['registered_count'],3);self.assertEqual(result['total'],1)
        self.assertEqual([row['id'] for row in result['fans']],['200'])
        self.assertEqual(result['recent_seconds'],webdata.RECENT_SECONDS)
        self.assertEqual(result['fans'][0]['live'][0]['user_id'],'100')
        self.assertEqual(webdata.fan_destinations(self.path,'10',private_database=private,q='bob secret')['total'],0)
        bob=webdata.fan_destinations(self.path,'10',private_database=other,activity='recent')
        self.assertEqual(bob['registered_count'],1);self.assertEqual(bob['total'],1)
        self.assertEqual([row['id'] for row in bob['fans']],['999'])
        self.assertEqual((self.path.read_bytes(),private.read_bytes(),other.read_bytes()),before)

    def test_fan_route_passes_search_and_activity_filters(self):
        import http.client
        import json
        import threading
        from urllib.parse import urlencode
        from spoondev import fans, web
        server=web.make_server(self.path,port=0)
        self.addCleanup(server.server_close)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join);self.addCleanup(server.shutdown)
        fans.import_followers(self.path,{'owner':{'id':'10','name':'owner'},'followers':[
            {'id':'200','name':'match% recent'},{'id':'999','name':'other'}],'complete':True})
        save_snapshot(self.path,{'room_id':'room','broadcaster':{'id':'100','name':'host'},
            'listeners':[{'id':'200','name':'match% recent'}],'complete':True,
            'observed_at':(datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()})
        for query,expected in (({'owner_id':'10','q':'%','activity':'recent'},200),
                               ({'owner_id':'10','activity':'invalid'},400)):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
            conn.request('GET','/api/fans?'+urlencode(query))
            response=conn.getresponse();result=json.loads(response.read());conn.close()
            self.assertEqual(response.status,expected)
            if expected==200:
                self.assertEqual(result['registered_count'],2);self.assertEqual(result['total'],1)
                self.assertEqual(result['activity'],'recent');self.assertEqual(result['q'],'%')
                self.assertEqual([row['id'] for row in result['fans']],['200'])

if __name__=='__main__': unittest.main()
