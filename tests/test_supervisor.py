from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from spoondev import supervisor,worker


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        # The service installer stores canonical paths, including on macOS.
        self.root=Path(self.temp.name).resolve()
        self.db=self.root/'data/spoondev.sqlite3';worker.initialize(self.db)
        self.stop=threading.Event()

    def test_crash_retries_and_preserves_server_cooldown(self):
        cooldown=time.time()+300
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE worker_state SET pid=?,state='stopped',cooldown_until=?",(os.getpid(),cooldown))
        calls=[]
        def run(*args,**kwargs):
            calls.append(kwargs['stop_event'])
            self.assertFalse(kwargs['stop_event'].is_set())
            if len(calls)==1:raise RuntimeError('A task crashed')
            with sqlite3.connect(self.db) as conn:
                status=worker.read_status(conn)
                self.assertEqual(status['state'],'restarting')
                self.assertIsNotNone(status['restart_at'])
                self.assertIn('A task crashed',status['last_error'])
                self.assertEqual(conn.execute('SELECT cooldown_until FROM worker_state').fetchone()[0],cooldown)
            self.stop.set()
        with patch('spoondev.supervisor.worker.run',side_effect=run),redirect_stdout(StringIO()):
            result=supervisor.run(self.db,'auth',stop_event=self.stop,initial_delay=.001,max_delay=.001)
        self.assertEqual(result,{'state':'stopped'})
        self.assertEqual(len(calls),2)
        self.assertIsNot(calls[0],calls[1])

    def test_internal_worker_stop_gets_fresh_event_on_retry(self):
        calls=[]
        def run(*args,**kwargs):
            event=kwargs['stop_event'];calls.append(event)
            self.assertFalse(event.is_set())
            if len(calls)==1:event.set()
            else:self.stop.set()
            return {'state':'stopped'}
        with patch('spoondev.supervisor.worker.run',side_effect=run),redirect_stdout(StringIO()):
            supervisor.run(self.db,'auth',stop_event=self.stop,initial_delay=.001,max_delay=.001)
        self.assertEqual(len(calls),2)
        self.assertIsNot(calls[0],calls[1])

    def test_duplicate_does_not_rewrite_owner_heartbeat_or_cooldown(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE worker_state SET pid=123,owner_token='owner',state='running',heartbeat_at=?,cooldown_until=?",
                         (worker._iso(),time.time()+300))
            before=conn.execute('SELECT * FROM worker_state').fetchone()
        def duplicate(*args,**kwargs):
            self.stop.set();raise worker.WorkerAlreadyRunning('Owned elsewhere')
        with patch('spoondev.supervisor.worker.run',side_effect=duplicate),redirect_stdout(StringIO()):
            supervisor.run(self.db,'auth',stop_event=self.stop)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT * FROM worker_state').fetchone(),before)

    def test_native_service_is_scoped_to_exact_checkout_and_database(self):
        marker=self.root/'data/collector-service.json'
        config={'label':'net.spooninsights.collector.'+'a'*12,'root':str(self.root),'db':str(self.db)}
        marker.write_text(json.dumps(config))
        with patch('spoondev.supervisor.subprocess.run') as run:
            run.return_value=subprocess.CompletedProcess([],0,'state = running\npid = 123\n')
            self.assertTrue(supervisor.service_loaded(self.root,self.db))
            self.assertEqual(run.call_args.args[0][-1],f'gui/{os.getuid()}/{config["label"]}')
            self.assertEqual(run.call_count,1)
            run.reset_mock()
            config['db']=str(self.root/'other.sqlite3');marker.write_text(json.dumps(config))
            self.assertFalse(supervisor.service_loaded(self.root,self.db));run.assert_not_called()
            config['db']=str(self.db);config['label']='malicious; command';marker.write_text(json.dumps(config))
            self.assertFalse(supervisor.service_loaded(self.root,self.db));run.assert_not_called()

    def test_loaded_inactive_native_service_is_restarted_without_killing_a_worker(self):
        config={'label':'net.spooninsights.collector.'+'a'*12,'root':str(self.root),'db':str(self.db)}
        (self.root/'data/collector-service.json').write_text(json.dumps(config))
        target=f'gui/{os.getuid()}/{config["label"]}'
        for successful in (True,False):
            with self.subTest(successful=successful),patch('spoondev.supervisor.subprocess.run') as run:
                run.side_effect=[subprocess.CompletedProcess([],0,'state = not running\nlast exit code = 0\n'),
                                 subprocess.CompletedProcess([],0 if successful else 1)]
                self.assertEqual(supervisor.service_loaded(self.root,self.db),successful)
                self.assertEqual([call.args[0] for call in run.call_args_list],
                                 [['launchctl','print',target],['launchctl','kickstart',target]])
                self.assertNotIn('-k',run.call_args.args[0])

    def test_unloaded_native_service_never_starts_another_job(self):
        config={'label':'net.spooninsights.collector.'+'a'*12,'root':str(self.root),'db':str(self.db)}
        (self.root/'data/collector-service.json').write_text(json.dumps(config))
        with patch('spoondev.supervisor.subprocess.run') as run:
            run.return_value=subprocess.CompletedProcess([],1,'')
            self.assertFalse(supervisor.service_loaded(self.root,self.db))
            self.assertEqual(run.call_count,1)

    def test_canonical_service_marker_is_recognized_through_a_checkout_symlink(self):
        alias=self.root/'checkout alias';alias.symlink_to(self.root,target_is_directory=True)
        config={'label':'net.spooninsights.collector.'+'a'*12,'root':str(self.root),'db':str(self.db)}
        (self.root/'data/collector-service.json').write_text(json.dumps(config))
        with patch('spoondev.supervisor.subprocess.run') as run:
            run.return_value=subprocess.CompletedProcess([],0,'state = running\npid = 123\n')
            self.assertTrue(supervisor.service_loaded(alias,alias/'data/spoondev.sqlite3'))
            self.assertEqual(run.call_count,1)
            self.assertEqual(run.call_args.args[0],
                             ['launchctl','print',f'gui/{os.getuid()}/{config["label"]}'])
            run.reset_mock()
            self.assertFalse(supervisor.service_loaded(alias,alias/'data/other.sqlite3'))
            run.assert_not_called()

    def test_existing_worker_schema_gets_additive_restart_column(self):
        legacy=self.root/'legacy.sqlite3'
        with sqlite3.connect(legacy) as conn:
            conn.execute('''CREATE TABLE worker_state(singleton INTEGER PRIMARY KEY,owner_token TEXT,pid INTEGER,
              started_at TEXT,heartbeat_at TEXT,stopped_at TEXT,state TEXT DEFAULT 'not_started',
              cooldown_until REAL DEFAULT 0,next_request_at REAL DEFAULT 0,window_started_at REAL DEFAULT 0,
              window_requests INTEGER DEFAULT 0,last_error TEXT)''')
            conn.execute('INSERT INTO worker_state(singleton,cooldown_until) VALUES(1,123)')
        worker.initialize(legacy)
        with sqlite3.connect(legacy) as conn:
            self.assertIn('restart_at',{row[1] for row in conn.execute('PRAGMA table_info(worker_state)')})
            self.assertEqual(conn.execute('SELECT cooldown_until FROM worker_state').fetchone()[0],123)


if __name__=='__main__':unittest.main()
