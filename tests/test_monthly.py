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

    def test_month_boundary_response_is_not_saved_under_previous_period(self):
        before=self.path.read_bytes()
        old=datetime(2026,10,31,14,59,59,tzinfo=timezone.utc)
        new=datetime(2026,10,31,15,0,0,tzinfo=timezone.utc)
        with patch('spoondev.monthly.datetime') as clock,patch('spoondev.monthly.fetch_snapshot',return_value=self.page):
            clock.now.side_effect=[old,old,new]
            summary=collect_monthly(self.path,max_djs=1)
        self.assertFalse(summary['complete'])
        self.assertEqual(summary['dj_count'],0)
        self.assertIn('period changed',summary['errors'][0])
        self.assertEqual(before,self.path.read_bytes())

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
        import threading
        from urllib.parse import urlsplit,parse_qs
        requests={'10':[],'20':[]}
        lock=threading.Lock()
        def fetch(url):
            parsed=urlsplit(url);query=parse_qs(parsed.query)
            dj_id=parsed.path.split('/')[-2]
            page=int(query.get('cursor',['0'])[0])
            # Mock.call_count increments are not synchronized across threads.
            # Record the actual pages per DJ instead of that shared counter.
            with lock:
                requests[dj_id].append((page,query.get('rankType')))
            return {'results':[{'user':{'id':1000+page,'nickname':'listener'},'favoriteTemperature':1}],
                    'next':str(page+1) if page<104 else None}
        with patch('spoondev.monthly.fetch_snapshot',new=fetch),patch('spoondev.monthly.time.sleep'):
            summary=collect_monthly(self.path,max_djs=0)
        for dj_id,pages in requests.items():
            with self.subTest(dj_id=dj_id):
                self.assertEqual(pages,[(page,['MONTHLY']) for page in range(105)])
        self.assertTrue(summary['complete'])
        self.assertEqual(summary['errors'],[])
        self.assertEqual(summary['dj_count'],2)
        self.assertEqual(summary['user_count'],105)
        with sqlite3.connect(self.path) as conn:
            rows=conn.execute('''SELECT s.dj_id,s.complete,COUNT(l.listener_id)
              FROM monthly_dj_snapshots s JOIN monthly_dj_listeners l ON l.snapshot_id=s.id
              GROUP BY s.id ORDER BY s.dj_id''').fetchall()
        self.assertEqual(rows,[('10',1,105),('20',1,105)])

    def test_optional_cap_and_cycle_still_stop(self):
        page=dict(self.page,next='repeat')
        with patch('spoondev.monthly.fetch_snapshot',return_value=page),patch('spoondev.monthly.time.sleep'):
            capped=collect_monthly(self.path,max_djs=1,max_pages=1)
            cyclic=collect_monthly(self.path,max_djs=1,max_pages=0)
        self.assertIn('page cap',capped['errors'][0])
        self.assertIn('pagination loop',cyclic['errors'][0])

    def test_explicit_cached_profile_never_seen_live_is_targeted_only(self):
        profiledb.initialize(self.path)
        profiledb.cache_users(self.path,[{'id':'123','name':'profile-only DJ','tag':'newdj'}])
        with patch('spoondev.monthly.fetch_snapshot',return_value=self.page) as fetch,patch('spoondev.monthly.directory.resolve_user') as resolve:
            result=collect_monthly(self.path,dj_ids=['123'])
        resolve.assert_not_called()
        self.assertEqual(fetch.call_args.args[0],'https://jp-gw.spooncast.net/favorite-temperatures/djs/123/rankings?rankType=MONTHLY')
        self.assertTrue(result['complete'])
        self.assertEqual(result['coverage'],'selected_broadcasters')
        self.assertEqual((result['known_dj_count'],result['selected_dj_count'],result['dj_count']),(2,1,1))
        self.assertEqual(profiledb.latest_profile(self.path,'30')['appearances'][0]['user_id'],'123')
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0],2)

    def test_explicit_observed_listener_profile_and_latest_cached_name(self):
        profiledb.initialize(self.path)
        profiledb.cache_users(self.path,[{'id':'99','name':'new profile name','tag':'new-tag'}],'2026-10-02T00:00:00Z')
        with patch('spoondev.monthly.fetch_snapshot',return_value=self.page),patch('spoondev.monthly.directory.resolve_user') as resolve:
            result=collect_monthly(self.path,dj_ids=['99'])
        resolve.assert_not_called()
        self.assertEqual(result['dj_count'],1)
        self.assertEqual(profiledb.profile_user(self.path,'99')['name'],'new profile name')
        self.assertEqual(profiledb.latest_profile(self.path,'30')['appearances'][0]['tag'],'new-tag')

    def test_explicit_unknown_profile_resolved_and_duplicate_ids_normalized(self):
        resolved={'id':'456','name':'resolved DJ','tag':'resolved-tag','last_seen_at':None}
        with patch('spoondev.monthly.fetch_snapshot',return_value=self.page) as fetch,patch('spoondev.monthly.directory.resolve_user',return_value=resolved) as resolve:
            result=collect_monthly(self.path,dj_ids=['00456',456,'456'])
        resolve.assert_called_once_with('456')
        self.assertEqual(fetch.call_count,1)
        self.assertEqual(result['selected_dj_count'],1)
        self.assertEqual(result['requested_dj_count'],1)
        self.assertEqual(profiledb.latest_profile(self.path,'30')['appearances'][0]['name'],'resolved DJ')

    def test_explicit_lookup_failure_reports_target_and_does_not_create_data(self):
        before=self.path.read_bytes()
        with patch('spoondev.monthly.fetch_snapshot') as fetch,patch('spoondev.monthly.directory.resolve_user',side_effect=ValueError('profile unavailable')):
            result=collect_monthly(self.path,dj_ids=['456'])
        fetch.assert_not_called()
        self.assertFalse(result['complete'])
        self.assertEqual(result['selected_dj_count'],1)
        self.assertEqual(result['dj_count'],0)
        self.assertIn('DJ 456: profile unavailable',result['errors'])
        self.assertEqual(before,self.path.read_bytes())

    def test_explicit_cap_only_resolves_selected_targets_and_error_is_not_complete(self):
        with patch('spoondev.monthly.fetch_snapshot',return_value=self.page),patch('spoondev.monthly.directory.resolve_user') as resolve:
            capped=collect_monthly(self.path,max_djs=1,dj_ids=['10','999'])
        resolve.assert_not_called()
        self.assertEqual((capped['selected_dj_count'],capped['requested_dj_count']),(1,2))
        self.assertTrue(capped['capped'])
        self.assertFalse(capped['complete'])
        with patch('spoondev.monthly.fetch_snapshot',return_value=self.page),patch('spoondev.monthly.directory.resolve_user',side_effect=ValueError('not found')),patch('spoondev.monthly.time.sleep'):
            failed=collect_monthly(self.path,max_djs=0,dj_ids=['10','999'])
        self.assertEqual((failed['selected_dj_count'],failed['dj_count']),(2,1))
        self.assertFalse(failed['complete'])
        self.assertFalse(failed['capped'])

    def test_explicit_invalid_and_empty_selection_never_falls_back_to_known(self):
        for selection in ('10',[True],['0'],['../10'],[1.5]):
            with self.subTest(selection=selection),self.assertRaises(ValueError):
                collect_monthly(self.path,dj_ids=selection)
        with patch('spoondev.monthly.fetch_snapshot') as fetch,patch('spoondev.monthly.directory.resolve_user') as resolve:
            result=collect_monthly(self.path,dj_ids=[])
        fetch.assert_not_called()
        resolve.assert_not_called()
        self.assertEqual(result['selected_dj_count'],0)
        self.assertEqual(result['coverage'],'selected_broadcasters')
        self.assertFalse(result['complete'])

    def test_invalid_falsey_pagination_is_not_reported_complete(self):
        for pointer in (False,0,[],{}):
            with self.subTest(pointer=pointer),patch('spoondev.monthly.fetch_snapshot',return_value=dict(self.page,next=pointer)):
                result=collect_monthly(self.path,max_djs=1)
                self.assertFalse(result['complete'])
                self.assertTrue(result['errors'])

if __name__=='__main__': unittest.main()
