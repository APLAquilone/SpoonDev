"""Saved own-room listener candidates; visiting never verifies a follow."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .fans import numeric_id
from . import webdata


def candidates(database, owner_id, *, private_database=None, source='observed',
               offset=0, limit=50, q='', sort='recent', activity='all', now=None):
    """Read a bound DJ's saved candidates without requiring a fan import.

    HTTP callers must enforce their account's profile binding before calling.
    Fan registrations are read only from the supplied private database. Monthly
    members follow the same positive-temperature definition as analytics.
    """
    owner_id = numeric_id(owner_id)
    if source not in ('observed', 'monthly'):
        raise ValueError('対象は自枠の観測または月間温度メンバーを指定してください。')
    if (type(offset) is not int or not 0 <= offset <= 1000000 or
            type(limit) is not int or not 1 <= limit <= 50):
        raise ValueError('Invalid pagination')
    if not isinstance(q, str) or len(q) > 200:
        raise ValueError('Search requires at most 200 characters')
    if activity not in ('all', 'current', 'recent', 'registered'):
        raise ValueError('Invalid activity filter')
    orders = {'recent': 'last_live_at DESC,id', 'activity': 'last_live_at DESC,id',
              'oldest': 'last_live_at IS NULL,last_live_at ASC,id', 'name': 'name COLLATE NOCASE,id',
              'name_desc': 'name COLLATE NOCASE DESC,id', 'id': 'LENGTH(id),id'}
    if sort not in orders:
        raise ValueError('Invalid sort')
    now = now or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now requires timezone')
    now = now.astimezone(timezone.utc)
    local = now.astimezone(ZoneInfo('Asia/Tokyo'))
    month = local.strftime('%Y-%m')
    month_start = datetime(local.year, local.month, 1, tzinfo=local.tzinfo).astimezone(
        timezone.utc).isoformat(timespec='microseconds')
    parameters = dict(webdata._activity_parameters(now), owner=owner_id,
                      month=month, month_start=month_start)
    counts = {state: 0 for state in ('current', 'recent', 'registered')}
    result = {'owner_id': owner_id, 'source': source, 'month': month,
              'state': 'not_collected', 'observed_at': None, 'complete': False,
              'checked_at': parameters['now'], 'q': q.strip(), 'activity': activity, 'users': [],
              'total': 0, 'has_more': False, 'activity_counts': counts,
              'category_counts': counts, 'recent_seconds': webdata.RECENT_SECONDS,
              'current_seconds': webdata.CURRENT_SECONDS, 'destinations_limit': 5,
              'current_window_basis': 'fixed_window',
              'classification': 'saved_observations_not_verified_followers'}

    def has_table(conn, name):
        return conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (name,)).fetchone() is not None

    with webdata._read(database, private_database) as conn:
        conn.execute('BEGIN')
        current_seconds = webdata._current_observation_window(conn, now)
        parameters.update(webdata._activity_parameters(now, current_seconds))
        result.update(current_seconds=current_seconds,
                      current_window_basis='observed_scan_cadence' if current_seconds > webdata.CURRENT_SECONDS
                      else 'fixed_window')
        if source == 'observed':
            if not has_table(conn, 'snapshots') or not has_table(conn, 'memberships'):
                return result
            state = conn.execute('''SELECT MAX(observed_at),MIN(complete),COUNT(*)
                FROM snapshots WHERE broadcaster_id=:owner AND observed_at>=:month_start
                AND observed_at<=:now''', parameters).fetchone()
            if not state[2]:
                return result
            observed_at, complete = state[0], bool(state[1])
            target_sql = '''SELECT DISTINCT m.listener_id AS id
                FROM snapshots s JOIN memberships m ON m.snapshot_id=s.id
                WHERE s.broadcaster_id=:owner AND s.observed_at>=:month_start
                AND s.observed_at<=:now'''
        else:
            if not has_table(conn, 'monthly_dj_snapshots') or not has_table(conn, 'monthly_dj_listeners'):
                return result
            snapshot = conn.execute('''SELECT id,observed_at,complete FROM monthly_dj_snapshots
                WHERE dj_id=:owner AND month=:month AND observed_at<=:now
                ORDER BY observed_at DESC,id DESC LIMIT 1''', parameters).fetchone()
            if snapshot is None:
                return result
            parameters['monthly_snapshot'] = snapshot['id']
            observed_at, complete = snapshot['observed_at'], bool(snapshot['complete'])
            target_sql = '''SELECT listener_id AS id FROM monthly_dj_listeners
                WHERE snapshot_id=:monthly_snapshot AND temperature>0'''
        result.update(observed_at=observed_at, complete=complete,
                      state='ready' if complete else 'partial')
        # Private fan tables are temporary views created by _read. Never join
        # a second account's list, or manufacture a registration for visitors.
        fan_table = conn.execute('''SELECT 1 FROM sqlite_temp_master WHERE name='registered_fans'
            UNION ALL SELECT 1 FROM sqlite_master WHERE name='registered_fans' LIMIT 1''').fetchone()
        registration = ('''EXISTS(SELECT 1 FROM registered_fans f
            WHERE f.owner_id=:owner AND f.user_id=a.id)''' if fan_table else '0')
        base = 'WITH ' + webdata._activity_ctes(
            target_sql, include_room_attempts=webdata._has_room_attempts(conn)) + ', profiles AS (' + webdata._profiles(
            conn, 'SELECT id FROM target_ids') + f'''), candidate_rows AS (
            SELECT a.*,COALESCE(p.name,'名前未取得') AS name,p.tag,
                {registration} AS registered_fan
            FROM activity_rows a LEFT JOIN profiles p ON p.id=a.id) '''
        conditions = []
        if result['q']:
            conditions.append("(id LIKE :pattern ESCAPE '\\' OR name LIKE :pattern ESCAPE '\\' OR tag LIKE :pattern ESCAPE '\\')")
            parameters['pattern'] = '%' + webdata._literal(result['q']) + '%'
        if activity == 'recent':
            conditions.append('recent=1')
        elif activity in ('current', 'registered'):
            conditions.append('activity_state=:activity')
            parameters['activity'] = activity
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        count_fields = ','.join(f"SUM(CASE WHEN activity_state='{state}' THEN 1 ELSE 0 END) OVER() AS count_{state}"
                                for state in counts)
        rows = conn.execute(base + 'SELECT *,' + count_fields + ' FROM candidate_rows' + where +
                            f' ORDER BY activity_priority,{orders[sort]} LIMIT :limit OFFSET :offset',
                            dict(parameters, limit=limit+1, offset=offset)).fetchall()
        if rows:
            counts = {state: rows[0]['count_'+state] for state in counts}
        elif offset:
            for row in conn.execute(base + 'SELECT activity_state,COUNT(*) AS count FROM candidate_rows' +
                                    where + ' GROUP BY activity_state', parameters):
                counts[row['activity_state']] = row['count']
        users = [dict(row) for row in rows[:limit]]
        for user in users:
            for state in counts:
                user.pop('count_'+state)
            user.pop('activity_priority')
            for key in ('current', 'recent', 'registered_fan'):
                user[key] = bool(user[key])
        webdata._add_destinations(conn, users, now)
        # A search with no match is not an empty source cohort. Only actual
        # saved source membership determines the source's ready/empty state.
        source_count = conn.execute('WITH target_ids AS (' + target_sql +
                                    ') SELECT COUNT(*) FROM target_ids', parameters).fetchone()[0]
    result.update(users=users, total=sum(counts.values()), has_more=len(rows)>limit,
                  activity_counts=counts, category_counts=counts, source_count=source_count)
    if not source_count and complete:
        result['state'] = 'empty'
    return result
