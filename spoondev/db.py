"""Atomic persistence of observed public room membership; absence is never inferred."""
from contextlib import contextmanager
from datetime import datetime, timezone
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, name TEXT NOT NULL, name_observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots (
 id INTEGER PRIMARY KEY, room_id TEXT NOT NULL, broadcaster_id TEXT NOT NULL REFERENCES users(id),
 observed_at TEXT NOT NULL, complete INTEGER NOT NULL CHECK(complete IN (0,1)));
CREATE TABLE IF NOT EXISTS names (
 snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
 user_id TEXT NOT NULL REFERENCES users(id), name TEXT NOT NULL,
 PRIMARY KEY(snapshot_id,user_id));
CREATE TABLE IF NOT EXISTS memberships (
 snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
 listener_id TEXT NOT NULL REFERENCES users(id), PRIMARY KEY(snapshot_id,listener_id));
CREATE INDEX IF NOT EXISTS snapshots_broadcaster ON snapshots(broadcaster_id,observed_at);
CREATE INDEX IF NOT EXISTS memberships_listener ON memberships(listener_id,snapshot_id);
"""

@contextmanager
def _connection(database):
    owned = not isinstance(database, sqlite3.Connection)
    conn = sqlite3.connect(database) if owned else database
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        yield conn
    finally:
        if owned:
            conn.close()


def initialize(database):
    with _connection(database) as conn:
        conn.executescript(SCHEMA)


def _user(value):
    if not isinstance(value, dict) or set(value) != {'id', 'name'}:
        raise ValueError('user must have exactly id and name')
    if not isinstance(value['id'], str) or not value['id'].strip():
        raise ValueError('user id must be a nonempty string')
    if not isinstance(value['name'], str):
        raise ValueError('user name must be a string')
    return value['id'], value['name']


def _validate(payload):
    keys = {'room_id','broadcaster','listeners','complete','observed_at'}
    if not isinstance(payload, dict) or set(payload) != keys:
        raise ValueError('snapshot must have exactly room_id, broadcaster, listeners, complete, observed_at')
    if not isinstance(payload['room_id'], str) or not payload['room_id'].strip():
        raise ValueError('room_id must be a nonempty string')
    if type(payload['complete']) is not bool:
        raise ValueError('complete must be boolean')
    if not isinstance(payload['observed_at'], str):
        raise ValueError('observed_at must be an ISO8601 string with timezone')
    try:
        stamp = datetime.fromisoformat(payload['observed_at'].replace('Z','+00:00'))
    except ValueError as exc:
        raise ValueError('observed_at must be an ISO8601 timestamp') from exc
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('observed_at requires timezone')
    stamp = stamp.astimezone(timezone.utc).isoformat(timespec='microseconds')
    broadcaster = _user(payload['broadcaster'])
    if not isinstance(payload['listeners'], list):
        raise ValueError('listeners must be a list')
    listeners = {}
    names = {broadcaster[0]: broadcaster[1]}
    for value in payload['listeners']:
        user_id, name = _user(value)
        if user_id in names and names[user_id] != name:
            raise ValueError('conflicting names for one user ID')
        names[user_id] = name
        listeners[user_id] = name
    return stamp, broadcaster, listeners, names


def save_snapshot(database, payload):
    """Store one validated observation atomically; returns its snapshot ID."""
    stamp, broadcaster, listeners, names = _validate(payload)
    with _connection(database) as conn:
        # SAVEPOINT also preserves a caller's surrounding transaction.
        conn.execute('SAVEPOINT snapshot_write')
        try:
            for user_id, name in names.items():
                conn.execute('''INSERT INTO users VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, name_observed_at=excluded.name_observed_at
                    WHERE excluded.name_observed_at >= users.name_observed_at''', (user_id,name,stamp))
            cursor = conn.execute('INSERT INTO snapshots(room_id,broadcaster_id,observed_at,complete) VALUES (?,?,?,?)',
                                  (payload['room_id'],broadcaster[0],stamp,int(payload['complete'])))
            snapshot_id = cursor.lastrowid
            conn.executemany('INSERT INTO names VALUES (?,?,?)', [(snapshot_id,u,n) for u,n in names.items()])
            conn.executemany('INSERT INTO memberships VALUES (?,?)', [(snapshot_id,u) for u in listeners])
            conn.execute('RELEASE SAVEPOINT snapshot_write')
        except BaseException:
            conn.execute('ROLLBACK TO SAVEPOINT snapshot_write')
            conn.execute('RELEASE SAVEPOINT snapshot_write')
            raise
        return snapshot_id


def _query(database, user_id, reverse):
    target = 's.broadcaster_id' if reverse else 'm.listener_id'
    condition = 'm.listener_id' if reverse else 's.broadcaster_id'
    sql = f'''SELECT u.id,u.name,SUM(s.complete),SUM(1-s.complete),MIN(s.observed_at),MAX(s.observed_at)
      FROM snapshots s JOIN memberships m ON m.snapshot_id=s.id JOIN users u ON u.id={target}
      WHERE {condition}=? GROUP BY u.id,u.name ORDER BY COUNT(*) DESC,u.id'''
    with _connection(database) as conn:
        rows = conn.execute(sql,(user_id,)).fetchall()
    fields = ('user_id','name','complete_count','incomplete_count','first_seen_at','last_seen_at')
    return [dict(zip(fields,row)) for row in rows]


def broadcaster_listeners(database, broadcaster_id):
    return _query(database,broadcaster_id,False)


def listener_broadcasters(database, listener_id):
    return _query(database,listener_id,True)

query_broadcaster_listeners = broadcaster_listeners
query_listener_broadcasters = listener_broadcasters
store_snapshot = save_snapshot
