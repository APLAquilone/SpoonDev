from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from spoondev import gifts
from spoondev.collector import FetchError
from spoondev.db import initialize, save_snapshot


def page(uid=30, amount=46, next_value=None):
    return {'status_code': 200,
            'results': [{'user': {'id': uid, 'nickname': 'listener', 'tag': 'tag'},
                         'total_spoon': amount}], 'next': next_value}


class GiftsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'db.sqlite'
        initialize(self.path)
        self.sleep = patch('spoondev.gifts.time.sleep')
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def read(self, dj_id='10'):
        with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro', uri=True) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA query_only=ON')
            return gifts.read_ranking(conn, dj_id)

    def collect(self, response=None, **kwargs):
        with patch('spoondev.gifts.fetch_snapshot', return_value=response or page()):
            return gifts.collect_gifts(self.path, '10', **kwargs)

    def test_specific_dj_amount_not_monthly_or_global_and_no_live_invention(self):
        save_snapshot(self.path, {'room_id': 'room', 'broadcaster': {'id': '10', 'name': 'DJ'},
            'listeners': [{'id': '30', 'name': 'listener'}], 'complete': True,
            'observed_at': '2026-10-01T00:00:00Z'})
        summary = self.collect()
        ranking = self.read()
        self.assertTrue(summary['complete'])
        self.assertEqual(ranking['source_period'], 'unspecified')
        self.assertEqual(ranking['coverage'], 'single_dj_public_ranking')
        self.assertEqual(ranking['rows'][0], {'user_id': '30', 'name': 'listener', 'tag': 'tag', 'total_spoon': 46})
        self.assertIsNone(self.read('20'))
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0], 1)

    def test_pagination_zero_unknown_sorted_and_successful_empty_replaces(self):
        second = {'results': [page(31, 0)['results'][0], page(32, None)['results'][0]], 'next': None}
        with patch('spoondev.gifts.fetch_snapshot', side_effect=[page(next_value='https://jp-api.spooncast.net/users/10/top_fan/?cursor=p%3D2'), second]) as fetch:
            result = gifts.collect_gifts(self.path, '10')
        self.assertEqual(fetch.call_args_list[1].args[0], 'https://jp-api.spooncast.net/users/10/top_fan/?cursor=p%3D2')
        self.assertTrue(result['complete'])
        self.assertEqual([r['total_spoon'] for r in self.read()['rows']], [46, 0, None])
        self.collect({'results': [], 'next': None})
        self.assertEqual(self.read()['rows'], [])
        self.assertTrue(self.read()['complete'])

    def test_first_page_failure_preserves_prior_bytes_and_429_stops(self):
        self.collect()
        before = self.path.read_bytes()
        with patch('spoondev.gifts.fetch_snapshot', return_value=FetchError('url', 'HTTP 429', 429, 120)) as fetch:
            result = gifts.collect_gifts(self.path, '10')
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(result['complete'])
        self.assertIsNone(result['snapshot_id'])
        self.assertEqual(result['retry_after'], 120)

    def test_partial_failure_is_explicitly_incomplete(self):
        responses = iter([page(next_value='two'), FetchError('url', 'HTTP 500', 500)])
        with patch('spoondev.gifts.fetch_snapshot', side_effect=lambda url: next(responses)):
            result = gifts.collect_gifts(self.path, '10')
        self.assertFalse(result['complete'])
        self.assertEqual(result['page_count'], 1)
        self.assertFalse(self.read()['complete'])
        self.assertEqual(len(self.read()['rows']), 1)

    def test_cross_origin_other_dj_and_extra_query_are_not_followed(self):
        pointers = ['https://example.com/?cursor=x',
                    'https://jp-api.spooncast.net/users/20/top_fan/?cursor=x',
                    'https://jp-api.spooncast.net/users/10/top_fan/?cursor=x&rankType=MONTHLY',
                    '//example.com/?cursor=x']
        for pointer in pointers:
            with self.subTest(pointer=pointer), patch('spoondev.gifts.fetch_snapshot', return_value=page(next_value=pointer)) as fetch:
                result = gifts.collect_gifts(self.path, '10')
                self.assertEqual(fetch.call_count, 1)
                self.assertFalse(result['complete'])
                self.assertTrue(result['errors'])

    def test_caps_cycles_stopped_event_and_invalid_limits(self):
        for max_pages, needle, expected in [(1, 'page cap', 1), (0, 'pagination loop', 2)]:
            with patch('spoondev.gifts.fetch_snapshot', return_value=page(next_value='repeat')) as fetch:
                result = gifts.collect_gifts(self.path, '10', max_pages=max_pages)
                self.assertEqual(fetch.call_count, expected)
                self.assertIn(needle, result['errors'][0])
        stopped = threading.Event()
        stopped.set()
        with patch('spoondev.gifts.fetch_snapshot') as fetch:
            result = gifts.collect_gifts(self.path, '10', stopped_event=stopped)
            fetch.assert_not_called()
            self.assertIsNone(result['snapshot_id'])
        for kwargs in [{'dj_id': '../10'}, {'dj_id': True}, {'dj_id': '10', 'max_pages': -1}, {'dj_id': '10', 'max_pages': True}]:
            with self.assertRaises(ValueError):
                gifts.collect_gifts(self.path, **kwargs)

    def test_malformed_amounts_or_conflicting_users_are_not_saved(self):
        for amount in [True, -1, 2.5, float('inf'), '20', 2**63]:
            with self.subTest(amount=amount):
                result = self.collect(page(amount=amount))
                self.assertIsNone(result['snapshot_id'])
        duplicated = {'results': [page(amount=1)['results'][0], page(amount=2)['results'][0]]}
        result = self.collect(duplicated)
        self.assertIsNone(result['snapshot_id'])
        self.assertIn('conflicting duplicate', result['errors'][0])

    def test_reader_does_not_initialize_old_database(self):
        before = self.path.read_bytes()
        self.assertIsNone(self.read())
        self.assertEqual(before, self.path.read_bytes())

    def test_invalid_falsey_pagination_is_partial_not_completed(self):
        for pointer in (False,0,[],{}):
            with self.subTest(pointer=pointer):
                result=self.collect(page(next_value=pointer))
                self.assertFalse(result['complete'])
                self.assertTrue(result['errors'])

    def test_default_follows_actual_end_without_fixed_page_cap(self):
        from urllib.parse import parse_qs, urlsplit
        def fetch(url):
            index = int(parse_qs(urlsplit(url).query).get('cursor', ['0'])[0])
            return page(uid=1000+index, amount=index,
                        next_value=str(index+1) if index < 104 else None)
        with patch('spoondev.gifts.fetch_snapshot', side_effect=fetch) as mock:
            result = gifts.collect_gifts(self.path, '10')
        self.assertEqual(mock.call_count, 105)
        self.assertTrue(result['complete'])
        self.assertEqual(result['user_count'], 105)


if __name__ == '__main__':
    unittest.main()
