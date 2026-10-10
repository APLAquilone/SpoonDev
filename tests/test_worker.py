"""Automatic collection behavior without upstream HTTP or login credentials."""
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from spoondev import accounts, collection_status, db, fans, gifts, profiledb, worker
from spoondev.collector import FetchError
from spoondev.spoon import SpoonRateLimit


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / 'observations.sqlite3'
        self.auth = self.root / 'auth.sqlite3'
        db.initialize(self.database)
        worker.initialize(self.database)
        self.uid = 'a'*32
        with sqlite3.connect(self.auth) as conn:
            conn.execute('CREATE TABLE accounts(id TEXT PRIMARY KEY)')
            conn.execute('INSERT INTO accounts VALUES(?)', (self.uid,))
        self.private = accounts.private(self.auth, self.uid)
        with sqlite3.connect(self.private) as conn:
            conn.execute('UPDATE account_settings SET spoon_id=? WHERE singleton=1', ('10',))

    def snapshot(self, dj='10', listener='77', room='1', complete=True):
        return {'room_id': room, 'broadcaster': {'id': dj, 'name': 'DJ'+dj},
                'listeners': [{'id': listener, 'name': 'User'+listener, 'favorite_temperature': 72}],
                'complete': complete, 'observed_at': worker._iso()}

    def metadata(self, snapshot, state='completed'):
        stamp = worker._iso()
        return {'room_id': snapshot['room_id'], 'broadcaster_id': snapshot['broadcaster']['id'],
                'started_at': stamp, 'finished_at': stamp, 'page_count': 1, 'state': state}

    def jobs(self):
        with sqlite3.connect(self.database) as conn:
            return worker.read_jobs(conn)

    def attempt(self, kind='gifts', uid='10'):
        worker.enqueue(self.database, kind, uid, priority=100)
        job = worker._claim(self.database)
        self.assertEqual((job['kind'], job['dj_id']), (kind, '' if kind=='live' else uid))
        return job

    def test_enqueue_deduplicates_without_resetting_refresh_and_readonly_defaults(self):
        for invalid in (None, '', 'x', False, 0, -1, 1.2):
            with self.assertRaises(ValueError):
                worker.enqueue(self.database, 'gifts', invalid)
        for invalid in (-1, 101, True, 2.5):
            with self.assertRaises(ValueError):
                worker.enqueue(self.database, 'gifts', '10', priority=invalid)
        worker.enqueue(self.database, 'gifts', '0010', priority=10)
        deadline = time.time()+5000
        with sqlite3.connect(self.database) as conn:
            conn.execute("UPDATE worker_jobs SET due_at=?,state='completed'", (deadline,))
        worker.enqueue(self.database, 'gifts', 10, priority=100)
        rows = self.jobs()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['dj_id'], rows[0]['priority'], rows[0]['state']), ('10', 100, 'completed'))
        self.assertAlmostEqual(datetime.fromisoformat(rows[0]['next_due_at']).timestamp(), deadline, places=5)
        self.assertIsNone(worker._claim(self.database))
        old = self.root/'old.sqlite3'
        with sqlite3.connect(old) as conn:
            before = conn.total_changes
            self.assertEqual(worker.read_status(conn)['state'], 'not_started')
            self.assertEqual(worker.read_jobs(conn, ['10']), [])
            self.assertEqual(conn.total_changes, before)

    def test_discovery_offline_binding_and_related_priority_without_private_changes(self):
        for dj, listener in (('10', '77'), ('20', '77'), ('30', '88'), ('50', '123')):
            db.save_snapshot(self.database, self.snapshot(dj, listener, dj))
        with sqlite3.connect(self.private) as conn:
            conn.execute('INSERT INTO favorites VALUES(?,?,?)', ('88', 'Favorite', None))
        fans.import_followers(self.private, {'owner': {'id': '10', 'name': 'DJ10'},
                                            'followers': ['99'], 'complete': True})
        profiledb.initialize(self.database)
        month = datetime.now(timezone.utc).astimezone(worker.ZoneInfo('Asia/Tokyo')).strftime('%Y-%m')
        profiledb.save_dj_ranking(self.database, {'id': '40', 'name': 'DJ40'},
                                  [{'user': {'id': '99', 'name': 'Fan'}, 'temperature': 20}], month, True)
        before = Path(self.private).read_bytes()
        with patch('spoondev.worker.fetch_snapshot') as fetch:
            result = worker._discover(self.database, self.auth)
        fetch.assert_not_called()
        self.assertEqual(before, Path(self.private).read_bytes())
        self.assertEqual(result, {'bound_dj_count': 1, 'target_dj_count': 5})
        ranking = {(row['kind'], row['dj_id']): row['priority'] for row in self.jobs()}
        for kind in ('monthly', 'gifts'):
            self.assertEqual({uid: ranking[(kind, uid)] for uid in ('10','20','30','40','50')},
                             {'10': 100, '20': 80, '30': 50, '40': 50, '50': 10})
        self.assertEqual(ranking[('live', '')], 90)
        # Own profile remains eligible after removing all live history.
        with sqlite3.connect(self.database) as conn:
            conn.execute('DELETE FROM worker_jobs')
            conn.execute('DELETE FROM memberships')
            conn.execute('DELETE FROM snapshots')
        worker._discover(self.database, self.auth)
        self.assertTrue(any(j['kind']=='gifts' and j['dj_id']=='10' for j in self.jobs()))

    def test_deleted_account_private_files_are_ignored(self):
        stale = accounts.private(self.auth, 'b'*32)
        with sqlite3.connect(stale) as conn:
            conn.execute('UPDATE account_settings SET spoon_id=? WHERE singleton=1', ('999',))
        owners, _ = worker._account_inputs(self.auth)
        self.assertEqual(owners, {'10'})

    def test_binding_change_demotes_old_jobs_and_preserves_their_deadline(self):
        worker._discover(self.database, self.auth)
        deadline = time.time()+3600
        with sqlite3.connect(self.database) as conn:
            conn.execute("UPDATE worker_jobs SET due_at=?,state='completed' WHERE dj_id='10'", (deadline,))
        with sqlite3.connect(self.private) as conn:
            conn.execute('UPDATE account_settings SET spoon_id=? WHERE singleton=1', ('20',))
        worker._discover(self.database, self.auth)
        for row in self.jobs():
            if row['dj_id']=='10':
                self.assertEqual((row['priority'],row['state']), (10,'completed'))
                self.assertAlmostEqual(datetime.fromisoformat(row['next_due_at']).timestamp(), deadline, places=5)
            elif row['dj_id']=='20':self.assertEqual(row['priority'],100)
        self.assertEqual(len([row for row in self.jobs() if row['dj_id']=='20']),2)

    def test_deleted_account_demotes_old_targets_but_keeps_other_bound_dj_priority(self):
        other_uid='b'*32
        with sqlite3.connect(self.auth) as conn:conn.execute('INSERT INTO accounts VALUES(?)',(other_uid,))
        other_private=accounts.private(self.auth,other_uid)
        with sqlite3.connect(other_private) as conn:
            conn.execute('UPDATE account_settings SET spoon_id=? WHERE singleton=1',('20',))
        worker._discover(self.database,self.auth)
        deadline=time.time()+3600
        with sqlite3.connect(self.database) as conn:
            conn.execute("UPDATE worker_jobs SET due_at=? WHERE dj_id='10'",(deadline,))
        with sqlite3.connect(self.auth) as conn:conn.execute('DELETE FROM accounts WHERE id=?',(self.uid,))
        worker._discover(self.database,self.auth)
        for row in self.jobs():
            if row['dj_id']=='10':
                self.assertEqual(row['priority'],10)
                self.assertAlmostEqual(datetime.fromisoformat(row['next_due_at']).timestamp(),deadline,places=5)
            elif row['dj_id']=='20':self.assertEqual(row['priority'],100)

    def test_legacy_saved_ids_do_not_block_bound_dj_collection(self):
        with sqlite3.connect(self.private) as conn:
            conn.execute('INSERT INTO favorites VALUES(?,?,?)', ('old-id', 'Legacy adapter user', None))
            conn.execute('INSERT INTO favorites VALUES(?,?,?)', ('88', 'Public Spoon user', None))
        owners, saved = worker._account_inputs(self.auth)
        self.assertEqual(owners, {'10'})
        self.assertEqual(saved, {'88'})
        worker._discover(self.database, self.auth)
        self.assertEqual({(j['kind'],j['dj_id']) for j in self.jobs()},
                         {('live',''), ('monthly','10'), ('gifts','10')})

    def test_future_observations_never_expand_discovery_targets(self):
        future = worker._iso(time.time()+86400)
        for dj, listener in (('10','77'), ('20','77')):
            snapshot = self.snapshot(dj, listener, dj)
            snapshot['observed_at'] = future
            db.save_snapshot(self.database, snapshot)
        with sqlite3.connect(self.private) as conn:
            conn.execute('INSERT INTO favorites VALUES(?,?,?)', ('88','Saved',None))
        profiledb.initialize(self.database)
        month = datetime.now(timezone.utc).astimezone(worker.ZoneInfo('Asia/Tokyo')).strftime('%Y-%m')
        profiledb.save_dj_ranking(self.database, {'id':'30','name':'Future DJ'},
                                  [{'user':{'id':'88','name':'Saved'},'temperature':10}], month, True, future)
        worker._discover(self.database, self.auth)
        self.assertEqual({j['dj_id'] for j in self.jobs()}, {'','10'})

    def test_aging_claim_deduplicates_and_eventually_prioritizes_old_targets(self):
        worker.enqueue(self.database, 'gifts', '10', priority=100)
        worker.enqueue(self.database, 'gifts', '20', priority=10)
        with sqlite3.connect(self.database) as conn:
            conn.execute('UPDATE worker_jobs SET due_at=? WHERE dj_id=?', (time.time()-24*3600, '20'))
        self.assertEqual(worker._claim(self.database)['dj_id'], '20')
        self.assertEqual(worker._claim(self.database)['dj_id'], '10')
        self.assertIsNone(worker._claim(self.database))

    def test_global_429_cooldown_persists_and_blocks_other_jobs(self):
        stop = threading.Event()
        budget = worker._Budget(self.database, stop, 2)
        with patch('spoondev.worker.fetch_snapshot', return_value=FetchError('url', 'HTTP429', 429, 120)) as fetch:
            first = budget.request('https://jp-api.spooncast.net/users/10/top_fan/')
            second = budget.request('https://jp-gw.spooncast.net/other')
            restarted = worker._Budget(self.database, stop, 2)
            third = restarted.request('https://jp-api.spooncast.net/other')
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual((first.status, second.status, third.status), (429, 429, 429))
        self.assertGreater(second.retry_after_seconds, 110)

    def test_http_budget_bounds_simultaneous_requests_across_collectors(self):
        stop = threading.Event(); release = threading.Event(); two_active = threading.Event()
        guard = threading.Lock(); active = 0; peak = 0
        def fetch(url, **kwargs):
            nonlocal active, peak
            with guard:
                active += 1; peak = max(peak, active)
                if active == 2:two_active.set()
            try:
                if not release.wait(5):raise RuntimeError('test request was not released')
                return {'results': []}
            finally:
                with guard:active -= 1
        budget = worker._Budget(self.database, stop, 2)
        with patch('spoondev.worker.fetch_snapshot', side_effect=fetch), patch('spoondev.worker.REQUEST_SPACING', 0):
            with ThreadPoolExecutor(max_workers=5) as pool:
                requests = [pool.submit(budget.request, 'https://jp-api.spooncast.net/'+str(i)) for i in range(5)]
                try:
                    self.assertTrue(two_active.wait(5))
                    self.assertEqual(peak, 2)
                finally:release.set()
                self.assertEqual([future.result(timeout=5) for future in requests], [{'results': []}]*5)
        self.assertEqual(peak, 2)

    def test_os_lock_blocks_duplicate_without_pid_or_cooldown_changes(self):
        with sqlite3.connect(self.database) as conn:
            conn.execute("UPDATE worker_state SET pid=999999,cooldown_until=? WHERE singleton=1", (time.time()+300,))
            before = conn.execute('SELECT * FROM worker_state').fetchone()
        with worker._lease(self.database):
            with self.assertRaises(worker.WorkerAlreadyRunning):
                worker.run(self.database, self.auth)
        with sqlite3.connect(self.database) as conn:
            self.assertEqual(conn.execute('SELECT * FROM worker_state').fetchone(), before)

    def test_failed_gifts_remain_unknown_and_shared_retry_applies(self):
        job = self.attempt()
        result = {'complete': False, 'snapshot_id': None, 'errors': ['HTTP429'], 'retry_after': 120}
        with patch('spoondev.worker.collect_gifts', return_value=result) as collect:
            summary = worker._attempt(self.database, job, threading.Event(), 4, 3600)
        collect.assert_called_once_with(self.database, '10', max_pages=0, stopped_event=collect.call_args.kwargs['stopped_event'])
        self.assertIsNone(summary['snapshot_id'])
        row = self.jobs()[0]
        self.assertEqual(row['state'], 'cooldown')
        self.assertEqual(row['last_attempt']['state'], 'failed')
        with sqlite3.connect(self.database) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM worker_snapshot_links').fetchone()[0], 0)
            self.assertGreater(conn.execute('SELECT cooldown_until FROM worker_state').fetchone()[0], time.time()+110)

    def test_live_callback_saved_once_and_links_real_snapshot(self):
        job = self.attempt('live', None)
        snapshot = self.snapshot()
        def collect(**kwargs):
            kwargs['room_callback'](snapshot, self.metadata(snapshot))
            return [snapshot], []
        with patch('spoondev.worker.collect_spoon', side_effect=collect):
            summary = worker._attempt(self.database, job, threading.Event(), 4, 300)
        self.assertTrue(summary['complete'])
        with sqlite3.connect(self.database) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM worker_snapshot_links').fetchone()[0], 1)
            metadata = conn.execute('SELECT state,page_count,collection_run_id,snapshot_id FROM observation_attempts').fetchone()
            self.assertEqual(metadata[:2], ('completed', 1))
            self.assertIsNotNone(metadata[2])
            self.assertEqual(metadata[3], summary['snapshot_ids'][0])

    def test_live_429_keeps_positive_room_and_failed_room_attempt(self):
        job = self.attempt('live', None)
        snapshot = self.snapshot(complete=False)
        def collect(**kwargs):
            callback = kwargs['room_callback']
            callback(snapshot, self.metadata(snapshot, 'partial'))
            failed = self.snapshot(room='2')
            callback(None, self.metadata(failed, 'failed'))
            raise SpoonRateLimit(120)
        with patch('spoondev.worker.collect_spoon', side_effect=collect):
            summary = worker._attempt(self.database, job, threading.Event(), 4, 300)
        self.assertFalse(summary['complete'])
        self.assertEqual(summary['room_count'], 1)
        self.assertEqual(self.jobs()[0]['last_attempt']['state'], 'partial')
        with sqlite3.connect(self.database) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM memberships').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM worker_snapshot_links').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT room_id,state FROM observation_attempts ORDER BY id').fetchall(),
                             [('1', 'partial'), ('2', 'failed')])
            self.assertGreater(conn.execute('SELECT cooldown_until FROM worker_state').fetchone()[0], time.time()+110)

    def test_recover_interrupted_job_marks_failed_and_preserves_cooldown(self):
        job = self.attempt()
        run_id = collection_status.begin(self.database, 'gifts', '10', 3600)
        cooldown = time.time()+300
        with sqlite3.connect(self.database) as conn:
            conn.execute('INSERT INTO worker_attempts(kind,dj_id,started_at,state,run_id) VALUES(?,?,?,?,?)',
                         ('gifts', '10', worker._iso(), 'running', run_id))
            conn.execute('UPDATE worker_state SET cooldown_until=?', (cooldown,))
        worker._recover(self.database)
        with sqlite3.connect(self.database) as conn:
            self.assertEqual(conn.execute('SELECT state FROM worker_attempts').fetchone()[0], 'failed')
            self.assertEqual(conn.execute('SELECT state FROM collection_runs').fetchone()[0], 'failed')
            self.assertEqual(conn.execute('SELECT state FROM worker_jobs').fetchone()[0], 'queued')
            self.assertEqual(conn.execute('SELECT cooldown_until FROM worker_state').fetchone()[0], cooldown)

    def test_run_collects_offline_owner_exactly_once_and_stops_cleanly(self):
        stop = threading.Event()
        observed = []
        guard = threading.Lock()
        def mark(kind):
            with guard:
                observed.append(kind)
                if len(observed)==3:stop.set()
        def monthly(database, **kwargs):
            self.assertEqual(kwargs['dj_ids'], ['10'])
            mark('monthly')
            return {'complete': True, 'dj_count': 1, 'errors': [], 'month': '2026-10'}
        def gift(database, uid, **kwargs):
            self.assertEqual(uid, '10')
            sid = gifts._save(database, uid, {'77': ('77','User77',None,12)}, True, worker._iso())
            mark('gifts')
            return {'complete': True, 'snapshot_id': sid, 'errors': [], 'source_period': 'unspecified'}
        def live(**kwargs):
            snapshot = self.snapshot()
            kwargs['room_callback'](snapshot, self.metadata(snapshot))
            mark('live')
            return [snapshot], []
        with patch('spoondev.worker.collect_monthly', side_effect=monthly), \
                patch('spoondev.worker.collect_gifts', side_effect=gift), \
                patch('spoondev.worker.collect_spoon', side_effect=live), \
                patch('spoondev.worker.fetch_snapshot') as fetch:
            result = worker.run(self.database, self.auth, stop_event=stop, concurrency=3)
        fetch.assert_not_called()
        self.assertEqual(result, {'state': 'stopped', 'attempt_count': 3})
        self.assertCountEqual(observed, ['gifts', 'monthly', 'live'])
        with sqlite3.connect(self.database) as conn:
            state = worker.read_status(conn)
            self.assertEqual(state['state'], 'stopped')
            self.assertFalse(state['worker_running'])
            self.assertIsNotNone(state['stopped_at'])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM worker_attempts').fetchone()[0], 3)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM worker_snapshot_links').fetchone()[0], 2)
        worker._discover(self.database, self.auth)
        self.assertIsNone(worker._claim(self.database))

    def test_real_collectors_use_shared_budget_and_preserve_period_semantics(self):
        stop = threading.Event(); seen = set(); guard = threading.Lock()
        profiledb.initialize(self.database)
        profiledb.cache_users(self.database, [{'id': '10', 'name': 'Owned DJ'}])
        live_url = 'https://jp-api.spooncast.net/lives/'
        gift_url = 'https://jp-api.spooncast.net/users/10/top_fan/'
        monthly_url = 'https://jp-gw.spooncast.net/favorite-temperatures/djs/10/rankings?rankType=MONTHLY'
        responses = {live_url: {'status_code': 200, 'results': [], 'next': None},
                     gift_url: {'results': [{'user': {'id': 77, 'nickname': 'Listener'}, 'total_spoon': 15}], 'next': None},
                     monthly_url: {'results': [{'user': {'id': 77, 'nickname': 'Listener'}, 'favoriteTemperature': 35}], 'next': None}}
        def fetch(url, **kwargs):
            with guard:
                seen.add(url)
                if seen == set(responses):stop.set()
            return responses[url]
        from spoondev import spoon, monthly, directory
        originals = [module.fetch_snapshot for module in (spoon, monthly, gifts, directory)]
        with patch('spoondev.worker.fetch_snapshot', side_effect=fetch) as network, \
                patch('spoondev.worker.REQUEST_SPACING', 0), patch('spoondev.gifts._last_request', None):
            result = worker.run(self.database, self.auth, stop_event=stop, concurrency=3)
        self.assertEqual(result['attempt_count'], 3)
        self.assertEqual(network.call_count, 3)
        self.assertEqual([module.fetch_snapshot for module in (spoon, monthly, gifts, directory)], originals)
        with sqlite3.connect(self.database) as conn:
            ranking = gifts.read_ranking(conn, '10')
            self.assertEqual(ranking['source_period'], 'unspecified')
            self.assertEqual(ranking['rows'][0]['total_spoon'], 15)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0], 0)
        self.assertEqual(profiledb.latest_profile(self.database, '77')['appearances'][0]['temperature'], 35)

    def test_invalid_startup_never_creates_observation_database(self):
        dest = self.root/'missing'/ 'new.sqlite3'
        with self.assertRaises(ValueError):
            worker.run(dest, self.auth, live_interval=float('nan'))
        self.assertFalse(dest.exists())
        empty = self.root/'empty-auth.sqlite3'
        with sqlite3.connect(empty) as conn:conn.execute('CREATE TABLE accounts(id TEXT)')
        with self.assertRaises(ValueError):worker.run(dest, empty)
        self.assertFalse(dest.exists())

    def test_unhandled_task_failure_does_not_stop_other_collection_jobs(self):
        stop=threading.Event();original=worker._attempt
        def attempt(database,job,*args):
            if job['kind']=='gifts':raise RuntimeError('Unexpected task crash')
            return original(database,job,*args)
        def live(**kwargs):
            stop.set()
            return [],[]
        with patch('spoondev.worker._attempt',side_effect=attempt), \
                patch('spoondev.worker.collect_monthly',return_value={'complete':True,'dj_count':1,'errors':[]}), \
                patch('spoondev.worker.collect_spoon',side_effect=live):
            result=worker.run(self.database,self.auth,stop_event=stop,concurrency=1)
        self.assertEqual(result['attempt_count'],3)
        jobs={(j['kind'],j['dj_id']):j for j in self.jobs()}
        self.assertEqual(jobs['gifts','10']['state'],'failed')
        self.assertEqual(jobs['monthly','10']['last_attempt']['state'],'completed')
        self.assertEqual(jobs['live','']['last_attempt']['state'],'completed')
        self.assertGreater(datetime.fromisoformat(jobs['gifts','10']['next_due_at']).timestamp(),time.time()+50)
        with sqlite3.connect(self.database) as conn:
            self.assertIn('Unexpected task crash',worker.read_status(conn)['last_error'])

    def test_scheduler_failure_cancels_fetching_before_executor_wait(self):
        stop=threading.Event();started=threading.Event();cancelled=[];checks=[]
        def attempt(*args):
            started.set()
            cancelled.append(stop.wait(1))
            return {'complete':False,'errors':['Collection stopped']}
        def cooldown():
            checks.append(True)
            if len(checks)==1:return 0
            self.assertTrue(started.wait(1))
            raise sqlite3.OperationalError('Database became unavailable')
        with patch('spoondev.worker._attempt',side_effect=attempt), \
                patch('spoondev.worker._Budget.cooldown',side_effect=cooldown):
            with self.assertRaisesRegex(sqlite3.OperationalError,'Database became unavailable'):
                worker.run(self.database,self.auth,stop_event=stop,concurrency=1)
        self.assertEqual(cancelled,[True])
        self.assertTrue(stop.is_set())
        with sqlite3.connect(self.database) as conn:
            self.assertEqual(worker.read_status(conn)['state'],'stopped')


if __name__ == '__main__':
    unittest.main()
