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

    def test_latest_temperature_and_absence_downgrade_current_fans(self):
        from spoondev import fans, profiledb, webdata
        profiledb.initialize(self.path);fans.initialize(self.path)
        followers=[{'id':str(uid),'name':str(uid)} for uid in range(400,408)]
        fans.import_followers(self.path,{'owner':{'id':'10','name':'owner'},
                                       'followers':followers,'complete':True})
        now=datetime.now(timezone.utc)
        def observe(room,minutes,listeners,complete=True,dj='100'):
            return save_snapshot(self.path,{'room_id':room,'broadcaster':{'id':dj,'name':'host'},
                'listeners':[{'id':str(uid),'name':str(uid),'favorite_temperature':temperature}
                             for uid,temperature in listeners],
                'complete':complete,'observed_at':(now-timedelta(minutes=minutes)).isoformat()})
        observe('first',8,[(400,20),(401,30),(402,40),(403,0)])
        observe('first',4,[(400,None),(402,41),(403,0)])
        observe('partial-room',7,[(404,20)],dj='101')
        observe('partial-room',3,[],complete=False,dj='101')
        observe('old',35,[(405,20)],dj='102')
        observe('future',-2,[(406,20)],dj='103')
        observe('temperature-missing',2,[(407,None)],dj='104')
        result=webdata.fan_destinations(self.path,'10')
        states={row['id']:row['activity_state'] for row in result['fans']}
        self.assertEqual(states,{'400':'recent','401':'recent','402':'current','403':'current',
                                 '404':'recent','405':'registered','406':'registered','407':'recent'})
        self.assertEqual(result['activity_counts'],{'current':2,'recent':4,'registered':2})
        self.assertEqual(result['category_counts'],result['activity_counts'])
        self.assertEqual(result['current_seconds'],600)
        rows={row['id']:row for row in result['fans']}
        self.assertIsNone(rows['400']['live'][0]['favorite_temperature'])
        self.assertEqual(rows['403']['live'][0]['favorite_temperature'],0)
        self.assertIsNone(rows['406']['last_live_at'])
        self.assertEqual({row['id'] for row in webdata.fan_destinations(self.path,'10',activity='current')['fans']},
                         {'402','403'})
        recent=webdata.fan_destinations(self.path,'10',activity='recent')
        self.assertEqual(recent['total'],6)  # Current observations also satisfy the recent filter.
        activity=webdata.favorite_activity(self.path,['400','401','402','403','404','405','406','407'])
        self.assertEqual({row['id']:row['activity_state'] for row in activity['users']},states)

    def test_fan_category_priority_is_applied_before_sort_and_page(self):
        from spoondev import fans, profiledb, webdata
        profiledb.initialize(self.path);fans.initialize(self.path)
        followers=[{'id':str(i),'name':f'A old {i}'} for i in range(400,455)]
        followers.extend([{'id':'900','name':'Z current'},{'id':'901','name':'Y recent'}])
        fans.import_followers(self.path,{'owner':{'id':'10','name':'owner'},'followers':followers,'complete':True})
        now=datetime.now(timezone.utc)
        for uid,minutes,temp in [('900',2,10),('901',20,20)]:
            save_snapshot(self.path,{'room_id':uid,'broadcaster':{'id':uid+'0','name':'host'},
                'listeners':[{'id':uid,'name':'Z current' if uid=='900' else 'Y recent',
                              'favorite_temperature':temp}],
                'complete':True,'observed_at':(now-timedelta(minutes=minutes)).isoformat()})
        first=webdata.fan_destinations(self.path,'10',sort='name',limit=2)
        second=webdata.fan_destinations(self.path,'10',sort='name',limit=2,offset=2)
        self.assertEqual([row['id'] for row in first['fans']],['900','901'])
        self.assertEqual([row['activity_state'] for row in second['fans']],['registered','registered'])
        self.assertEqual(first['activity_counts'],{'current':1,'recent':1,'registered':55})
        self.assertEqual(second['activity_counts'],first['activity_counts'])
        self.assertEqual(first['total'],57)
        only_old=webdata.fan_destinations(self.path,'10',activity='registered')
        self.assertEqual(only_old['total'],55)
        self.assertEqual(only_old['activity_counts']['current'],0)

    def test_new_room_invalidates_old_presence_but_other_dj_can_be_current(self):
        from spoondev.webdata import favorite_activity
        now=datetime.now(timezone.utc)
        def observe(room,dj,minutes,listeners):
            save_snapshot(self.path,{'room_id':room,'broadcaster':{'id':dj,'name':'host'},
                'listeners':[{'id':uid,'name':'listener','favorite_temperature':20} for uid in listeners],
                'complete':True,'observed_at':(now-timedelta(minutes=minutes)).isoformat()})
        observe('old-room','100',8,['400','401'])
        observe('new-room','100',4,[])
        observe('other-dj','101',5,['401'])
        observe('future-room','101',-2,[])
        result={row['id']:row for row in favorite_activity(self.path,['400','401'])['users']}
        self.assertEqual(result['400']['activity_state'],'recent')
        self.assertEqual(result['401']['activity_state'],'current')

    def test_observation_range_is_latest_session_and_preserves_missing_temperature(self):
        now=datetime.now(timezone.utc)
        def observe(room,minutes,present=True,temperature=20,complete=True):
            save_snapshot(self.path,{'room_id':room,'broadcaster':{'id':'100','name':'host'},
                'listeners':[{'id':'200','name':'listener','favorite_temperature':temperature}] if present else [],
                'complete':complete,'observed_at':(now-timedelta(minutes=minutes)).isoformat()})
        observe('range',50)
        observe('range',10);observe('range',8)
        observe('range',7,present=False)
        observe('range',6);observe('range',5)
        relation=user_details(self.path,'200')['broadcasters'][0]
        self.assertEqual(relation['first_seen_at'],(now-timedelta(minutes=6)).isoformat(timespec='microseconds'))
        self.assertEqual(relation['last_seen_at'],(now-timedelta(minutes=5)).isoformat(timespec='microseconds'))
        self.assertEqual(relation['session_observation_count'],2)
        self.assertNotEqual(relation['first_seen_at'],relation['all_time_first_seen_at'])
        observe('new-range',3,temperature=50)
        observe('new-range',2,temperature=None,complete=False)
        relation=user_details(self.path,'200')['broadcasters'][0]
        self.assertEqual(relation['first_seen_at'],(now-timedelta(minutes=3)).isoformat(timespec='microseconds'))
        self.assertIsNone(relation['favorite_temperature'])
        self.assertFalse(relation['observation_range_complete'])
        self.assertEqual(relation['observed_from_at'],relation['first_seen_at'])
        self.assertEqual(relation['observed_until_at'],relation['last_seen_at'])

    def test_observation_range_can_cross_calendar_midnight_but_long_gap_starts_again(self):
        from unittest.mock import patch
        from spoondev import webdata
        now=datetime(2026,10,11,0,15,tzinfo=timezone.utc)
        for stamp in ('2026-10-10T23:55:00Z','2026-10-11T00:05:00Z'):
            save_snapshot(self.path,{'room_id':'midnight','broadcaster':{'id':'100','name':'host'},
                'listeners':[{'id':'200','name':'listener'}],'complete':True,'observed_at':stamp})
        with patch.object(webdata,'datetime',wraps=datetime) as clock:
            clock.now.return_value=now
            row=user_details(self.path,'200')['broadcasters'][0]
        self.assertEqual(row['first_seen_at'],'2026-10-10T23:55:00.000000+00:00')
        self.assertEqual(row['last_seen_at'],'2026-10-11T00:05:00.000000+00:00')
        save_snapshot(self.path,{'room_id':'midnight','broadcaster':{'id':'100','name':'host'},
            'listeners':[{'id':'200','name':'listener'}],'complete':True,'observed_at':'2026-10-11T00:30:00Z'})
        with patch.object(webdata,'datetime',wraps=datetime) as clock:
            clock.now.return_value=datetime(2026,10,11,0,35,tzinfo=timezone.utc)
            row=user_details(self.path,'200')['broadcasters'][0]
        self.assertEqual(row['first_seen_at'],row['last_seen_at'])

    def test_favorite_previews_are_batched_and_future_monthly_ranking_is_excluded(self):
        from spoondev import profiledb, webdata
        profiledb.initialize(self.path)
        now=datetime.now(timezone.utc)
        month=now.astimezone(__import__('zoneinfo').ZoneInfo('Asia/Tokyo')).strftime('%Y-%m')
        profiledb.save_dj_ranking(self.path,{'id':'100','name':'host'},
            [{'user':{'id':'200','name':'listener'},'temperature':42}],month,True,
            observed_at=(now-timedelta(minutes=5)).isoformat())
        profiledb.save_dj_ranking(self.path,{'id':'100','name':'host'},[],month,True,
            observed_at=(now+timedelta(minutes=5)).isoformat())
        save_snapshot(self.path,{'room_id':'preview','broadcaster':{'id':'100','name':'host'},
            'listeners':[{'id':'200','name':'listener','favorite_temperature':45}],
            'complete':True,'observed_at':(now-timedelta(minutes=2)).isoformat()})
        result=webdata.favorite_activity(self.path,['200','404'])
        self.assertEqual(result['users'][0]['monthly'][0]['temperature'],42)
        self.assertEqual(result['users'][0]['live'][0]['favorite_temperature'],45)
        self.assertEqual(result['users'][1]['monthly'],[])
        self.assertEqual(result['users'][1]['live'],[])

    def test_compact_preview_is_bounded_but_detail_keeps_full_observed_range(self):
        from unittest.mock import patch
        from spoondev import webdata
        now=datetime.now(timezone.utc)
        # A long series should remain a full detail range, while list polling
        # only needs points from the advertised recent observation window.
        for minutes in (60,50,40,30,20,10,2):
            save_snapshot(self.path,{'room_id':'continuous-samples',
                'broadcaster':{'id':'100','name':'host'},
                'listeners':[{'id':'200','name':'listener','favorite_temperature':20}],
                'complete':True,'observed_at':(now-timedelta(minutes=minutes)).isoformat()})
        with patch.object(webdata,'datetime',wraps=datetime) as clock:
            clock.now.return_value=now
            preview=webdata.favorite_activity(self.path,['200'])['users'][0]['live'][0]
            detail=webdata.user_details(self.path,'200')['broadcasters'][0]
        self.assertEqual(preview['observed_from_at'],(now-timedelta(minutes=30)).isoformat(timespec='microseconds'))
        self.assertEqual(detail['observed_from_at'],(now-timedelta(minutes=60)).isoformat(timespec='microseconds'))
        self.assertEqual(preview['session_observation_count'],4)
        self.assertEqual(detail['session_observation_count'],7)
        self.assertIn('直近30分',preview['observation_range_note'])
        self.assertNotIn('observation_window_start_at',detail)

    def test_empty_fan_page_retains_counts_and_no_query_count_fields_leak(self):
        from spoondev import fans, profiledb, webdata
        profiledb.initialize(self.path);fans.initialize(self.path)
        fans.import_followers(self.path,{'owner':{'id':'10','name':'owner'},
            'followers':[{'id':'200','name':'listener'}],'complete':True})
        first=webdata.fan_destinations(self.path,'10',limit=1)
        beyond=webdata.fan_destinations(self.path,'10',limit=1,offset=100)
        self.assertEqual(beyond['fans'],[])
        self.assertEqual(beyond['total'],first['total'])
        self.assertEqual(beyond['category_counts'],first['category_counts'])
        self.assertFalse(any(key.startswith('count_') for key in first['fans'][0]))

if __name__=='__main__': unittest.main()
