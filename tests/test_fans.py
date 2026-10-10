from datetime import datetime,timezone,timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest
from spoondev import fans,profiledb,webdata
from spoondev.db import initialize,save_snapshot

class FanTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.db=Path(self.temp.name)/'db.sqlite';initialize(self.db);profiledb.initialize(self.db);fans.initialize(self.db)
        self.owner={'id':'10','name':'own','tag':'owner'}

    def put(self,followers,complete=False):
        return fans.import_followers(self.db,{'owner':self.owner,'followers':followers,'complete':complete})

    def test_sort_all_fans_before_pagination(self):
        self.put([{'id':str(i+100),'name':f'Name {60-i:02d}'} for i in range(60)],True)
        first=webdata.fan_destinations(self.db,'10',sort='name')
        last=webdata.fan_destinations(self.db,'10',offset=50,sort='name')
        names=[u['name'] for u in first['fans']+last['fans']]
        self.assertEqual(names,sorted(names));self.assertEqual(len(names),60)
        desc=webdata.fan_destinations(self.db,'10',sort='name_desc')['fans']
        self.assertEqual(desc[0]['name'],'Name 60')
        ids=webdata.fan_destinations(self.db,'10',sort='id')['fans']
        self.assertEqual([u['id'] for u in ids],[str(i) for i in range(100,150)])
        with self.assertRaises(ValueError):webdata.fan_destinations(self.db,'10',sort='name; DROP TABLE users')

    def test_partial_merges_full_replaces_and_empty_full_clears(self):
        self.put([{'id':'20','name':'same'},{'id':'21','name':'same'}],True)
        self.assertEqual(self.put(['22'])['fan_count'],3)
        self.assertEqual(self.put(['21','21'],True)['fan_count'],1)
        rows=webdata.fan_destinations(self.db,'10')['fans']
        self.assertEqual([u['id'] for u in rows],['21'])
        self.assertEqual(rows[0]['name'],'same')
        self.assertEqual(self.put([],True)['fan_count'],0)

    def test_invalid_input_preserves_list_and_no_credentials_saved(self):
        self.put(['20'],True);before=self.db.read_bytes()
        for values in (['bad'],[True],[0],[{'id':'20','name':'a'},{'id':'20','name':'b'}]):
            with self.assertRaises(ValueError):self.put(values,True)
            self.assertEqual(self.db.read_bytes(),before)
        self.put([{'id':'21','name':'fan','token':'not-a-real-token','email':'not-stored'}])
        self.assertNotIn(b'not-a-real-token',self.db.read_bytes())
        with sqlite3.connect(self.db) as conn:
            self.assertIsNone(conn.execute("SELECT id FROM profile_users WHERE id='20'").fetchone())

    def test_destinations_current_month_latest_snapshot_and_recent_live(self):
        self.put(['20','21','22'],True)
        now=datetime.now(timezone.utc);month=now.astimezone(webdata.ZoneInfo('Asia/Tokyo')).strftime('%Y-%m')
        old=(now-timedelta(hours=2)).isoformat();recent=(now-timedelta(minutes=3)).isoformat()
        for uid,stamp in [('20',recent),('21',old)]:
            save_snapshot(self.db,{'room_id':'r','broadcaster':{'id':'30','name':'DJ'},
                'listeners':[{'id':uid,'name':'listener'}],'complete':False,'observed_at':stamp})
        dj={'id':'40','name':'MonthlyDJ'}
        profiledb.save_dj_ranking(self.db,dj,[{'user':{'id':'20','name':'listener'},'temperature':35}],month,True,old)
        profiledb.save_dj_ranking(self.db,dj,[],month,True,recent)
        profiledb.save_dj_ranking(self.db,{'id':'41','name':'latest'},
            [{'user':{'id':'20','name':'listener'},'temperature':0}],month,False,recent)
        # Previous months must not show as current-month appearances.
        profiledb.save_dj_ranking(self.db,{'id':'42','name':'old-month'},
            [{'user':{'id':'20','name':'listener'},'temperature':99}],'2020-01',True)
        before=self.db.read_bytes();data=webdata.fan_destinations(self.db,'10');users={u['id']:u for u in data['fans']}
        self.assertEqual(users['20']['live'][0]['user_id'],'30')
        self.assertEqual(users['21']['live'],[])
        self.assertEqual([d['user_id'] for d in users['20']['monthly']],['41'])
        self.assertEqual(users['20']['monthly'][0]['temperature'],0)
        self.assertFalse(users['20']['monthly'][0]['complete'])
        self.assertEqual(users['22']['monthly'],[])
        self.assertEqual(self.db.read_bytes(),before)
        self.assertEqual(webdata.fan_destinations(self.db,'999')['owner'],None)

    def test_activity_sort_keeps_categories_first_and_nulls_last(self):
        self.put(['20','21','22'],True)
        now=datetime.now(timezone.utc)
        for minute,listener in [(90,'20'),(60,'21'),(5,'20')]:
            save_snapshot(self.db,{'room_id':str(minute),'broadcaster':{'id':'10','name':'own'},
                'listeners':[{'id':listener,'name':listener}],'complete':True,
                'observed_at':(now-timedelta(minutes=minute)).isoformat()})
        self.assertEqual([u['id'] for u in webdata.fan_destinations(self.db,'10',sort='activity')['fans']],['20','21','22'])
        # Three-category layout is always prioritized; the chosen sort applies
        # within each category, including older recorded fans before unknowns.
        oldest=webdata.fan_destinations(self.db,'10',sort='oldest')['fans']
        self.assertEqual([u['id'] for u in oldest],['20','21','22'])
        self.assertEqual([u['activity_state'] for u in oldest],['recent','registered','registered'])

    def test_old_live_temperature_does_not_manufacture_monthly_ranking(self):
        self.put([{'id':'315888158','name':'つくちゃん'}],True)
        now=datetime.now(timezone.utc);month=now.astimezone(webdata.ZoneInfo('Asia/Tokyo')).strftime('%Y-%m')
        save_snapshot(self.db,{'room_id':'past','broadcaster':{'id':'317169445','name':'DJ'},
            'listeners':[{'id':'315888158','name':'つくちゃん','favorite_temperature':92}],
            'complete':True,'observed_at':(now-timedelta(hours=6)).isoformat()})
        profiledb.save_dj_ranking(self.db,{'id':'317169445','name':'DJ'},[],month,True)
        entry=webdata.fan_destinations(self.db,'10')['fans'][0]
        detail=webdata.user_details(self.db,'315888158')
        self.assertEqual(entry['live'],[]);self.assertEqual(entry['monthly'],[])
        self.assertEqual(detail['broadcasters'][0]['favorite_temperature'],92)
        self.assertEqual(detail['appearances'],[])
        self.assertEqual(detail['profile_coverage'],'known_broadcasters')
        self.assertTrue(detail['profile_ranking_complete'])
        # Monthly temperature appears only when separately indexed, in both views.
        profiledb.save_dj_ranking(self.db,{'id':'317169445','name':'DJ'},
            [{'user':{'id':'315888158','name':'つくちゃん'},'temperature':35}],month,False)
        self.assertEqual(webdata.fan_destinations(self.db,'10')['fans'][0]['monthly'][0]['temperature'],35)
        detail=webdata.user_details(self.db,'315888158')
        self.assertEqual(detail['appearances'][0]['temperature'],35)
        self.assertFalse(detail['profile_ranking_complete'])

    def test_recent_fans_are_sorted_before_pagination(self):
        self.put([str(i) for i in range(100,155)],True)
        now=datetime.now(timezone.utc)
        save_snapshot(self.db,{'room_id':'r','broadcaster':{'id':'10','name':'own'},
            'listeners':[{'id':'154','name':'recent'}],'complete':True,
            'observed_at':(now-timedelta(minutes=5)).isoformat()})
        result=webdata.fan_destinations(self.db,'10')
        self.assertEqual(result['fans'][0]['id'],'154')
        self.assertTrue(result['fans'][0]['recent'])
        self.assertFalse(result['fans'][1]['recent'])

    def test_pagination_and_max_five_destinations(self):
        self.put([str(i) for i in range(100,155)],True)
        first=webdata.fan_destinations(self.db,'10');last=webdata.fan_destinations(self.db,'10',offset=50)
        self.assertEqual(len(first['fans']),50);self.assertTrue(first['has_more'])
        self.assertEqual(len(last['fans']),5);self.assertFalse(last['has_more'])
        self.assertEqual(first['total'],55)
        month=datetime.now(webdata.ZoneInfo('Asia/Tokyo')).strftime('%Y-%m')
        for i in range(7):
            profiledb.save_dj_ranking(self.db,{'id':str(200+i),'name':'DJ'},
                [{'user':{'id':'100','name':'fan'},'temperature':i}],month,True)
        rows=webdata.fan_destinations(self.db,'10')['fans'][0]['monthly']
        self.assertEqual([row['temperature'] for row in rows],[6,5,4,3,2])
