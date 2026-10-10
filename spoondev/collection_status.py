"""Persist collection round outcomes; recorded state is not proof of a live process."""
from datetime import datetime,timezone,timedelta
from contextlib import closing
import json,sqlite3
import math

SCHEMA='''CREATE TABLE IF NOT EXISTS collection_runs(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,target TEXT NOT NULL,started_at TEXT NOT NULL,finished_at TEXT,state TEXT NOT NULL,interval_seconds REAL NOT NULL,details TEXT NOT NULL);CREATE INDEX IF NOT EXISTS collection_runs_latest ON collection_runs(kind,target,id);'''

def begin(database,kind,target='',interval=0):
    with closing(sqlite3.connect(database,timeout=30)) as c, c:
        c.executescript(SCHEMA)
        return c.execute('INSERT INTO collection_runs(kind,target,started_at,state,interval_seconds,details) VALUES(?,?,?,\'running\',?,\'{}\')',(kind,target,datetime.now(timezone.utc).isoformat(),interval)).lastrowid

def finish(database,run_id,details,failed=False):
    incomplete=bool(details.get('errors')) or details.get('complete') is False
    successful=None
    if 'snapshot_id' in details:
        successful=details['snapshot_id'] is not None
    else:
        for count in ('dj_count','room_count'):
            if count in details:
                successful=details[count]>0
                break
    state=('failed' if failed or (incomplete and successful is False) else
           'partial' if incomplete else 'completed')
    with closing(sqlite3.connect(database,timeout=30)) as c, c:
        c.execute('UPDATE collection_runs SET finished_at=?,state=?,details=? WHERE id=?',(datetime.now(timezone.utc).isoformat(),state,json.dumps(details,ensure_ascii=False),run_id))

def read(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='collection_runs'").fetchone():return []
    rows=conn.execute('SELECT * FROM collection_runs WHERE id IN (SELECT MAX(id) FROM collection_runs GROUP BY kind,target) ORDER BY id DESC LIMIT 30').fetchall()
    result=[]
    for row in rows:
        r=dict(zip(('id','kind','target','started_at','finished_at','state','interval_seconds','details'),row));r['details']=json.loads(r['details'])
        retry=r['details'].get('retry_after',0)
        if type(retry) not in (int,float) or not math.isfinite(retry) or retry<0:retry=0
        r['next_eligible_at']=(datetime.fromisoformat(r['finished_at'])+timedelta(seconds=max(r['interval_seconds'],retry))).isoformat() if r['finished_at'] else None
        result.append(r)
    return result
