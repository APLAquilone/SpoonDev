"""Read-only queries for the local search website.

Every entry point opens SQLite with mode=ro, so a missing database is an
error and can never silently become an empty database.
"""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
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


def search_users(database, q, limit=50, offset=0):
    """Search stable ID prefixes and literal name/profile-tag substrings."""
    if not isinstance(q,str):
        raise ValueError('q must be a string')
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('limit must be an integer between 1 and 500')
    if type(offset) is not int or offset < 0:
        raise ValueError('offset must be a nonnegative integer')
    escaped = _literal(q)
    sql = f'''WITH profiles AS ({_PROFILE}) SELECT id,name,tag,last_seen_at FROM profiles
      WHERE id LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' OR tag LIKE ? ESCAPE '\\'
      ORDER BY last_seen_at DESC,id ASC LIMIT ? OFFSET ?'''
    with _read(database) as conn:
        rows = conn.execute(sql,(escaped+'%','%'+escaped+'%','%'+escaped+'%',limit+1,offset)).fetchall()
    return {'users':[dict(row) for row in rows[:limit]],'has_more':len(rows)>limit}


def user_details(database, user_id):
    """Return the current profile and both observed relationship directions."""
    with _read(database) as conn:
        row = conn.execute(_PROFILE+' WHERE u.id=?',(user_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result['broadcasters'] = listener_broadcasters(conn,user_id)
        result['listeners'] = broadcaster_listeners(conn,user_id)
        return result


def stats(database):
    with _read(database) as conn:
        row = conn.execute('''SELECT (SELECT COUNT(*) FROM users) AS user_count,
          COUNT(*) AS snapshot_count,MAX(observed_at) AS last_observed_at FROM snapshots''').fetchone()
        return dict(row)


def history(database, broadcaster, listener):
    with _read(database) as conn:
        return temperature_history(conn,broadcaster,listener)
