"""Read-only queries for the local search website.

Every entry point opens SQLite with mode=ro, so a missing database is an
error and can never silently become an empty database.
"""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from . import profiledb
from .db import broadcaster_listeners, listener_broadcasters, temperature_history


@contextmanager
def _read(database, private_database=None):
    uri = Path(database).resolve().as_uri() + '?mode=ro'
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    if private_database:
        conn.execute('ATTACH DATABASE ? AS private', (Path(private_database).resolve().as_uri()+'?mode=ro',))
        for table in ('fan_owners','registered_fans'):
            conn.execute(f'CREATE TEMP VIEW {table} AS SELECT * FROM private.{table}')
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
      FROM (SELECT id,name,tag,last_seen_at FROM ({_PROFILE})
            UNION ALL SELECT id,name,tag,updated_at AS last_seen_at FROM profile_users)
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
        result['profile_coverage'] = None
        result['profile_ranking_complete'] = None
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='profile_snapshots'").fetchone():
            profile = profiledb.latest_profile(conn,user_id)
            if profile and profile['month'] == datetime.now(ZoneInfo('Asia/Tokyo')).strftime('%Y-%m'):
                result.update(appearances=profile['appearances'],profile_month=profile['month'],
                              profile_observed_at=profile['observed_at'],profile_complete=profile['complete'],
                              profile_coverage=profile.get('coverage'),profile_ranking_complete=profile.get('ranking_complete'))
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


def favorite_activity(database, user_ids):
    # Last listener observation, independent of profile/monthly refresh times.
    if not isinstance(user_ids, list) or len(user_ids) > 100 or any(
        not isinstance(user_id, str) or not user_id.isascii() or not user_id.isdigit()
        or len(user_id) > 200 for user_id in user_ids
    ):
        raise ValueError('At most 100 numeric account IDs required')
    user_ids = list(dict.fromkeys(user_ids))
    now = datetime.now(timezone.utc)
    observed = {}
    if user_ids:
        with _read(database) as conn:
            placeholders = ','.join('?' for _ in user_ids)
            rows = conn.execute(f'''SELECT m.listener_id AS id,MAX(s.observed_at) AS last_live_at
                FROM memberships m JOIN snapshots s ON s.id=m.snapshot_id
                WHERE m.listener_id IN ({placeholders}) GROUP BY m.listener_id''', user_ids).fetchall()
            observed = {row['id']: row['last_live_at'] for row in rows}
    users = []
    for user_id in user_ids:
        value = observed.get(user_id)
        recent = False
        if value:
            seen = datetime.fromisoformat(value.replace('Z', '+00:00'))
            recent = now - timedelta(minutes=30) <= seen <= now
        users.append({'id': user_id, 'last_live_at': value, 'recent': recent})
    return {'users': users, 'checked_at': now.isoformat(), 'recent_minutes': 30}


def fan_owners(database, private_database=None):
    with _read(database, private_database) as conn:
        return [dict(row) for row in conn.execute('''SELECT o.*,
            (SELECT COUNT(*) FROM registered_fans f WHERE f.owner_id=o.id) AS fan_count
            FROM fan_owners o ORDER BY imported_at DESC,id''')]


def fan_destinations(database, owner_id, offset=0, limit=50, private_database=None):
    """Join an owner's imported fans with current-month rankings and recent live sightings."""
    from .fans import numeric_id
    owner_id=numeric_id(owner_id)
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=50:
        raise ValueError('Invalid pagination')
    now=datetime.now(timezone.utc)
    month=now.astimezone(ZoneInfo('Asia/Tokyo')).strftime('%Y-%m')
    cutoff=(now-timedelta(minutes=30)).isoformat(timespec='microseconds')
    with _read(database, private_database) as conn:
        owner=conn.execute('SELECT * FROM fan_owners WHERE id=?',(owner_id,)).fetchone()
        if owner is None:
            return {'owner':None,'fans':[],'has_more':False,'total':0,'month':month}
        total=conn.execute('SELECT COUNT(*) FROM registered_fans WHERE owner_id=?',(owner_id,)).fetchone()[0]
        rows=conn.execute(f'''WITH profiles AS ({_profiles(conn)}), activity AS (
          SELECT m.listener_id,MAX(s.observed_at) AS last_live_at FROM registered_fans f
          JOIN memberships m ON m.listener_id=f.user_id JOIN snapshots s ON s.id=m.snapshot_id
          WHERE f.owner_id=? AND s.observed_at<=? GROUP BY m.listener_id)
          SELECT f.user_id AS id,COALESCE(p.name,f.name,'名前未取得') AS name,
          COALESCE(p.tag,f.tag) AS tag,a.last_live_at,
          COALESCE(a.last_live_at>=?,0) AS recent FROM registered_fans f
          LEFT JOIN profiles p ON p.id=f.user_id LEFT JOIN activity a ON a.listener_id=f.user_id
          WHERE f.owner_id=? ORDER BY recent DESC,
          CASE WHEN a.last_live_at>=? THEN a.last_live_at END DESC,f.user_id LIMIT ? OFFSET ?''',
          (owner_id,now.isoformat(timespec='microseconds'),cutoff,owner_id,cutoff,limit+1,offset)).fetchall()
        users=[dict(row,monthly=[],live=[]) for row in rows[:limit]]
        indexed=conn.execute('SELECT COUNT(DISTINCT dj_id),MAX(observed_at) FROM monthly_dj_snapshots WHERE month=?',(month,)).fetchone()
        if users:
            by_id={user['id']:user for user in users}
            placeholders=','.join('?' for _ in users)
            monthly=conn.execute(f'''WITH latest AS (
              SELECT id,dj_id,observed_at,complete,ROW_NUMBER() OVER (
              PARTITION BY dj_id ORDER BY observed_at DESC,id DESC) AS rn
              FROM monthly_dj_snapshots WHERE month=?), relations AS (
              SELECT l.listener_id,u.id AS user_id,u.name,u.tag,l.temperature,s.observed_at,s.complete,
              ROW_NUMBER() OVER(PARTITION BY l.listener_id ORDER BY l.temperature DESC,u.id) AS position
              FROM latest s JOIN monthly_dj_listeners l ON l.snapshot_id=s.id
              JOIN profile_users u ON u.id=s.dj_id WHERE s.rn=1 AND l.listener_id IN ({placeholders}))
              SELECT * FROM relations WHERE position<=5 ORDER BY listener_id,position''',
              [month]+list(by_id)).fetchall()
            for row in monthly:
                by_id[row['listener_id']]['monthly'].append({key:row[key] for key in
                    ('user_id','name','tag','temperature','observed_at','complete')})
            live=conn.execute(f'''WITH sightings AS (
              SELECT m.listener_id,s.broadcaster_id,MAX(s.observed_at) AS observed_at
              FROM memberships m JOIN snapshots s ON s.id=m.snapshot_id
              WHERE m.listener_id IN ({placeholders}) AND s.observed_at>=? AND s.observed_at<=?
              GROUP BY m.listener_id,s.broadcaster_id), relations AS (
              SELECT x.listener_id,u.id AS user_id,u.name,x.observed_at,
              ROW_NUMBER() OVER(PARTITION BY x.listener_id ORDER BY x.observed_at DESC,u.id) AS position
              FROM sightings x JOIN users u ON u.id=x.broadcaster_id)
              SELECT * FROM relations WHERE position<=5 ORDER BY listener_id,position''',
              list(by_id)+[cutoff,now.isoformat(timespec='microseconds')]).fetchall()
            for row in live:
                by_id[row['listener_id']]['live'].append({key:row[key] for key in ('user_id','name','observed_at')})
    return {'owner':dict(owner),'fans':users,'has_more':len(rows)>limit,'total':total,
            'month':month,'indexed_djs':indexed[0],'monthly_observed_at':indexed[1],
            'checked_at':now.isoformat(),'recent_minutes':30,'destinations_limit':5}
