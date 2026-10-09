import unittest
from unittest.mock import patch
from spoondev import onair
from spoondev.collector import FetchError

class OnAirTests(unittest.TestCase):
    def setUp(self):
        for key,value in [('_cached',None),('_expires',0),('_error',None)]:
            patcher=patch.object(onair,key,value);patcher.start();self.addCleanup(patcher.stop)
        self.page={'status_code':200,'results':[{'id':1,'author':{'id':10,'nickname':'host'},
                    'listeners':[{'id':99,'nickname':'listener'}]}],'next':None}

    def test_live_hosts_only_pagination_and_shared_cache(self):
        first=dict(self.page,next=onair.BASE+'/lives/?cursor=next')
        second={'status_code':200,'results':[{'id':2,'author':{'id':20,'nickname':'host2'}}],'next':None}
        with patch('spoondev.onair.fetch_snapshot',side_effect=[first,second]) as fetch,patch('spoondev.onair.time.sleep'):
            result=onair.current_public_lives()
            self.assertEqual({u['user_id'] for u in result['users']},{'10','20'})
            self.assertEqual(onair.current_public_lives(),result)
            self.assertEqual(fetch.call_count,2)

    def test_end_and_failure_do_not_retain_old_live_status(self):
        with patch('spoondev.onair.fetch_snapshot',return_value=self.page):
            self.assertTrue(onair.current_public_lives()['users'])
        onair._expires=0
        with patch('spoondev.onair.fetch_snapshot',return_value={'status_code':200,'results':[],'next':None}):
            self.assertEqual(onair.current_public_lives()['users'],[])
        onair._expires=0
        with patch('spoondev.onair.fetch_snapshot',return_value=FetchError('url','HTTP429',429,120)) as fetch:
            with self.assertRaises(onair.LiveStatusError):onair.current_public_lives()
            with self.assertRaises(onair.LiveStatusError):onair.current_public_lives()
            self.assertEqual(fetch.call_count,1)
            self.assertIsNone(onair._cached)
            self.assertGreater(onair._expires-onair.time.monotonic(),119)

    def test_partial_discovery_and_unsafe_pagination_are_not_published(self):
        for next_url in ['https://other.example/lives/',onair.BASE+'/lives/']:
            onair._expires=0
            with patch('spoondev.onair.fetch_snapshot',return_value=dict(self.page,next=next_url)) as fetch:
                with self.assertRaises(onair.LiveStatusError):onair.current_public_lives()
                self.assertEqual(fetch.call_count,1)
                self.assertIsNone(onair._cached)
