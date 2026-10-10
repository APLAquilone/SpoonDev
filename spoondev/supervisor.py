"""Restart an owned automatic collector without restarting the web or tunnel."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import threading
import time

from . import worker


def service_loaded(root, database):
    """Recognize only the service registered for this exact checkout and DB."""
    root=Path(root).resolve();database=Path(database).resolve()
    try:
        config=json.loads((root/'data/collector-service.json').read_text())
        label=config.get('label')
        if (not isinstance(label,str) or not re.fullmatch(r'net\.spooninsights\.collector\.[0-9a-f]{12}',label)
                or config.get('root')!=str(root) or config.get('db')!=str(database)):
            return False
        target=f'gui/{os.getuid()}/{label}'
        result=subprocess.run(['launchctl','print',target],stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL,text=True,timeout=5)
        if result.returncode!=0:return False
        # A successful SIGTERM leaves the job loaded but inactive because its
        # launchd KeepAlive policy only restarts unsuccessful exits. Merely
        # seeing a loaded job must not suppress all collection on web restart.
        if (re.search(r'^\s*state\s*=\s*running\s*$',result.stdout,re.MULTILINE)
                and re.search(r'^\s*pid\s*=\s*[1-9][0-9]*\s*$',result.stdout,re.MULTILINE)):
            return True
        # No -k: a worker that starts between inspection and this call is never
        # killed. A failed request lets the launcher supervisor use its lease.
        return subprocess.run(['launchctl','kickstart',target],stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,timeout=5).returncode==0
    except (OSError,ValueError,TypeError,subprocess.TimeoutExpired):
        return False


def _mark_restart(database, delay, message):
    # An existing worker owns its own status. Waiting supervisors do not alter
    # that worker's PID, heartbeat, cooldown, or state.
    try:
        # SQLite's own context manager commits/rolls back, but never closes
        # the connection. Restart loops must release their file descriptors
        # immediately, including on error, rather than waiting for cyclic GC.
        with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=rw',uri=True,timeout=30)) as conn, conn:
            conn.execute("""UPDATE worker_state SET state='restarting',restart_at=?,last_error=?
              WHERE singleton=1 AND pid=? AND state!='running'""",
              (time.time()+delay,message,os.getpid()))
    except sqlite3.Error:
        pass


def run(database, auth_database, *, stop_event=None, concurrency=4,
        live_interval=300, ranking_interval=3600, initial_delay=5, max_delay=60):
    """Retry crashes with backoff; leave another DB lease owner untouched.

    A signal stops both the current worker and the supervisor. Internal worker
    shutdown (for example persistent SQLite trouble) gets a fresh stop event
    on retry. HTTP 429 cooldown stays in the database across restarts.
    """
    stop=stop_event if stop_event is not None else threading.Event()
    failures=0;duplicate_reported=False
    while not stop.is_set():
        child_stop=threading.Event();finished=threading.Event()
        def forward_stop():
            while not finished.wait(.1):
                if stop.is_set():child_stop.set();return
        thread=threading.Thread(target=forward_stop,daemon=True);thread.start()
        started=time.monotonic()
        try:
            worker.run(database,auth_database,stop_event=child_stop,concurrency=concurrency,
                       live_interval=live_interval,ranking_interval=ranking_interval)
            message='Collector ended unexpectedly; restarting.'
            duplicate_reported=False
        except worker.WorkerAlreadyRunning:
            message=None
            if not duplicate_reported:
                print(json.dumps({'state':'already_running',
                    'message':'Existing automatic collector kept running; waiting for its lease.'}),flush=True)
                duplicate_reported=True
        except Exception as exc:
            message=f'Collector failed ({type(exc).__name__}): {exc}'
            duplicate_reported=False
        finally:
            finished.set();thread.join(timeout=1)
        if stop.is_set():break
        if message is None:
            delay=max_delay
        else:
            # A worker that ran healthily for a while starts again at the small
            # delay. Repeated immediate failures back off instead of spinning.
            failures=1 if time.monotonic()-started>=60 else failures+1
            delay=min(max_delay,initial_delay*2**min(failures-1,6))
            _mark_restart(database,delay,message)
            print(json.dumps({'state':'restarting','retry_after':delay,'error':message},ensure_ascii=False),flush=True)
        stop.wait(delay)
    return {'state':'stopped'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',default='data/spoondev.sqlite3')
    parser.add_argument('--auth-db',default='data/accounts.sqlite3')
    parser.add_argument('--concurrency',type=int,default=4)
    parser.add_argument('--live-interval',type=float,default=300)
    parser.add_argument('--ranking-interval',type=float,default=3600)
    parser.add_argument('--skip-loaded-service',action='store_true')
    args=parser.parse_args(argv)
    if args.skip_loaded_service and service_loaded(Path.cwd(),args.db):
        print('Native collection service is already loaded; launcher keeps it running.',flush=True)
        return 0
    # Validate persistent inputs once. A permanent setup mistake should fail
    # visibly instead of producing an endless stream of restart attempts.
    worker._account_ids(args.auth_db)
    if not 1<=args.concurrency<=16 or any(not 30<=v<float('inf') for v in (args.live_interval,args.ranking_interval)):
        parser.error('Concurrency must be 1..16 and intervals finite, at least 30 seconds')
    stop=threading.Event()
    previous={sig:signal.getsignal(sig) for sig in (signal.SIGINT,signal.SIGTERM)}
    try:
        for sig in previous:signal.signal(sig,lambda *_:stop.set())
        run(args.db,args.auth_db,stop_event=stop,concurrency=args.concurrency,
            live_interval=args.live_interval,ranking_interval=args.ranking_interval)
        return 0
    finally:
        for sig,handler in previous.items():signal.signal(sig,handler)

