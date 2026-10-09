"""Read-only broadcaster dashboard for the current account's linked profile.

An observed listener is evidence of a sighting, never proof of a first visit or
current presence. Temperatures are kept separate from monthly rankings and gifts.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .webdata import _read


_JAPAN = ZoneInfo("Asia/Tokyo")
_LISTENER_LIMIT = 20
_NOTES = {
    "first_observed": "初観測は、この配信者への保存済みライブ観測で初めて見つかった日時です。初訪問を意味しません。",
    "average_temperature": "観測平均温度は、この配信者で保存された温度のあるライブ観測の平均です。月間温度とは別の値です。",
    "live": "ライブ観測は収集時の記録です。現在の視聴状態や実際の訪問回数ではありません。",
    "monthly": "月間温度は今月の取得済み配信者ランキングです。取得日時と部分取得の状態を確認してください。",
    "gifts": "Spoon数は取得できたランキングの値です。集計期間が確認できない場合、月間・生涯の総投げ額とは扱いません。",
}


def _has_table(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _settings(private_database):
    """Read only this account's settings; legacy imported owners never bind it."""
    profile = None
    updated_at = None
    counts = {"favorites": 0, "registered_fans": 0, "fan_lists": 0}
    if private_database is None:
        return profile, updated_at, counts
    with _read(private_database) as conn:
        if _has_table(conn, "account_settings"):
            row = conn.execute(
                "SELECT spoon_id,spoon_name,spoon_tag,updated_at FROM account_settings WHERE singleton=1"
            ).fetchone()
            if row:
                updated_at = row["updated_at"]
                if row["spoon_id"]:
                    profile = {"id": row["spoon_id"], "name": row["spoon_name"], "tag": row["spoon_tag"]}
        if _has_table(conn, "favorites"):
            counts["favorites"] = conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0]
        if _has_table(conn, "fan_owners"):
            counts["fan_lists"] = conn.execute("SELECT COUNT(*) FROM fan_owners").fetchone()[0]
        if profile and _has_table(conn, "registered_fans"):
            counts["registered_fans"] = conn.execute(
                "SELECT COUNT(*) FROM registered_fans WHERE owner_id=?", (profile["id"],)
            ).fetchone()[0]
    return profile, updated_at, counts


def _listeners(conn, broadcaster_id, stamp, recent_cutoff, week_start, latest_snapshot):
    """Aggregate sightings only of the account's explicitly linked broadcaster."""
    parameters = (broadcaster_id, stamp, latest_snapshot, recent_cutoff, week_start)
    ctes = """WITH own_sightings AS (
        SELECT m.listener_id,s.id AS snapshot_id,s.observed_at,n.name,a.tag,a.favorite_temperature,
            ROW_NUMBER() OVER(PARTITION BY m.listener_id ORDER BY s.observed_at DESC,s.id DESC) AS latest
        FROM snapshots s JOIN memberships m ON m.snapshot_id=s.id
        LEFT JOIN names n ON n.snapshot_id=s.id AND n.user_id=m.listener_id
        LEFT JOIN user_attributes a ON a.snapshot_id=s.id AND a.user_id=m.listener_id
        WHERE s.broadcaster_id=? AND s.observed_at<=?
    ), totals AS (
        SELECT listener_id,COUNT(*) AS observation_count,COUNT(favorite_temperature) AS temperature_observation_count,
            AVG(favorite_temperature) AS observed_average_temperature,
            MIN(observed_at) AS first_seen_at,MAX(observed_at) AS last_seen_at,
            MAX(snapshot_id=?) AS in_latest_snapshot
        FROM own_sightings GROUP BY listener_id
    ), listeners AS (
        SELECT t.listener_id AS id,COALESCE(l.name,u.name,'名前未取得') AS name,l.tag,
            l.favorite_temperature AS latest_temperature,
            t.observation_count,t.temperature_observation_count,t.observed_average_temperature,
            t.first_seen_at,t.last_seen_at,t.in_latest_snapshot,
            t.last_seen_at>=? AS recent,t.first_seen_at>=? AS newly_observed
        FROM totals t JOIN own_sightings l ON l.listener_id=t.listener_id AND l.latest=1
        LEFT JOIN users u ON u.id=t.listener_id
    ) """
    # Every list is capped before rows are materialized. Counts retain full scope.
    counts = conn.execute(
        ctes + "SELECT COUNT(*),COALESCE(SUM(recent),0),COALESCE(SUM(newly_observed),0) FROM listeners",
        parameters,
    ).fetchone()
    lists = {}
    for key, condition, order in (
        ("latest", "in_latest_snapshot=1", "last_seen_at DESC,id"),
        ("recent", "recent=1", "last_seen_at DESC,id"),
        ("new", "newly_observed=1", "first_seen_at DESC,id"),
    ):
        rows = conn.execute(
            ctes + f"SELECT * FROM listeners WHERE {condition} ORDER BY {order} LIMIT ?",
            parameters + (_LISTENER_LIMIT,),
        ).fetchall()
        users = []
        for row in rows:
            user = dict(row)
            user.pop("in_latest_snapshot")
            user["recent"] = bool(user["recent"])
            user["newly_observed"] = bool(user["newly_observed"])
            if user["observed_average_temperature"] is not None:
                user["observed_average_temperature"] = round(user["observed_average_temperature"], 2)
            users.append(user)
        lists[key] = users
    return counts, lists


def _monthly(conn, broadcaster_id, month, stamp):
    result = {"state": "not_collected", "month": month, "observed_at": None,
              "complete": False, "listener_count": 0, "listeners": []}
    if not _has_table(conn, "monthly_dj_snapshots"):
        return result
    snapshot = conn.execute("""SELECT id,observed_at,complete FROM monthly_dj_snapshots
        WHERE dj_id=? AND month=? AND observed_at<=? ORDER BY observed_at DESC,id DESC LIMIT 1""",
        (broadcaster_id, month, stamp)).fetchone()
    if snapshot is None:
        return result
    total = conn.execute("SELECT COUNT(*) FROM monthly_dj_listeners WHERE snapshot_id=?",
                         (snapshot["id"],)).fetchone()[0]
    rows = conn.execute("""SELECT m.listener_id AS id,u.name,u.tag,m.temperature
        FROM monthly_dj_listeners m JOIN profile_users u ON u.id=m.listener_id
        WHERE m.snapshot_id=? ORDER BY m.temperature IS NULL,m.temperature DESC,m.listener_id LIMIT ?""",
        (snapshot["id"], _LISTENER_LIMIT)).fetchall()
    result.update(state=("partial" if not snapshot["complete"] else "ready" if total else "empty"),
                  observed_at=snapshot["observed_at"], complete=bool(snapshot["complete"]),
                  listener_count=total, listeners=[dict(row) for row in rows])
    return result


def _gifts(conn, broadcaster_id, stamp):
    result = {"state": "not_collected", "observed_at": None, "complete": False,
              "source_period": "unspecified", "unit": "spoon", "listener_count": 0, "listeners": []}
    if not _has_table(conn, "gift_dj_snapshots"):
        return result, {}
    from .gifts import read_ranking
    snapshot = read_ranking(conn, broadcaster_id, observed_before=stamp)
    if snapshot is None:
        return result, {}
    rows = snapshot["rows"]
    result.update(state=("partial" if not snapshot["complete"] else "ready" if rows else "empty"),
                  observed_at=snapshot["observed_at"], complete=bool(snapshot["complete"]),
                  source_period=snapshot["source_period"], listener_count=len(rows),
                  observed_total_spoon=sum(r['total_spoon'] for r in rows) if rows and all(r['total_spoon'] is not None for r in rows) else None,
                  listeners=[{"id": row["user_id"], "name": row["name"], "tag": row["tag"],
                              "total_spoon": row["total_spoon"]} for row in rows[:_LISTENER_LIMIT]])
    return result, {row["user_id"]: row["total_spoon"] for row in rows}


def summary(database, private_database=None, *, now=None):
    """Return a bounded personal dashboard without exposing platform-wide stats."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now requires timezone")
    now = now.astimezone(timezone.utc)
    stamp = now.isoformat(timespec="microseconds")
    local = now.astimezone(_JAPAN)
    month = local.strftime("%Y-%m")
    first_day = local.date() - timedelta(days=6)
    week_start = datetime.combine(first_day, datetime.min.time(), tzinfo=_JAPAN).astimezone(timezone.utc)
    recent_cutoff = (now-timedelta(minutes=30)).isoformat(timespec="microseconds")
    profile, settings_at, private_counts = _settings(private_database)
    result = {
        "state": "ready" if profile else "needs_profile", "profile": profile,
        "profile_updated_at": settings_at, "checked_at": stamp, "month": month,
        "recent_minutes": 30, "new_listener_days": 7, "listener_limit": _LISTENER_LIMIT,
        "counts": dict(private_counts, observed_listeners=0, recent_listeners=0, new_listeners_7d=0),
        "live": {"state": "not_collected", "last_observed_at": None, "complete": False,
                 "recent": False,
                 "latest_listener_count": 0, "listener_count": 0, "listeners": []},
        "monthly": {"state": "not_collected", "month": month, "observed_at": None,
                    "complete": False, "listener_count": 0, "listeners": []},
        "gifts": {"state": "not_collected", "observed_at": None, "complete": False,
                  "source_period": "unspecified", "unit": "spoon", "listener_count": 0, "listeners": []},
        "recent_listeners": [], "newly_observed_listeners": [],
        "daily": [{"date": (first_day+timedelta(days=i)).isoformat(), "listener_count": 0,
                   "new_listener_count": 0, "snapshot_count": 0} for i in range(7)],
        "notes": dict(_NOTES),
    }
    if not profile:
        return result

    broadcaster_id = profile["id"]
    with _read(database) as conn:
        latest = conn.execute("""SELECT id,observed_at,complete FROM snapshots
            WHERE broadcaster_id=? AND observed_at<=? ORDER BY observed_at DESC,id DESC LIMIT 1""",
            (broadcaster_id, stamp)).fetchone()
        if latest:
            counts, users = _listeners(conn, broadcaster_id, stamp, recent_cutoff,
                                       week_start.isoformat(timespec="microseconds"), latest["id"])
            latest_total = conn.execute("SELECT COUNT(*) FROM memberships WHERE snapshot_id=?",
                                        (latest["id"],)).fetchone()[0]
            result["counts"].update(observed_listeners=counts[0], recent_listeners=counts[1], new_listeners_7d=counts[2])
            result["live"].update(
                state=("partial" if not latest["complete"] else "recent" if latest["observed_at"] >= recent_cutoff else "stale"),
                last_observed_at=latest["observed_at"], complete=bool(latest["complete"]),
                recent=latest["observed_at"] >= recent_cutoff,
                latest_listener_count=latest_total, listener_count=counts[0], listeners=users["latest"],
            )
            result["recent_listeners"] = users["recent"]
            result["newly_observed_listeners"] = users["new"]
            by_day = {day["date"]: day for day in result["daily"]}
            rows = conn.execute("""SELECT date(s.observed_at,'+9 hours') AS day,
                COUNT(DISTINCT m.listener_id) AS listeners,COUNT(DISTINCT s.id) AS snapshots
                FROM snapshots s LEFT JOIN memberships m ON m.snapshot_id=s.id
                WHERE s.broadcaster_id=? AND s.observed_at>=? AND s.observed_at<=? GROUP BY day""",
                (broadcaster_id, week_start.isoformat(timespec="microseconds"), stamp)).fetchall()
            for row in rows:
                if row["day"] in by_day:
                    by_day[row["day"]].update(listener_count=row["listeners"], snapshot_count=row["snapshots"])
            rows = conn.execute("""SELECT date(first_seen,'+9 hours') AS day,COUNT(*) AS listeners FROM (
                SELECT m.listener_id,MIN(s.observed_at) AS first_seen FROM snapshots s
                JOIN memberships m ON m.snapshot_id=s.id WHERE s.broadcaster_id=? AND s.observed_at<=?
                GROUP BY m.listener_id) WHERE first_seen>=? GROUP BY day""",
                (broadcaster_id, stamp, week_start.isoformat(timespec="microseconds"))).fetchall()
            for row in rows:
                if row["day"] in by_day:
                    by_day[row["day"]]["new_listener_count"] = row["listeners"]
        result["monthly"] = _monthly(conn, broadcaster_id, month, stamp)
        result["gifts"], gift_amounts = _gifts(conn, broadcaster_id, stamp)
        for users in (result["live"]["listeners"], result["recent_listeners"], result["newly_observed_listeners"]):
            for user in users:
                user["total_spoon"] = gift_amounts.get(user["id"])
        # A connected ID can be saved before its name is fetched. Use public data
        # for that exact profile only, without changing its private association.
        if not profile["name"] and _has_table(conn, "profile_users"):
            row = conn.execute("SELECT name,tag FROM profile_users WHERE id=?", (broadcaster_id,)).fetchone()
            if row:
                result["profile"] = dict(profile, name=row["name"], tag=profile["tag"] or row["tag"])
        if not result["profile"]["name"]:
            row = conn.execute("SELECT name FROM users WHERE id=?", (broadcaster_id,)).fetchone()
            if row:
                result["profile"] = dict(result["profile"], name=row["name"])
        from .worker import read_status, read_jobs
        status = read_status(conn)
        # Public worker status is useful; process IDs and other users' targets are not.
        result["collection"] = {"status": {key: status[key] for key in
            ("state", "worker_running", "heartbeat_at", "cooldown_until")},
            "jobs": read_jobs(conn, dj_ids=[broadcaster_id])}
        for job in result["collection"]["jobs"]:
            source = result.get("monthly" if job["kind"] == "monthly" else "gifts")
            if source is not None and job["kind"] in ("monthly", "gifts"):
                source["latest_attempt"] = job["last_attempt"]
                source["next_due_at"] = job["next_due_at"]
                source["collection_state"] = job["state"]
    # Shared, as-of listener metrics are independent of the DJ's average temperature.
    from .insights import listener_insights
    groups = (result["live"]["listeners"], result["recent_listeners"],
              result["newly_observed_listeners"], result["monthly"]["listeners"],
              result["gifts"]["listeners"])
    ids = list(dict.fromkeys(str(u.get("id") or u.get("user_id")) for group in groups for u in group))
    metrics = {}
    for start in range(0, len(ids), 100):
        metrics.update(listener_insights(database, ids[start:start+100],
                                        broadcaster_id=broadcaster_id, now=now))
    for group in groups:
        for user in group:
            user["insights"] = metrics.get(str(user.get("id") or user.get("user_id")))
    return result
