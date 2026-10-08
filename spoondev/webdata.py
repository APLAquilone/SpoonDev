"""Read-only queries for the local search website.

Every entry point opens SQLite with mode=ro, so a missing database is an
error and can never silently become an empty database.
"""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
from . import profiledb
from .db import broadcaster_listeners, listener_broadcasters, temperature_history


@contextmanager
def _read(database):
    uri = Path(database).resolve().as_uri() + '?mode=ro'
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    try:
        yield conn
    finally:
        conn.close()


_PROFILE = '''SELECT u.id,u.name,u.name_observed_at AS last_seen_at,
 (SELECT a.tag FROM user_attributes a JOIN snapshots s ON s.id=a.snapshot_id
  WHERE a.user_id=u.id ORDER BY s.observed_at DESC,s.id DESC LIMIT 1) AS tag
 FROM users u'''


def _literal(value):
    return value.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')


def _profiles(conn):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='profile_users'").fetchone() is None:
        return _PROFILE
    return f'''SELECT id,name,tag,last_seen_at FROM (
      SELECT id,name,tag,last_seen_at,ROW_NUMBER() OVER(PARTITION BY id ORDER BY last_seen_at DESC) AS rn
      FROM ({_PROFILE} UNION ALL SELECT id,name,tag,updated_at AS last_seen_at FROM profile_users)
    ) WHERE rn=1'''


def search_users(database, q, limit=50, offset=0):
    """Search stable ID prefixes and literal name/profile-tag substrings."""
    if not isinstance(q,str):
        raise ValueError('q must be a string')
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('limit must be an integer between 1 and 500')
    if type(offset) is not int or offset < 0:
        raise ValueError('offset must be a nonnegative integer')
    escaped = _literal(q)
    with _read(database) as conn:
        sql = f'''WITH profiles AS ({_profiles(conn)}) SELECT id,name,tag,last_seen_at FROM profiles
          WHERE id LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' OR tag LIKE ? ESCAPE '\\'
          ORDER BY last_seen_at DESC,id ASC LIMIT ? OFFSET ?'''
        rows = conn.execute(sql,(escaped+'%','%'+escaped+'%','%'+escaped+'%',limit+1,offset)).fetchall()
    return {'users':[dict(row) for row in rows[:limit]],'has_more':len(rows)>limit}


def user_details(database, user_id):
    """Return the current profile and both observed relationship directions."""
    with _read(database) as conn:
        row = conn.execute('SELECT * FROM ('+_profiles(conn)+') WHERE id=?',(user_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result['broadcasters'] = listener_broadcasters(conn,user_id)
        result['listeners'] = broadcaster_listeners(conn,user_id)
        result['appearances'] = []
        result['profile_month'] = None
        result['profile_observed_at'] = None
        result['profile_complete'] = False
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='profile_snapshots'").fetchone():
            profile = profiledb.latest_profile(conn,user_id)
            if profile and profile['month'] == datetime.now(ZoneInfo('Asia/Tokyo')).strftime('%Y-%m'):
                result.update(appearances=profile['appearances'],profile_month=profile['month'],
                              profile_observed_at=profile['observed_at'],profile_complete=profile['complete'])
        return result


def stats(database):
    with _read(database) as conn:
        row = conn.execute('''SELECT COUNT(*) AS snapshot_count,MAX(observed_at) AS last_observed_at FROM snapshots''').fetchone()
        result = dict(row)
        result['user_count'] = conn.execute('SELECT COUNT(*) FROM ('+_profiles(conn)+')').fetchone()[0]
        result['monthly_indexed_djs'] = 0
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='monthly_dj_snapshots'").fetchone():
            month=datetime.now(ZoneInfo('Asia/Tokyo')).strftime('%Y-%m')
            result['monthly_indexed_djs']=conn.execute('SELECT COUNT(DISTINCT dj_id) FROM monthly_dj_snapshots WHERE month=?',(month,)).fetchone()[0]
        return result


def history(database, broadcaster, listener):
    with _read(database) as conn:
        return temperature_history(conn,broadcaster,listener)
