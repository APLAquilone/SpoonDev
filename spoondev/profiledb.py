"""Monthly public profile temperatures, distinct from live-room observations."""
from contextlib import contextmanager
from datetime import datetime, timezone
import math
import re
import sqlite3

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS profile_users (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, tag TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS profile_snapshots (
 id INTEGER PRIMARY KEY, listener_id TEXT NOT NULL REFERENCES profile_users(id),
 observed_at TEXT NOT NULL, month TEXT NOT NULL, complete INTEGER NOT NULL CHECK(complete IN (0,1)));
CREATE TABLE IF NOT EXISTS profile_destinations (
 snapshot_id INTEGER NOT NULL REFERENCES profile_snapshots(id) ON DELETE CASCADE,
 broadcaster_id TEXT NOT NULL REFERENCES profile_users(id), temperature REAL,
 PRIMARY KEY(snapshot_id,broadcaster_id));
CREATE TABLE IF NOT EXISTS monthly_dj_snapshots (
 id INTEGER PRIMARY KEY, dj_id TEXT NOT NULL REFERENCES profile_users(id),
 month TEXT NOT NULL, observed_at TEXT NOT NULL, complete INTEGER NOT NULL CHECK(complete IN (0,1)));
CREATE TABLE IF NOT EXISTS monthly_dj_listeners (
 snapshot_id INTEGER NOT NULL REFERENCES monthly_dj_snapshots(id) ON DELETE CASCADE,
 listener_id TEXT NOT NULL REFERENCES profile_users(id), temperature REAL,
 PRIMARY KEY(snapshot_id,listener_id));
CREATE INDEX IF NOT EXISTS monthly_dj_snapshots_latest ON monthly_dj_snapshots(dj_id,month,observed_at);
CREATE INDEX IF NOT EXISTS monthly_dj_listeners_user ON monthly_dj_listeners(listener_id,snapshot_id);
CREATE INDEX IF NOT EXISTS profile_snapshots_listener ON profile_snapshots(listener_id,observed_at);
'''

@contextmanager
def _connection(database):
    owned=not isinstance(database,sqlite3.Connection)
    conn=sqlite3.connect(database,timeout=30) if owned else database
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        yield conn
    finally:
        if owned: conn.close()


def initialize(database):
    with _connection(database) as conn:
        conn.executescript(_SCHEMA)


def _timestamp(value):
    if value is None:
        stamp=datetime.now(timezone.utc)
    else:
        if not isinstance(value,str): raise ValueError('observed_at requires ISO8601 string')
        try: stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
        except ValueError as exc: raise ValueError('invalid observed_at') from exc
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError('observed_at requires timezone')
    return stamp.astimezone(timezone.utc).isoformat(timespec='microseconds')


def _user(value, destination=False):
    allowed={'id','name','tag'} | ({'temperature'} if destination else set())
    if not isinstance(value,dict) or not {'id','name'} <= value.keys() or value.keys()-allowed:
        raise ValueError('user requires id and name with optional tag')
    if not isinstance(value['id'],str) or not value['id'].strip(): raise ValueError('id requires nonempty string')
    if not isinstance(value['name'],str): raise ValueError('name requires string')
    tag=value.get('tag')
    if tag is not None and not isinstance(tag,str): raise ValueError('tag requires string or null')
    if destination:
        temperature=value.get('temperature')
        if temperature is not None:
            if type(temperature) not in (int,float): raise ValueError('temperature requires finite number or null')
            try: valid=math.isfinite(temperature)
            except OverflowError: valid=False
            if not valid: raise ValueError('temperature requires finite number')
    return (value['id'],value['name'],tag)


def _upsert(conn,users,stamp):
    conn.executemany('''INSERT INTO profile_users VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET
      name=excluded.name,tag=excluded.tag,updated_at=excluded.updated_at
      WHERE excluded.updated_at >= profile_users.updated_at''',[(u,n,t,stamp) for u,n,t in users])


@contextmanager
def _atomic(conn):
    conn.execute('SAVEPOINT profile_write')
    try:
        yield
        conn.execute('RELEASE SAVEPOINT profile_write')
    except BaseException:
        conn.execute('ROLLBACK TO SAVEPOINT profile_write')
        conn.execute('RELEASE SAVEPOINT profile_write')
        raise


def cache_users(database,users,observed_at=None):
    if not isinstance(users,list): raise ValueError('users requires list')
    canonical={}
    for value in users:
        user=_user(value)
        if user[0] in canonical and canonical[user[0]] != user: raise ValueError('conflicting duplicate user')
        canonical[user[0]]=user
    stamp=_timestamp(observed_at)
    with _connection(database) as conn, _atomic(conn):
        _upsert(conn,canonical.values(),stamp)


def save_profile(database,listener,destinations,month,complete,observed_at=None):
    """Save monthly-profile relationships atomically without inventing live visits."""
    owner=_user(listener)
    if not isinstance(month,str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',month):
        raise ValueError('month requires YYYY-MM')
    if type(complete) is not bool: raise ValueError('complete requires boolean')
    if not isinstance(destinations,list): raise ValueError('destinations requires list')
    stamp=_timestamp(observed_at)
    users={owner[0]:owner}
    temperatures={}
    for value in destinations:
        user=_user(value,True)
        temperature=value.get('temperature')
        if user[0] in users and users[user[0]] != user: raise ValueError('conflicting duplicate user')
        if user[0] in temperatures and temperatures[user[0]] != temperature: raise ValueError('conflicting duplicate temperature')
        users[user[0]]=user
        temperatures[user[0]]=temperature
    with _connection(database) as conn, _atomic(conn):
        _upsert(conn,users.values(),stamp)
        snapshot_id=conn.execute('INSERT INTO profile_snapshots(listener_id,observed_at,month,complete) VALUES (?,?,?,?)',
                                 (owner[0],stamp,month,int(complete))).lastrowid
        conn.executemany('INSERT INTO profile_destinations VALUES (?,?,?)',[(snapshot_id,u,t) for u,t in temperatures.items()])
        return snapshot_id


def profile_user(database,user_id):
    with _connection(database) as conn:
        row=conn.execute('SELECT id,name,tag,updated_at FROM profile_users WHERE id=?',(user_id,)).fetchone()
    return dict(zip(('id','name','tag','updated_at'),row)) if row else None


def latest_profile(database,user_id):
    with _connection(database) as conn:
        # Optional additive tables may not exist in an older database opened read-only.
        exists=conn.execute("SELECT 1 FROM sqlite_master WHERE name='monthly_dj_snapshots'").fetchone()
        if exists:
            indexed=_indexed_profile(conn,user_id)
            if indexed is not None:
                return indexed
        row=conn.execute('''SELECT id,month,observed_at,complete FROM profile_snapshots
          WHERE listener_id=? ORDER BY observed_at DESC,id DESC LIMIT 1''',(user_id,)).fetchone()
        if row is None: return None
        appearances=conn.execute('''SELECT u.id,u.name,u.tag,d.temperature FROM profile_destinations d
          JOIN profile_users u ON u.id=d.broadcaster_id WHERE d.snapshot_id=?
          ORDER BY d.temperature DESC,u.id''',(row[0],)).fetchall()
    return {'month':row[1],'observed_at':row[2],'complete':bool(row[3]),'appearances':[
        dict(zip(('user_id','name','tag','temperature'),a),source='monthly_profile') for a in appearances]}


def search_cached(database,q='',limit=50,offset=0):
    """Return cached profiles matching literal ID prefix/name/tag substring."""
    if not isinstance(q,str): raise ValueError('q requires string')
    if type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
        raise ValueError('invalid pagination')
    escaped=q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
    with _connection(database) as conn:
        rows=conn.execute('''SELECT id,name,tag,updated_at FROM profile_users
          WHERE id LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' OR tag LIKE ? ESCAPE '\\'
          ORDER BY updated_at DESC,id LIMIT ? OFFSET ?''',
          (escaped+'%','%'+escaped+'%','%'+escaped+'%',limit,offset)).fetchall()
    return [dict(zip(('id','name','tag','updated_at'),row)) for row in rows]


def save_dj_ranking(database,dj,entries,month,complete,observed_at=None):
    """Commit one fetched DJ ranking, including an explicitly successful empty list."""
    owner=_user(dj)
    if not isinstance(month,str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',month):
        raise ValueError('month requires YYYY-MM')
    if type(complete) is not bool: raise ValueError('complete requires boolean')
    if not isinstance(entries,list): raise ValueError('entries requires list')
    users={owner[0]:owner}; temperatures={}
    for entry in entries:
        if not isinstance(entry,dict) or set(entry)!={'user','temperature'}:
            raise ValueError('entry requires user and temperature')
        user=_user(entry['user'])
        _user(dict(entry['user'],temperature=entry['temperature']),True)
        if user[0] in users and users[user[0]] != user: raise ValueError('conflicting duplicate user')
        if user[0] in temperatures and temperatures[user[0]] != entry['temperature']:
            raise ValueError('conflicting duplicate temperature')
        users[user[0]]=user; temperatures[user[0]]=entry['temperature']
    stamp=_timestamp(observed_at)
    with _connection(database) as conn, _atomic(conn):
        _upsert(conn,users.values(),stamp)
        snapshot_id=conn.execute('INSERT INTO monthly_dj_snapshots(dj_id,month,observed_at,complete) VALUES (?,?,?,?)',
                                 (owner[0],month,stamp,int(complete))).lastrowid
        conn.executemany('INSERT INTO monthly_dj_listeners VALUES (?,?,?)',[(snapshot_id,u,t) for u,t in temperatures.items()])
        return snapshot_id


def _indexed_profile(conn,user_id):
    month_row=conn.execute('SELECT MAX(month) FROM monthly_dj_snapshots').fetchone()
    month=month_row[0]
    if month is None: return None
    # Newest successful observation replaces that DJ's previous ranking, even when empty.
    rows=conn.execute('''WITH latest AS (
       SELECT dj_id,MAX(id) AS sid FROM monthly_dj_snapshots ds WHERE month=?
       AND observed_at=(SELECT MAX(observed_at) FROM monthly_dj_snapshots other
         WHERE other.dj_id=ds.dj_id AND other.month=ds.month) GROUP BY dj_id)
       SELECT u.id,u.name,u.tag,l.temperature,s.observed_at,s.complete
       FROM latest x JOIN monthly_dj_snapshots s ON s.id=x.sid
       JOIN monthly_dj_listeners l ON l.snapshot_id=s.id JOIN profile_users u ON u.id=s.dj_id
       WHERE l.listener_id=? ORDER BY l.temperature DESC,u.id''',(month,user_id)).fetchall()
    if not rows:
        observed=conn.execute('SELECT MAX(observed_at) FROM monthly_dj_snapshots WHERE month=?',(month,)).fetchone()[0]
        return {'month':month,'observed_at':observed,'complete':False,'ranking_complete':True,
                'coverage':'known_broadcasters','appearances':[]}
    return {'month':month,'observed_at':max(row[4] for row in rows),'complete':False,
            'ranking_complete':all(bool(row[5]) for row in rows),'coverage':'known_broadcasters',
            'appearances':[dict(zip(('user_id','name','tag','temperature'),row[:4]),
                              source='monthly_profile',complete=bool(row[5])) for row in rows]}
