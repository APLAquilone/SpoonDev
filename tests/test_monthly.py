from datetime import datetime,timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from spoondev.db import initialize,save_snapshot
from spoondev import profiledb
from spoondev.monthly import collect_monthly
from spoondev.collector import FetchError

class MonthlyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'db.sqlite'; initialize(self.path)
        for user_id in ('10','20'):
            save_snapshot(self.path,{'room_id':'room'+user_id,'broadcaster':{'id':user_id,'name':'DJ'+user_id},
              'listeners':[{'id':'99','name':'live-listener'}],'complete':True,'observed_at':'2026-10-01T00:00:00Z'})
        self.page={'results':[{'user':{'id':30,'nickname':'listener','tag':'tag'},'favoriteTemperature':46}], 'next':None}

    def test_inverse_direction_no_live_snapshot_invention(self):
        with patch('spoondev.monthly.fetch_snapshot',return_value=self.page),patch('spoondev.monthly.time.sleep'):
            summary=collect_monthly(self.path)
        result=profiledb.latest_profile(self.path,'30')
        self.assertEqual([r['user_id'] for r in result['appearances']],['10','20'])
        self.assertTrue(summary['complete']); self.assertEqual(summary['coverage'],'known_broadcasters')
        self.assertEqual(profiledb.latest_profile(self.path,'10')['appearances'],[])
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0],2)

    def test_month_uses_japan_date(self):
        with patch('spoondev.monthly.datetime') as clock,patch('spoondev.monthly.fetch_snapshot',return_value=self.page),patch('spoondev.monthly.time.sleep'):
            clock.now.return_value=datetime(2026,9,30,16,0,tzinfo=timezone.utc)
            summary=collect_monthly(self.path)
        self.assertEqual(summary['month'],'2026-10')

    def test_partial_failure_and_cap(self):
        def fetch(url):
            return self.page if '/10/' in url else FetchError(url,'HTTP 500',500)
        with patch('spoondev.monthly.fetch_snapshot',side_effect=fetch),patch('spoondev.monthly.time.sleep'):
            summary=collect_monthly(self.path)
        self.assertFalse(summary['complete']); self.assertEqual(len(summary['errors']),1)
        self.assertFalse(profiledb.latest_profile(self.path,'30')['complete'])
        with patch('spoondev.monthly.fetch_snapshot',return_value=self.page):
            summary=collect_monthly(self.path,max_djs=1)
        self.assertTrue(summary['capped']); self.assertFalse(summary['complete'])

    def test_all_failed_preserves_previous(self):
        profiledb.initialize(self.path)
        profiledb.save_profile(self.path,{'id':'30','name':'listener'},[{'id':'10','name':'DJ10','temperature':2}],'2026-09',True)
        before=self.path.read_bytes()
        with patch('spoondev.monthly.fetch_snapshot',return_value=FetchError('url','HTTP 500',500)),patch('spoondev.monthly.time.sleep'):
            summary=collect_monthly(self.path)
        self.assertEqual(summary['user_count'],0); self.assertFalse(summary['complete'])
        self.assertEqual(before,self.path.read_bytes())

    def test_pagination_same_origin_and_zero_temperature(self):
        page2={'results':[{'user':{'id':31,'nickname':'zero'},'favoriteTemperature':0},
                          {'user':{'id':32,'nickname':'null'},'favoriteTemperature':None}],'next':None}
        first=dict(self.page,next='https://jp-gw.spooncast.net/favorite-temperatures/djs/10/rankings?cursor=next&rankType=ALL_TIME')
        with patch('spoondev.monthly.fetch_snapshot',side_effect=[first,page2]) as fetch,patch('spoondev.monthly.time.sleep'):
            summary=collect_monthly(self.path,max_djs=1)
        self.assertEqual(summary['user_count'],3)
        self.assertIn('rankType=MONTHLY',fetch.call_args_list[1].args[0])
        self.assertEqual(profiledb.latest_profile(self.path,'31')['appearances'][0]['temperature'],0)
        self.assertIsNone(profiledb.latest_profile(self.path,'32')['appearances'][0]['temperature'])

    def test_fast_dj_is_visible_while_other_fetch_still_runs(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        fast_saved=threading.Event()
        slow_released=threading.Event()
        original=profiledb.save_dj_ranking
        def save(*args,**kwargs):
            result=original(*args,**kwargs)
            if args[1]['id']=='20': fast_saved.set()
            return result
        def fetch(url):
            if '/10/' in url:
                if not slow_released.wait(5): raise ValueError('test slow fetch timeout')
            return self.page
        with patch('spoondev.monthly.fetch_snapshot',side_effect=fetch),patch('spoondev.monthly.time.sleep'),patch('spoondev.monthly.profiledb.save_dj_ranking',side_effect=save):
            with ThreadPoolExecutor(max_workers=1) as executor:
                future=executor.submit(collect_monthly,self.path)
                try:
                    self.assertTrue(fast_saved.wait(5))
                    self.assertFalse(future.done())
                    self.assertEqual(profiledb.latest_profile(self.path,'30')['appearances'][0]['user_id'],'20')
                finally:
                    slow_released.set()
                self.assertTrue(future.result(timeout=5)['complete'])

    def test_reject_cross_origin_pointer(self):
        page=dict(self.page,next='https://example.com/collect?cursor=x')
        with patch('spoondev.monthly.fetch_snapshot',return_value=page) as fetch:
            summary=collect_monthly(self.path,max_djs=1)
        self.assertEqual(fetch.call_count,1); self.assertFalse(summary['complete'])
        self.assertIn('origin',summary['errors'][0])

    def test_default_follows_beyond_old_page_cap(self):
        from urllib.parse import urlsplit,parse_qs
        def fetch(url):
            page=int(parse_qs(urlsplit(url).query).get('cursor',['0'])[0])
            return {'results':[{'user':{'id':1000+page,'nickname':'listener'},'favoriteTemperature':1}],
                    'next':str(page+1) if page<104 else None}
        with patch('spoondev.monthly.fetch_snapshot',side_effect=fetch) as mock,patch('spoondev.monthly.time.sleep'):
            summary=collect_monthly(self.path,max_djs=0)
        self.assertEqual(mock.call_count,210)
        self.assertTrue(summary['complete'])
        self.assertEqual(summary['user_count'],105)

    def test_optional_cap_and_cycle_still_stop(self):
        page=dict(self.page,next='repeat')
        with patch('spoondev.monthly.fetch_snapshot',return_value=page),patch('spoondev.monthly.time.sleep'):
            capped=collect_monthly(self.path,max_djs=1,max_pages=1)
            cyclic=collect_monthly(self.path,max_djs=1,max_pages=0)
        self.assertIn('page cap',capped['errors'][0])
        self.assertIn('pagination loop',cyclic['errors'][0])

if __name__=='__main__': unittest.main()
