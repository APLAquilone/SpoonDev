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


RECENT_SECONDS = 30 * 60
CURRENT_SECONDS = 10 * 60
OBSERVATION_GAP_SECONDS = 15 * 60
OBSERVATION_RANGE_NOTE = '観測された時間の範囲です。連続した滞在時間ではありません。'


def _activity_ctes(target_sql):
    """Presence estimates use the newest saved observation, never a cached temperature.

    A newer observation of the same DJ also invalidates an older room. A missing
    listener in a partial observation remains unknown, so it cannot qualify as
    current; its positive, recent observation remains available below.
    """
    return f'''target_ids AS ({target_sql}), observations AS (
      SELECT m.listener_id,MAX(s.observed_at) AS last_live_at
      FROM target_ids t JOIN memberships m ON m.listener_id=t.id
      JOIN snapshots s ON s.id=m.snapshot_id
      WHERE s.observed_at<=:now GROUP BY m.listener_id), current_observations AS (
      SELECT DISTINCT m.listener_id FROM target_ids t
      JOIN memberships m ON m.listener_id=t.id JOIN snapshots s ON s.id=m.snapshot_id
      JOIN user_attributes a ON a.snapshot_id=s.id AND a.user_id=m.listener_id
      WHERE s.observed_at>=:current_cutoff AND s.observed_at<=:now
        AND a.favorite_temperature IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM snapshots newer
          WHERE newer.broadcaster_id=s.broadcaster_id AND newer.observed_at<=:now
            AND (newer.observed_at>s.observed_at OR
                 (newer.observed_at=s.observed_at AND newer.id>s.id)))), activity_rows AS (
      SELECT t.id,o.last_live_at,c.listener_id IS NOT NULL AS current,
        COALESCE(o.last_live_at>=:recent_cutoff,0) AS recent,
        CASE WHEN c.listener_id IS NOT NULL THEN 'current'
             WHEN o.last_live_at>=:recent_cutoff THEN 'recent'
             ELSE 'registered' END AS activity_state,
        CASE WHEN c.listener_id IS NOT NULL THEN 0
             WHEN o.last_live_at>=:recent_cutoff THEN 1 ELSE 2 END AS activity_priority
      FROM target_ids t LEFT JOIN observations o ON o.listener_id=t.id
      LEFT JOIN current_observations c ON c.listener_id=t.id)'''


def _activity_parameters(now):
    return {'now':now.isoformat(timespec='microseconds'),
            'recent_cutoff':(now-timedelta(seconds=RECENT_SECONDS)).isoformat(timespec='microseconds'),
            'current_cutoff':(now-timedelta(seconds=CURRENT_SECONDS)).isoformat(timespec='microseconds')}


def _latest_observation_sessions(conn, user_ids, now, *, since=None):
    """Return each listener/DJ's newest room session as observed bounds.

    A room change, gap exceeding 15 minutes, or a complete observation without
    that listener starts a new session. These bounds describe recorded points,
    not uninterrupted listening or real arrival/departure times. Compact lists
    use ``since`` to show only their recent window; full detail keeps all points.
    """
    if not user_ids:
        return {}
    placeholders=','.join('?' for _ in user_ids)
    # Start bounded previews at the time index, so polling a 50-person list
    # never sorts every observation those people have accumulated over months.
    source=('snapshots s CROSS JOIN memberships m ON m.snapshot_id=s.id'
            if since is not None else 'memberships m JOIN snapshots s ON s.id=m.snapshot_id')
    lower_bound=' AND s.observed_at>=?' if since is not None else ''
    sql=f'''WITH points AS (
      SELECT m.listener_id,s.*,
        ROW_NUMBER() OVER(PARTITION BY m.listener_id,s.broadcaster_id
          ORDER BY s.observed_at DESC,s.id DESC) AS point_order
      FROM {source}
      WHERE m.listener_id IN ({placeholders}) AND s.observed_at<=?{lower_bound}), latest AS (
      SELECT * FROM points WHERE point_order=1), ordered AS (
      SELECT p.*,LAG(p.observed_at) OVER (
        PARTITION BY p.listener_id,p.broadcaster_id ORDER BY p.observed_at,p.id) AS previous_at,
        LAG(p.id) OVER (
        PARTITION BY p.listener_id,p.broadcaster_id ORDER BY p.observed_at,p.id) AS previous_id,
        LAG(p.room_id) OVER (
        PARTITION BY p.listener_id,p.broadcaster_id ORDER BY p.observed_at,p.id) AS previous_room
      FROM points p), marked AS (
      SELECT p.*,CASE WHEN previous_at IS NULL OR
        previous_room<>room_id OR
        (julianday(observed_at)-julianday(previous_at))*86400> ?+0.001 OR
        EXISTS (SELECT 1 FROM snapshots gap WHERE gap.broadcaster_id=p.broadcaster_id
          AND gap.room_id=p.room_id AND gap.complete=1
          AND (gap.observed_at>p.previous_at OR
               (gap.observed_at=p.previous_at AND gap.id>p.previous_id))
          AND (gap.observed_at<p.observed_at OR
               (gap.observed_at=p.observed_at AND gap.id<p.id))
          AND NOT EXISTS (SELECT 1 FROM memberships gm
            WHERE gm.snapshot_id=gap.id AND gm.listener_id=p.listener_id))
        THEN 1 ELSE 0 END AS starts_session FROM ordered p), numbered AS (
      SELECT *,SUM(starts_session) OVER(PARTITION BY listener_id,broadcaster_id
        ORDER BY observed_at,id) AS session_number FROM marked), sessions AS (
      SELECT listener_id,broadcaster_id,room_id,session_number,
        MIN(observed_at) AS first_seen_at,MAX(observed_at) AS last_seen_at,
        MIN(complete) AS observation_range_complete,COUNT(*) AS session_observation_count,
        ROW_NUMBER() OVER(PARTITION BY listener_id,broadcaster_id
          ORDER BY session_number DESC) AS session_order
      FROM numbered GROUP BY listener_id,broadcaster_id,room_id,session_number)
      SELECT x.*,a.favorite_temperature FROM sessions x JOIN latest l
        ON l.listener_id=x.listener_id AND l.broadcaster_id=x.broadcaster_id
      LEFT JOIN user_attributes a ON a.snapshot_id=l.id AND a.user_id=l.listener_id
      WHERE x.session_order=1'''
    result={}
    parameters=[*user_ids,now.isoformat(timespec='microseconds')]
    if since is not None:
        parameters.append(since.isoformat(timespec='microseconds'))
    parameters.append(OBSERVATION_GAP_SECONDS)
    for row in conn.execute(sql,parameters):
        item=dict(row)
        item.pop('session_order');item.pop('session_number')
        item['observation_range_complete']=bool(item['observation_range_complete'])
        item['observed_from_at']=item['first_seen_at']
        item['observed_until_at']=item['last_seen_at']
        item['observation_range_note']=OBSERVATION_RANGE_NOTE
        if since is not None:
            item['observation_window_start_at']=since.isoformat(timespec='microseconds')
            item['observation_range_note']='直近30分の観測点の範囲です。連続した滞在時間ではありません。'
        result[(item['listener_id'],item['broadcaster_id'])]=item
    return result


def _add_destinations(conn, users, now, limit=5):
    """Batch compact previews; never make one profile/metrics request per user."""
    if not users:
        return
    by_id={user['id']:user for user in users}
    placeholders=','.join('?' for _ in users)
    month=now.astimezone(ZoneInfo('Asia/Tokyo')).strftime('%Y-%m')
    stamp=now.isoformat(timespec='microseconds')
    for user in users:
        user['live']=[];user['monthly']=[]
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='monthly_dj_snapshots'").fetchone():
        rows=conn.execute(f'''WITH latest AS (
          SELECT id,dj_id,observed_at,complete,ROW_NUMBER() OVER (
            PARTITION BY dj_id ORDER BY observed_at DESC,id DESC) AS rn
          FROM monthly_dj_snapshots WHERE month=? AND observed_at<=?), relations AS (
          SELECT l.listener_id,u.id AS user_id,u.name,u.tag,l.temperature,s.observed_at,s.complete,
            ROW_NUMBER() OVER(PARTITION BY l.listener_id ORDER BY l.temperature DESC,u.id) AS position
          FROM latest s JOIN monthly_dj_listeners l ON l.snapshot_id=s.id
          JOIN profile_users u ON u.id=s.dj_id WHERE s.rn=1 AND l.listener_id IN ({placeholders}))
          SELECT * FROM relations WHERE position<=? ORDER BY listener_id,position''',
          [month,stamp,*by_id,limit]).fetchall()
        for row in rows:
            item={key:row[key] for key in ('user_id','name','tag','temperature','observed_at','complete')}
            item['complete']=bool(item['complete'])
            by_id[row['listener_id']]['monthly'].append(item)
    recent_ids=[user['id'] for user in users if user.get('recent')]
    if not recent_ids:
        return
    since=now-timedelta(seconds=RECENT_SECONDS)
    sessions=_latest_observation_sessions(conn,recent_ids,now,since=since)
    cutoff=since.isoformat(timespec='microseconds')
    profiles={row['id']:dict(row) for row in conn.execute(
        'SELECT * FROM ('+_profiles(conn)+') WHERE id IN ('+
        ','.join('?' for _ in {key[1] for key in sessions})+')',
        list({key[1] for key in sessions}))} if sessions else {}
    for (listener_id,dj_id),session in sessions.items():
        if session['last_seen_at']<cutoff:
            continue
        profile=profiles.get(dj_id,{})
        item=dict(session,user_id=dj_id,name=profile.get('name','名前未取得'),tag=profile.get('tag'),
                  observed_at=session['last_seen_at'])
        by_id[listener_id]['live'].append(item)
    for user in users:
        user['live']=sorted(user['live'],key=lambda item:(item['observed_at'],item['user_id']),reverse=True)[:limit]


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


def _profiles(conn, target_sql=None):
    # Public search needs every profile; private lists only need their selected
    # identities. Filter both profile sources before the window merge and tags.
    stored=_PROFILE
    cached='SELECT id,name,tag,updated_at AS last_seen_at FROM profile_users'
    if target_sql is not None:
        stored+=f' WHERE u.id IN ({target_sql})'
        cached+=f' WHERE id IN ({target_sql})'
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='profile_users'").fetchone() is None:
        return stored
    return f'''SELECT id,name,tag,last_seen_at FROM (
      SELECT id,name,tag,last_seen_at,ROW_NUMBER() OVER(PARTITION BY id ORDER BY last_seen_at DESC) AS rn
      FROM (SELECT id,name,tag,last_seen_at FROM ({stored})
            UNION ALL {cached})
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
        conn.execute('BEGIN')
        row = conn.execute('SELECT * FROM ('+_profiles(conn)+') WHERE id=?',(user_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result['broadcasters'] = listener_broadcasters(conn,user_id)
        sessions=_latest_observation_sessions(conn,[user_id],datetime.now(timezone.utc))
        relations=[]
        for relation in result['broadcasters']:
            session=sessions.get((user_id,relation['user_id']))
            if session is None:  # Future-dated imports are not past observations.
                continue
            relation['all_time_first_seen_at']=relation['first_seen_at']
            relation['all_time_last_seen_at']=relation['last_seen_at']
            relation.update({key:value for key,value in session.items()
                             if key not in ('listener_id','broadcaster_id')})
            relations.append(relation)
        result['broadcasters']=relations
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
    """Batch actual listener sightings and compact destination previews."""
    if not isinstance(user_ids, list) or len(user_ids) > 100 or any(
        not isinstance(user_id, str) or not user_id.isascii() or not user_id.isdigit()
        or len(user_id) > 200 for user_id in user_ids
    ):
        raise ValueError('At most 100 numeric account IDs required')
    user_ids=list(dict.fromkeys(user_ids))
    now=datetime.now(timezone.utc)
    users=[]
    if user_ids:
        parameters=_activity_parameters(now)
        parameters.update({f'uid{i}':uid for i,uid in enumerate(user_ids)})
        target_sql=' UNION ALL '.join(f'SELECT :uid{i} AS id' for i in range(len(user_ids)))
        with _read(database) as conn:
            conn.execute('BEGIN')
            rows=conn.execute('WITH '+_activity_ctes(target_sql)+
                ', profiles AS ('+_profiles(conn,'SELECT id FROM target_ids')+') SELECT a.*,p.name,p.tag FROM activity_rows a '
                'LEFT JOIN profiles p ON p.id=a.id',parameters).fetchall()
            by_id={row['id']:dict(row) for row in rows}
            users=[by_id[uid] for uid in user_ids]
            for user in users:
                user['recent']=bool(user['recent']);user['current']=bool(user['current'])
                user.pop('activity_priority')
            _add_destinations(conn,users,now)
    counts={state:sum(user['activity_state']==state for user in users)
            for state in ('current','recent','registered')}
    return {'users':users,'checked_at':now.isoformat(),'recent_minutes':RECENT_SECONDS/60,
            'recent_seconds':RECENT_SECONDS,'current_seconds':CURRENT_SECONDS,
            'activity_counts':counts,'category_counts':counts,'destinations_limit':5}


def fan_owners(database, private_database=None):
    with _read(database, private_database) as conn:
        return [dict(row) for row in conn.execute('''SELECT o.*,
            (SELECT COUNT(*) FROM registered_fans f WHERE f.owner_id=o.id) AS fan_count
            FROM fan_owners o ORDER BY imported_at DESC,id''')]


def fan_destinations(database, owner_id, offset=0, limit=50, private_database=None, sort="recent", q="", activity="all"):
    """Private imported fans, grouped by fresh listener observations before pagination."""
    from .fans import numeric_id
    owner_id=numeric_id(owner_id)
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=50:
        raise ValueError('Invalid pagination')
    if not isinstance(q,str) or len(q)>200:
        raise ValueError('Search requires at most 200 characters')
    q=q.strip()
    if activity not in ('all','current','recent','registered'):
        raise ValueError('Invalid activity filter')
    orders={'recent':"last_live_at DESC,id",'activity':"last_live_at DESC,id",
            'oldest':"last_live_at IS NULL,last_live_at ASC,id",
            'name':"name COLLATE NOCASE ASC,id",'name_desc':"name COLLATE NOCASE DESC,id",
            'id':"LENGTH(id),id"}
    if sort not in orders: raise ValueError('Invalid sort')
    now=datetime.now(timezone.utc)
    month=now.astimezone(ZoneInfo('Asia/Tokyo')).strftime('%Y-%m')
    counts={state:0 for state in ('current','recent','registered')}
    metadata={'month':month,'q':q,'activity':activity,'checked_at':now.isoformat(),
              'recent_minutes':RECENT_SECONDS/60,'recent_seconds':RECENT_SECONDS,
              'current_seconds':CURRENT_SECONDS,'destinations_limit':5}
    with _read(database, private_database) as conn:
        conn.execute('BEGIN')
        owner=conn.execute('SELECT * FROM fan_owners WHERE id=?',(owner_id,)).fetchone()
        if owner is None:
            return dict(metadata,owner=None,fans=[],has_more=False,registered_count=0,total=0,
                        activity_counts=counts,category_counts=counts)
        registered_count=conn.execute('SELECT COUNT(*) FROM registered_fans WHERE owner_id=?',(owner_id,)).fetchone()[0]
        target_sql='SELECT user_id AS id FROM registered_fans WHERE owner_id=:owner'
        base='WITH '+_activity_ctes(target_sql)+', profiles AS ('+_profiles(conn,'SELECT id FROM target_ids')+'''), fan_rows AS (
          SELECT a.*,COALESCE(p.name,f.name,'名前未取得') AS name,COALESCE(p.tag,f.tag) AS tag
          FROM activity_rows a JOIN registered_fans f ON f.user_id=a.id AND f.owner_id=:owner
          LEFT JOIN profiles p ON p.id=f.user_id) '''
        parameters=dict(_activity_parameters(now),owner=owner_id)
        conditions=[]
        if q:
            conditions.append("(id LIKE :pattern ESCAPE '\\' OR name LIKE :pattern ESCAPE '\\' OR tag LIKE :pattern ESCAPE '\\')")
            parameters['pattern']='%'+_literal(q)+'%'
        if activity=='recent':
            conditions.append('recent=1')
        elif activity in ('current','registered'):
            conditions.append('activity_state=:activity')
            parameters['activity']=activity
        where=' WHERE '+' AND '.join(conditions) if conditions else ''
        # Count all matching categories and choose the page in one snapshot and
        # one execution of the observation joins. A normal poll must not repeat
        # the full listener history once for counts and once for the page.
        count_fields=','.join(
            f"SUM(CASE WHEN activity_state='{state}' THEN 1 ELSE 0 END) OVER() AS count_{state}"
            for state in counts)
        rows=conn.execute(base+'SELECT *,'+count_fields+' FROM fan_rows'+where+
                          f' ORDER BY activity_priority,{orders[sort]} LIMIT :limit OFFSET :offset',
                          dict(parameters,limit=limit+1,offset=offset)).fetchall()
        if rows:
            counts={state:rows[0]['count_'+state] for state in counts}
        elif offset:
            # Keep totals correct when a saved page becomes empty after fans
            # are removed or a search/filter narrows the matching population.
            for row in conn.execute(base+'SELECT activity_state,COUNT(*) AS count FROM fan_rows'+where+
                                    ' GROUP BY activity_state',parameters):
                counts[row['activity_state']]=row['count']
        total=sum(counts.values())
        users=[dict(row) for row in rows[:limit]]
        for user in users:
            for state in counts:
                user.pop('count_'+state)
            user['recent']=bool(user['recent']);user['current']=bool(user['current'])
            user.pop('activity_priority')
        _add_destinations(conn,users,now)
        indexed=(0,None)
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='monthly_dj_snapshots'").fetchone():
            indexed=conn.execute('SELECT COUNT(DISTINCT dj_id),MAX(observed_at) FROM monthly_dj_snapshots '
                'WHERE month=? AND observed_at<=?',(month,parameters['now'])).fetchone()
    return dict(metadata,owner=dict(owner),fans=users,has_more=len(rows)>limit,
                registered_count=registered_count,total=total,activity_counts=counts,category_counts=counts,
                indexed_djs=indexed[0],monthly_observed_at=indexed[1])
