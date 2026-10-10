"""Persistent public-data collection queue for a dedicated CLI process.

Web handlers only enqueue work. This module runs outside the HTTP server and
does not access Spoon login credentials. Queue completion refers to the public
endpoint's available pages, never all Spoon users or all visits.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo

from .collector import FetchError, fetch_snapshot
from .db import initialize as initialize_observations, record_room_attempt, save_snapshot
from .fans import numeric_id
from .gifts import collect_gifts
from .monthly import collect_monthly
from .spoon import collect_spoon, SpoonRateLimit
from . import collection_status

REQUEST_SPACING = 0.4
REQUESTS_PER_MINUTE = 60
DISCOVERY_INTERVAL = 60
HEARTBEAT_INTERVAL = 5
HEARTBEAT_FRESH_SECONDS = 30

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS worker_jobs (
 kind TEXT NOT NULL CHECK(kind IN ('live','monthly','gifts')),dj_id TEXT NOT NULL,
 priority INTEGER NOT NULL,created_at REAL NOT NULL,requested_at REAL NOT NULL,
 due_at REAL NOT NULL,state TEXT NOT NULL,last_attempt_id INTEGER,
 failure_count INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(kind,dj_id));
CREATE INDEX IF NOT EXISTS worker_jobs_due ON worker_jobs(state,due_at,priority);
CREATE TABLE IF NOT EXISTS worker_attempts (
 id INTEGER PRIMARY KEY,kind TEXT NOT NULL,dj_id TEXT NOT NULL,
 started_at TEXT NOT NULL,finished_at TEXT,state TEXT NOT NULL,
 summary TEXT NOT NULL DEFAULT '{}',run_id INTEGER);
CREATE INDEX IF NOT EXISTS worker_attempts_target ON worker_attempts(kind,dj_id,id);
CREATE TABLE IF NOT EXISTS worker_snapshot_links (
 kind TEXT NOT NULL,snapshot_id INTEGER NOT NULL,attempt_id INTEGER NOT NULL,
 PRIMARY KEY(kind,snapshot_id));
CREATE TABLE IF NOT EXISTS worker_state (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),owner_token TEXT,pid INTEGER,
 started_at TEXT,heartbeat_at TEXT,stopped_at TEXT,state TEXT NOT NULL DEFAULT 'not_started',
 cooldown_until REAL NOT NULL DEFAULT 0,next_request_at REAL NOT NULL DEFAULT 0,
 window_started_at REAL NOT NULL DEFAULT 0,window_requests INTEGER NOT NULL DEFAULT 0,
 last_error TEXT,restart_at REAL);
INSERT OR IGNORE INTO worker_state(singleton) VALUES(1);
'''


class WorkerAlreadyRunning(ValueError):
    """An OS lock already owns this observation database's worker."""


def _iso(seconds=None):
    stamp=datetime.fromtimestamp(seconds,timezone.utc) if seconds is not None else datetime.now(timezone.utc)
    return stamp.isoformat(timespec='microseconds')


def initialize(database):
    """Create additive queue/status tables. Does not start a process or fetch data."""
    path=Path(database)
    path.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(path,timeout=30) as conn:
        conn.executescript(_SCHEMA)
        # Serialize this additive migration with other startup/enqueue calls.
        conn.execute('BEGIN IMMEDIATE')
        if 'restart_at' not in {row[1] for row in conn.execute('PRAGMA table_info(worker_state)')}:
            conn.execute('ALTER TABLE worker_state ADD COLUMN restart_at REAL')


def _table(conn,name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone() is not None


@contextmanager
def _readonly(path):
    conn=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
    conn.execute('PRAGMA query_only=ON')
    try:yield conn
    finally:conn.close()


def _target(kind,dj_id):
    if kind not in ('live','monthly','gifts'):raise ValueError('Unknown collection kind')
    if kind=='live':
        if dj_id not in (None,''):raise ValueError('Live discovery uses the public directory, not a DJ selector')
        return ''
    return numeric_id(dj_id)


def _enqueue(conn,kind,dj_id,priority,now):
    conn.execute('''INSERT INTO worker_jobs(kind,dj_id,priority,created_at,requested_at,due_at,state)
      VALUES(?,?,?,?,?,?,'queued') ON CONFLICT(kind,dj_id) DO UPDATE SET
      priority=MAX(worker_jobs.priority,excluded.priority),requested_at=excluded.requested_at''',
      (kind,dj_id,priority,now,now,now))


def enqueue(database,kind,dj_id=None,priority=50):
    """Deduplicate a public target; repeated registration never resets its due time.

    Higher priority runs first. 100 is reserved by convention for bound DJs.
    No network request is made, so HTTP handlers can safely call this function.
    """
    uid=_target(kind,dj_id)
    if type(priority) is not int or not 0<=priority<=100:raise ValueError('priority must be 0..100')
    initialize(database)
    with sqlite3.connect(database,timeout=30) as conn:
        _enqueue(conn,kind,uid,priority,time.time())


def read_status(conn):
    """Return observed worker heartbeat; a fresh heartbeat is not an uptime guarantee."""
    result={'state':'not_started','worker_running':False,'heartbeat_at':None,
            'started_at':None,'stopped_at':None,'cooldown_until':None,'pid':None,
            'restart_at':None,'last_error':None}
    if not _table(conn,'worker_state'):return result
    columns={r[1] for r in conn.execute('PRAGMA table_info(worker_state)')}
    restart='restart_at' if 'restart_at' in columns else 'NULL'
    row=conn.execute(f'''SELECT state,pid,started_at,heartbeat_at,stopped_at,cooldown_until,
      {restart},last_error FROM worker_state WHERE singleton=1''').fetchone()
    if row is None:return result
    state,pid,started,heartbeat,stopped,cooldown,restart_at,last_error=row
    recent=False
    if heartbeat and state=='running' and not stopped:
        try:
            age=time.time()-datetime.fromisoformat(heartbeat).timestamp()
            recent=0<=age<=HEARTBEAT_FRESH_SECONDS
        except (ValueError,TypeError):pass
    result.update(state=('cooldown' if recent and cooldown>time.time() else
                         'running' if recent else 'unconfirmed' if state=='running' else state),
                  worker_running=recent,heartbeat_at=heartbeat,started_at=started,
                  stopped_at=stopped,pid=pid,
                  cooldown_until=_iso(cooldown) if cooldown>time.time() else None,
                  restart_at=_iso(restart_at) if restart_at else None,last_error=last_error)
    return result


def read_jobs(conn,dj_ids=None):
    """Read jobs, optionally filtered to the caller's bound DJ(s). No DB writes."""
    if dj_ids is not None:
        if not isinstance(dj_ids,(list,tuple)):raise ValueError('dj_ids requires a list')
        dj_ids=list(dict.fromkeys(numeric_id(uid) for uid in dj_ids))
        if not dj_ids:return []
    if not _table(conn,'worker_jobs'):return []
    where=' WHERE j.dj_id IN ('+','.join('?' for _ in dj_ids)+')' if dj_ids is not None else ''
    rows=conn.execute('''SELECT j.kind,j.dj_id,j.priority,j.state,j.due_at,
      a.state,a.started_at,a.finished_at,a.summary
      FROM worker_jobs j LEFT JOIN worker_attempts a ON a.id=j.last_attempt_id'''+where+
      ' ORDER BY j.priority DESC,j.due_at,j.kind,j.dj_id',dj_ids or []).fetchall()
    return [{'kind':r[0],'dj_id':r[1],'priority':r[2],'state':r[3],'next_due_at':_iso(r[4]),
             'last_attempt':None if r[5] is None else
             {'state':r[5],'started_at':r[6],'finished_at':r[7],'summary':json.loads(r[8])}}
            for r in rows]


@contextmanager
def _lease(database):
    """Keep a stable lock file; PID is metadata, never the authority for ownership."""
    lock_path=Path(str(Path(database).resolve())+'.worker.lock')
    fd=os.open(lock_path,os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:raise WorkerAlreadyRunning('A collection worker already owns this database.') from exc
        yield secrets.token_hex(16)
    finally:
        os.close(fd)


def _account_inputs(auth_database):
    """Read current accounts only; retained files for deleted accounts are ignored."""
    with _readonly(auth_database) as conn:
        ids=[r[0] for r in conn.execute('SELECT id FROM accounts')]
    if not ids:raise ValueError('Create an account before starting automatic collection')
    owners=set(); saved=set()
    def add_numeric(target,rows):
        for raw in rows:
            try:target.add(numeric_id(raw))
            except ValueError:continue # Legacy adapter IDs are not Spoon IDs.
    for uid in ids:
        if not isinstance(uid,str) or not re.fullmatch('[0-9a-f]{32}',uid):continue
        path=Path(auth_database).resolve().parent/'account-data'/uid/'private.sqlite3'
        if not path.is_file():continue
        with _readonly(path) as conn:
            if _table(conn,'account_settings'):
                row=conn.execute('SELECT spoon_id FROM account_settings WHERE singleton=1').fetchone()
                if row and row[0]:add_numeric(owners,[row[0]])
            if _table(conn,'favorites'):
                add_numeric(saved,(r[0] for r in conn.execute('SELECT id FROM favorites')))
            if _table(conn,'registered_fans'):
                add_numeric(saved,(r[0] for r in conn.execute('SELECT user_id FROM registered_fans')))
    return owners,saved


def _appearances(conn,users,now):
    result=set(); users=list(users)
    cutoff=_iso(now-30*86400);asof=_iso(now)
    month=datetime.fromtimestamp(now,timezone.utc).astimezone(ZoneInfo('Asia/Tokyo')).strftime('%Y-%m')
    for offset in range(0,len(users),400):
        batch=users[offset:offset+400]; placeholders=','.join('?' for _ in batch)
        rows=conn.execute(f'''SELECT DISTINCT s.broadcaster_id FROM memberships m
          JOIN snapshots s ON s.id=m.snapshot_id WHERE m.listener_id IN ({placeholders})
          AND s.observed_at>=? AND s.observed_at<=?''',[*batch,cutoff,asof]).fetchall()
        result.update(r[0] for r in rows)
        if _table(conn,'monthly_dj_snapshots'):
            rows=conn.execute(f'''SELECT DISTINCT s.dj_id FROM monthly_dj_listeners l
              JOIN monthly_dj_snapshots s ON s.id=l.snapshot_id
              WHERE l.listener_id IN ({placeholders}) AND s.month=? AND s.observed_at<=? AND s.id=(
                SELECT other.id FROM monthly_dj_snapshots other WHERE other.dj_id=s.dj_id
                AND other.month=s.month AND other.observed_at<=? ORDER BY other.observed_at DESC,other.id DESC LIMIT 1)''',
                [*batch,month,asof,asof]).fetchall()
            result.update(r[0] for r in rows)
    return result


def _discover(database,auth_database):
    owners,saved=_account_inputs(auth_database)
    now=time.time(); recent=set()
    with _readonly(database) as conn:
        for owner in owners:
            recent.update(r[0] for r in conn.execute('''SELECT DISTINCT m.listener_id FROM snapshots s
              JOIN memberships m ON m.snapshot_id=s.id WHERE s.broadcaster_id=? AND s.observed_at>=? AND s.observed_at<=?''',
              (owner,_iso(now-7*86400),_iso(now))))
        recent_djs=_appearances(conn,recent,now)
        saved_djs=_appearances(conn,saved,now)
        other_djs={r[0] for r in conn.execute('SELECT DISTINCT broadcaster_id FROM snapshots WHERE observed_at<=?',(_iso(now),))}
    targets={}
    for ids,priority in ((other_djs,10),(saved_djs,50),(recent_djs,80),(owners,100)):
        for raw in ids:
            try:uid=numeric_id(raw)
            except ValueError:continue
            targets[uid]=max(priority,targets.get(uid,0))
    with sqlite3.connect(database,timeout=30) as conn:
        # Public targets may remain useful after an account is deleted or its
        # binding changes. Recompute their current importance, preserving the
        # saved deadline and in-flight state instead of keeping old own-DJ 100.
        conn.execute("UPDATE worker_jobs SET priority=10 WHERE kind IN ('monthly','gifts')")
        _enqueue(conn,'live','',90,now)
        for uid,priority in targets.items():
            for kind in ('monthly','gifts'):_enqueue(conn,kind,uid,priority,now)
    return {'bound_dj_count':len(owners),'target_dj_count':len(targets)}


class _Budget:
    """One bounded request budget shared by all collectors in this worker process."""
    def __init__(self,database,stop_event,concurrency):
        self.database=database;self.stop=stop_event
        self.slots=threading.BoundedSemaphore(concurrency)

    def cooldown(self):
        with sqlite3.connect(self.database,timeout=30) as conn:
            return conn.execute('SELECT cooldown_until FROM worker_state WHERE singleton=1').fetchone()[0]

    def request(self,url,**kwargs):
        while not self.stop.is_set():
            if self.slots.acquire(timeout=0.1):break
        else:return FetchError(url,'Collection stopped')
        try:
            while not self.stop.is_set():
                now=time.time()
                with sqlite3.connect(self.database,timeout=30) as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    cooldown,next_request,window,requests=conn.execute('''SELECT cooldown_until,
                      next_request_at,window_started_at,window_requests FROM worker_state WHERE singleton=1''').fetchone()
                    if cooldown>now:return FetchError(url,'HTTP 429; shared collection cooldown',429,cooldown-now)
                    if now-window>=60:window=now;requests=0
                    delay=max(0,next_request-now,window+60-now if requests>=REQUESTS_PER_MINUTE else 0)
                    if delay<=0:
                        conn.execute('''UPDATE worker_state SET next_request_at=?,window_started_at=?,
                          window_requests=? WHERE singleton=1''',(now+REQUEST_SPACING,window,requests+1))
                        break
                if self.stop.wait(min(delay,1)):return FetchError(url,'Collection stopped')
            else:return FetchError(url,'Collection stopped')
            result=fetch_snapshot(url,**kwargs)
            if isinstance(result,FetchError) and result.status==429:
                retry=result.retry_after_seconds
                delay=retry if type(retry) in (int,float) and math.isfinite(retry) and retry>0 else 60
                with sqlite3.connect(self.database,timeout=30) as conn:
                    conn.execute('UPDATE worker_state SET cooldown_until=MAX(cooldown_until,?) WHERE singleton=1',
                                 (time.time()+delay,))
            return result
        finally:self.slots.release()


@contextmanager
def _budgeted_collectors(budget):
    # Existing collectors bind the HTTP function at module import. The worker is
    # a dedicated CLI process, so routing these boundaries does not touch server
    # request threads. Restore every binding when that process's run returns.
    from . import spoon,monthly,gifts,directory
    originals=[]
    try:
        for module in (spoon,monthly,gifts,directory):
            originals.append((module,module.fetch_snapshot))
            module.fetch_snapshot=budget.request
        yield
    finally:
        for module,original in originals:module.fetch_snapshot=original


def _claim(database):
    now=time.time()
    with sqlite3.connect(database,timeout=30) as conn:
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute('''SELECT kind,dj_id,priority,failure_count FROM worker_jobs
          WHERE state!='running' AND due_at<=?
          ORDER BY priority+MAX(0,(?-due_at)/3600.0)*5 DESC,due_at,kind,dj_id LIMIT 1''',(now,now)).fetchone()
        if row is None:return None
        conn.execute("UPDATE worker_jobs SET state='running' WHERE kind=? AND dj_id=?",row[:2])
        return {'kind':row[0],'dj_id':row[1],'priority':row[2],'failure_count':row[3]}


def _attempt(database,job,stop,concurrency,interval):
    kind=job['kind'];uid=job['dj_id'];started=_iso();run_id=None;attempt_id=None
    try:
        run_id=collection_status.begin(database,kind,uid,interval)
        with sqlite3.connect(database,timeout=30) as conn:
            attempt_id=conn.execute('''INSERT INTO worker_attempts(kind,dj_id,started_at,state,run_id)
              VALUES(?,?,?,'running',?)''',(kind,uid,started,run_id)).lastrowid
            conn.execute('UPDATE worker_jobs SET last_attempt_id=? WHERE kind=? AND dj_id=?',(attempt_id,kind,uid))
        links=[]
        if kind=='monthly':
            summary=collect_monthly(database,max_djs=0,concurrency=1,max_pages=0,stopped_event=stop,dj_ids=[uid])
        elif kind=='gifts':
            summary=collect_gifts(database,uid,max_pages=0,stopped_event=stop)
            if summary.get('snapshot_id') is not None:links.append(summary['snapshot_id'])
        else:
            processed=set(); callback_errors=[]; partial_rooms=[]; callback_lock=threading.Lock()
            def room_finished(snapshot,metadata):
                # Callbacks execute in collector threads; save completed rooms
                # immediately so another room's 429 cannot discard their data.
                with callback_lock:
                    rid=metadata['room_id']
                    if rid in processed:return
                    processed.add(rid)
                    try:
                        if snapshot is None:
                            record_room_attempt(database,metadata,collection_run_id=run_id)
                        else:
                            links.append(save_snapshot(database,snapshot,metadata=metadata,collection_run_id=run_id))
                        if metadata['state']!='completed':partial_rooms.append(rid)
                    except (ValueError,TypeError,KeyError,sqlite3.Error) as exc:
                        callback_errors.append(f'Room {rid}: {exc}')
            try:results,errors=collect_spoon(concurrency=concurrency,max_rooms=0,max_pages=0,
                                           room_callback=room_finished,stopped_event=stop)
            except SpoonRateLimit as exc:
                results=[];errors=['HTTP429'];retry=exc.retry_after
            else:retry=0
            for result in results:
                if result.get('room_id') in processed:continue
                try:links.append(save_snapshot(database,result))
                except (ValueError,TypeError,KeyError) as exc:errors.append(str(exc))
            errors.extend(callback_errors)
            summary={'complete':not errors and not partial_rooms and all(r.get('complete') is True for r in results),
                     'room_count':len(links),'errors':errors,'retry_after':retry,
                     'coverage':'listed_public_rooms','snapshot_ids':links}
        summary=dict(summary,restart_policy='restart_from_first_page')
        collection_status.finish(database,run_id,summary)
        with sqlite3.connect(database,timeout=30) as conn:
            state=conn.execute('SELECT state FROM collection_runs WHERE id=?',(run_id,)).fetchone()[0]
        failed=state!='completed'
        failures=job['failure_count']+1 if failed else 0
        retry=summary.get('retry_after',0)
        if type(retry) not in (int,float) or not math.isfinite(retry) or retry<0:retry=0
        delay=max(retry,min(interval,60*2**min(failures-1,6)) if failed else interval)
        with sqlite3.connect(database,timeout=30) as conn:
            conn.execute('''UPDATE worker_attempts SET finished_at=?,state=?,summary=? WHERE id=?''',
                         (_iso(),state,json.dumps(summary,ensure_ascii=False),attempt_id))
            conn.execute('''UPDATE worker_jobs SET state=?,due_at=?,failure_count=? WHERE kind=? AND dj_id=?''',
                         ('cooldown' if retry else state,time.time()+delay,failures,kind,uid))
            if retry:
                conn.execute('UPDATE worker_state SET cooldown_until=MAX(cooldown_until,?) WHERE singleton=1',
                             (time.time()+retry,))
            conn.executemany('INSERT OR IGNORE INTO worker_snapshot_links VALUES(?,?,?)',
                             [(kind,sid,attempt_id) for sid in links])
        return summary
    except Exception as exc:
        summary={'complete':False,'errors':[str(exc)],'restart_policy':'restart_from_first_page'}
        if run_id is not None:collection_status.finish(database,run_id,summary,failed=True)
        with sqlite3.connect(database,timeout=30) as conn:
            if attempt_id is not None:
                conn.execute('UPDATE worker_attempts SET finished_at=?,state=\'failed\',summary=? WHERE id=?',
                             (_iso(),json.dumps(summary,ensure_ascii=False),attempt_id))
            conn.execute('''UPDATE worker_jobs SET state='failed',due_at=?,failure_count=failure_count+1
              WHERE kind=? AND dj_id=?''',(time.time()+min(interval,60),kind,uid))
        return summary


def _recover(database):
    stamp=_iso()
    details={'complete':False,'errors':['Previous worker ended without a saved result.'],
             'restart_policy':'restart_from_first_page'}
    with sqlite3.connect(database,timeout=30) as conn:
        unfinished=conn.execute("SELECT id,run_id FROM worker_attempts WHERE state='running'").fetchall()
        for attempt_id,_ in unfinished:
            conn.execute('UPDATE worker_attempts SET finished_at=?,state=\'failed\',summary=? WHERE id=?',
                         (stamp,json.dumps(details),attempt_id))
        conn.execute("UPDATE worker_jobs SET state='queued',due_at=MAX(due_at,?) WHERE state='running'",(time.time(),))
    for _,run_id in unfinished:
        if run_id is not None:collection_status.finish(database,run_id,details,failed=True)


def _record_task_failure(database,job,exc,interval):
    """Contain a failed task even if its own error handler did not finish.

    A persistent database error is allowed to escape to the supervisor: it
    must not keep fetching data that it cannot reliably record.
    """
    summary={'complete':False,'errors':[f'Collection task failed ({type(exc).__name__}): {exc}'],
             'restart_policy':'restart_from_first_page'}
    run_id=None
    with sqlite3.connect(database,timeout=30) as conn:
        row=conn.execute('''SELECT a.id,a.run_id,a.state FROM worker_jobs j
          LEFT JOIN worker_attempts a ON a.id=j.last_attempt_id
          WHERE j.kind=? AND j.dj_id=?''',(job['kind'],job['dj_id'])).fetchone()
        if row and row[2]=='running':
            conn.execute("UPDATE worker_attempts SET finished_at=?,state='failed',summary=? WHERE id=?",
                         (_iso(),json.dumps(summary,ensure_ascii=False),row[0]))
            run_id=row[1]
        delay=min(interval,60*2**min(job['failure_count'],6))
        conn.execute("""UPDATE worker_jobs SET state='failed',due_at=?,failure_count=failure_count+1
          WHERE kind=? AND dj_id=?""",(time.time()+delay,job['kind'],job['dj_id']))
        conn.execute('UPDATE worker_state SET last_error=? WHERE singleton=1',(summary['errors'][0],))
    if run_id is not None:collection_status.finish(database,run_id,summary,failed=True)
    return summary


@contextmanager
def _cancel_on_failure(stop):
    """Cancel fetching before the executor waits for its unfinished jobs."""
    try:
        yield
    except BaseException:
        stop.set()
        raise


def run(database,auth_database,stop_event=None,concurrency=4,live_interval=300,ranking_interval=3600):
    """Run one supervised worker until stopped; duplicate workers raise explicitly.

    The caller must use a dedicated CLI process, set signal handlers and keep
    old manual collectors stopped. Old collector processes do not acquire this
    lock. Registered DJ jobs refresh independently; discovery never resets due
    dates or triggers a full monthly sweep on every pass.
    """
    if type(concurrency) is not int or not 1<=concurrency<=16:raise ValueError('concurrency must be 1..16')
    for value in (live_interval,ranking_interval):
        if type(value) not in (int,float) or not math.isfinite(value) or value<30:raise ValueError('interval must be finite and at least 30 seconds')
    _account_inputs(auth_database) # Reject a missing/empty auth DB before creating observation files.
    initialize_observations(database);initialize(database)
    stop=stop_event if stop_event is not None else threading.Event()
    attempts=0
    with _lease(database) as token:
        _recover(database)
        with sqlite3.connect(database,timeout=30) as conn:
            conn.execute('''UPDATE worker_state SET owner_token=?,pid=?,started_at=?,heartbeat_at=?,
              stopped_at=NULL,state='running',last_error=NULL,restart_at=NULL WHERE singleton=1''',(token,os.getpid(),_iso(),_iso()))
        heartbeat_stop=threading.Event()
        def heartbeat():
            failures=0
            while not heartbeat_stop.wait(HEARTBEAT_INTERVAL):
                try:
                    with sqlite3.connect(database,timeout=30) as conn:
                        changed=conn.execute('UPDATE worker_state SET heartbeat_at=? WHERE singleton=1 AND owner_token=?',(_iso(),token)).rowcount
                    if not changed:stop.set();return
                    failures=0
                except sqlite3.Error:
                    failures+=1
                    # One busy heartbeat must not terminate all collectors.
                    if failures>=3:stop.set();return
        thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
        futures={};last_discovery=0;budget=_Budget(database,stop,concurrency)
        try:
            with (_budgeted_collectors(budget),ThreadPoolExecutor(max_workers=concurrency) as pool,
                  _cancel_on_failure(stop)):
                while not stop.is_set():
                    now=time.time()
                    if now-last_discovery>=DISCOVERY_INTERVAL:
                        try:_discover(database,auth_database)
                        except (OSError,ValueError,sqlite3.Error):
                            with sqlite3.connect(database,timeout=30) as conn:
                                conn.execute("UPDATE worker_state SET last_error='Account discovery could not be read.' WHERE singleton=1")
                        last_discovery=now
                    for future in list(futures):
                        if future.done():
                            job=futures.pop(future)
                            try:future.result()
                            except Exception as exc:
                                interval=live_interval if job['kind']=='live' else ranking_interval
                                _record_task_failure(database,job,exc,interval)
                            attempts+=1
                    if budget.cooldown()<=now:
                        while len(futures)<concurrency and not stop.is_set():
                            job=_claim(database)
                            if job is None:break
                            interval=live_interval if job['kind']=='live' else ranking_interval
                            future=pool.submit(_attempt,database,job,stop,concurrency,interval)
                            futures[future]=job
                    stop.wait(0.2)
                for future,job in futures.items():
                    try:future.result()
                    except Exception as exc:
                        interval=live_interval if job['kind']=='live' else ranking_interval
                        _record_task_failure(database,job,exc,interval)
                    attempts+=1
        finally:
            heartbeat_stop.set();thread.join(timeout=2)
            with sqlite3.connect(database,timeout=30) as conn:
                conn.execute("UPDATE worker_state SET state='stopped',stopped_at=? WHERE singleton=1 AND owner_token=?",(_iso(),token))
    return {'state':'stopped','attempt_count':attempts}
