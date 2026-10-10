"""Listener activity across observed rooms, for an account's relevant listeners.

A cohort is selected from the linked DJ's fans, this month's own-room listeners,
and confirmed monthly-temperature members. Their activity is then counted across
all saved public room observations. This does not measure app opening/closing,
continuous listening duration, or the service's complete population.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .webdata import _read


_JAPAN = ZoneInfo("Asia/Tokyo")
_METRICS = ("all", "fans", "visitors", "monthly", "other", "overlap", "first", "repeat")
_NOTES = {
    "scope": "ご自身の登録ファン・今月の自枠リスナー・月間温度メンバーが、他枠も含めてリスナーとして観測された日時を集計しています。ご自身が配信した時間だけの集計ではありません。",
    "hourly": "日本時間の同じ日・時間帯で同じ人を1回だけ数えます。複数の枠や繰り返し取得で人数を増やしません。",
    "average": "互換用の時間帯集計は、人数を確認できた日の値だけを合計しています。未観測は0人として扱いません。",
    "totals": "期間内の人数は、対象期間全体で重複を除いた観測人数です。日・時間帯別の合計とは一致しません。",
    "cohorts": "登録ファンは現在のご自身の登録一覧、今月の自枠リスナーは今月ご自身の配信で観測された人、月間温度メンバーは今月の最新取得ランキングで温度が0より大きい人です。現在の分類を過去の観測にも適用します。",
    "overlap": "各対象には重複があります。対象別の人数を足して全体の人数として扱うことはできません。",
    "other": "その他は対象に含まれる人のうち、現在の登録ファン一覧に含まれない人です。実際にフォローしていないことや新規訪問を意味しません。",
    "first": "初観測・再観測は、ご自身の配信で保存された最初のリスナー観測日を基準にした互換用の分類です。初訪問ではなく、自枠で観測されていないファンはこの分類に含みません。",
    "unknown": "未観測の時間帯からSpoonを開いていないとは判断できません。対象者が観測されなかった時間は未観測のまま表示します。観測されていた時間帯も、連続して聴いていた時間や現在の聴取状態を示しません。部分取得の人数は確認できた範囲です。",
    "monthly": "月間ランキングが未取得の場合、月間温度メンバーの人数は不明です。部分取得では確認できたメンバーだけを対象にします。",
}


def _has_table(conn, name, schema="main"):
    return conn.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _linked_profile(conn, schema="main"):
    if not _has_table(conn, "account_settings", schema):
        return None
    row = conn.execute(f"""SELECT spoon_id,spoon_name,spoon_tag
        FROM {schema}.account_settings WHERE singleton=1""").fetchone()
    return ({"id": row["spoon_id"], "name": row["spoon_name"], "tag": row["spoon_tag"]}
            if row and row["spoon_id"] else None)


def _private_profile(private_database):
    if private_database is None:
        return None
    with _read(private_database) as conn:
        return _linked_profile(conn)


def _empty_bin(hour):
    return dict.fromkeys(_METRICS) | {
        "hour": hour, "snapshot_count": 0, "complete_snapshot_count": 0,
        "partial_snapshot_count": 0, "observed_days": 0, "known_days": 0, "partial": False,
    }


def _window(days, now):
    if type(days) is not int or days not in (7, 28):
        raise ValueError("days requires 7 or 28")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now requires timezone")
    now = now.astimezone(timezone.utc)
    local = now.astimezone(_JAPAN)
    first_day = local.date() - timedelta(days=days-1)
    start = datetime.combine(first_day, datetime.min.time(), tzinfo=_JAPAN)
    return (now.isoformat(timespec="microseconds"), local, first_day,
            start.astimezone(timezone.utc).isoformat(timespec="microseconds"))


def _result(days, now, *, profile=None, individual=False):
    stamp, local, first_day, start_stamp = _window(days, now)
    result = {
        "state": "not_collected" if profile is not None else "needs_profile",
        "mode": "individual" if individual else "cohort",
        "scope": "individual_across_rooms" if individual else "cohort_across_rooms",
        "profile": profile, "checked_at": stamp,
        "window": {"days": days, "start_date": first_day.isoformat(),
                   "end_date": local.date().isoformat(), "timezone": "Asia/Tokyo"},
        "monthly": {"state": "not_collected", "month": local.strftime("%Y-%m"),
                    "observed_at": None, "complete": False, "listener_count": 0,
                    "confirmed_listener_count": None},
        "cohorts": {"registered_fan_count": 0, "monthly_visitor_count": 0,
                    "monthly_confirmed_count": None, "cohort_count": 0,
                    "overlap_count": None, "classification": "current"},
        "totals": dict.fromkeys(_METRICS) | {
            "snapshot_count": 0, "complete_snapshot_count": 0, "partial_snapshot_count": 0,
            "observed_days": 0, "positive_days": 0, "last_observed_at": None,
        },
        "hourly": [_empty_bin(hour) for hour in range(24)],
        "daily": [{"date": (first_day+timedelta(days=i)).isoformat(),
                   "hours": [_empty_bin(hour) for hour in range(24)]} for i in range(days)],
        "notes": dict(_NOTES),
    }
    if individual:
        result["notes"]["scope"] = "このユーザーが、他枠も含めてリスナーとして観測された日時です。配信していた日時ではありません。"
    return result, stamp, local, first_day, start_stamp


def _monthly(conn, broadcaster_id, month, stamp):
    result = {"state": "not_collected", "month": month, "observed_at": None,
              "complete": False, "listener_count": 0, "confirmed_listener_count": None}
    if not _has_table(conn, "monthly_dj_snapshots") or not _has_table(conn, "monthly_dj_listeners"):
        return result, None, set()
    snapshot = conn.execute("""SELECT id,observed_at,complete FROM monthly_dj_snapshots
        WHERE dj_id=? AND month=? AND observed_at<=?
        ORDER BY observed_at DESC,id DESC LIMIT 1""", (broadcaster_id, month, stamp)).fetchone()
    if snapshot is None:
        return result, None, set()
    count = conn.execute("SELECT COUNT(*) FROM monthly_dj_listeners WHERE snapshot_id=?",
                         (snapshot["id"],)).fetchone()[0]
    members = {row[0] for row in conn.execute("""SELECT listener_id FROM monthly_dj_listeners
        WHERE snapshot_id=? AND temperature>0""", (snapshot["id"],))}
    result.update(state="partial" if not snapshot["complete"] else "ready" if count else "empty",
                  observed_at=snapshot["observed_at"], complete=bool(snapshot["complete"]),
                  listener_count=count, confirmed_listener_count=len(members))
    return result, snapshot["id"], members


_COUNTS = """COUNT(DISTINCT listener_id) AS all_count,
    COUNT(DISTINCT CASE WHEN fan_member THEN listener_id END) AS fans_count,
    COUNT(DISTINCT CASE WHEN visitor_member THEN listener_id END) AS visitors_count,
    COUNT(DISTINCT CASE WHEN monthly_member THEN listener_id END) AS monthly_count,
    COUNT(DISTINCT CASE WHEN NOT fan_member THEN listener_id END) AS other_count,
    COUNT(DISTINCT CASE WHEN fan_member AND monthly_member THEN listener_id END) AS overlap_count,
    COUNT(DISTINCT id) AS snapshot_count,
    COUNT(DISTINCT CASE WHEN complete THEN id END) AS complete_snapshot_count,
    COUNT(DISTINCT CASE WHEN NOT complete THEN id END) AS partial_snapshot_count"""


def _aggregate(conn, result, ctes, parameters, start_stamp, stamp, *, monthly_known=False):
    """Count matching sightings without publishing unrelated system coverage.

    A complete saved room sample does not imply complete knowledge of a listener.
    Cells with no matching sighting stay unknown, including hours when unrelated
    rooms were sampled successfully. Counts and partial status belong only to
    samples containing the selected listeners.
    """
    rows = conn.execute(ctes + f"""SELECT day,hour,{_COUNTS},
        COUNT(DISTINCT CASE WHEN first_day=day THEN listener_id END) AS first_count,
        COUNT(DISTINCT CASE WHEN first_day<day THEN listener_id END) AS repeat_count
        FROM sightings GROUP BY day,hour""", parameters).fetchall()
    first_day = result["window"]["start_date"]
    totals = conn.execute(ctes + f"""SELECT {_COUNTS},
        COUNT(DISTINCT CASE WHEN first_day>=? THEN listener_id END) AS first_count,
        COUNT(DISTINCT CASE WHEN first_day<? THEN listener_id END) AS repeat_count,
        COUNT(DISTINCT day) AS positive_days,MAX(observed_at) AS last_observed_at
        FROM sightings""", parameters + [first_day, first_day]).fetchone()

    def values(row):
        return {key: (row[key+"_count"] if row["all_count"] > 0 and
                      (monthly_known or key not in ("monthly", "overlap")) else None)
                for key in _METRICS}

    result["totals"].update(values(totals), positive_days=totals["positive_days"],
                            observed_days=totals["positive_days"],
                            last_observed_at=totals["last_observed_at"])
    for key in ("snapshot_count", "complete_snapshot_count", "partial_snapshot_count"):
        result["totals"][key] = totals[key]
    by_day = {day["date"]: day for day in result["daily"]}
    for row in rows:
        cell = by_day[row["day"]]["hours"][row["hour"]]
        cell.update(values(row), observed_days=1, known_days=1,
                    partial=bool(row["partial_snapshot_count"]))
        for key in ("snapshot_count", "complete_snapshot_count", "partial_snapshot_count"):
            cell[key] = row[key]
    if rows:
        result["state"] = "partial" if totals["partial_snapshot_count"] else "ready"
    elif conn.execute("SELECT 1 FROM snapshots WHERE observed_at>=? AND observed_at<=? LIMIT 1",
                      (start_stamp, stamp)).fetchone():
        # Existence only: an ordinary account must not receive another cohort's
        # global collection counts or unrelated partial/failed-room status.
        result["state"] = "not_observed"
    for day in result["daily"]:
        for cell in day["hours"]:
            hour = result["hourly"][cell["hour"]]
            for key in _METRICS:
                if cell[key] is not None:
                    hour[key] = (hour[key] or 0) + cell[key]
            for key in ("snapshot_count", "complete_snapshot_count", "partial_snapshot_count", "observed_days", "known_days"):
                hour[key] += cell[key]
            hour["partial"] = hour["partial"] or cell["partial"]
    return result


def summary(database, private_database=None, *, days=7, now=None):
    """Select an account-local cohort, then count its activity across all DJs."""
    # Validate before opening either database. An unlinked account must not
    # inherit a legacy fan owner, or even open the public observation database.
    _window(days, now)
    profile = _private_profile(private_database)
    result, stamp, local, _, start_stamp = _result(days, now, profile=profile)
    if profile is None:
        return result
    with _read(database, private_database) as conn:
        conn.execute("BEGIN")
        # The same attached read transaction supplies the binding and fan list.
        # A concurrent admin rebind cannot mix another DJ with this fan cohort.
        profile = _linked_profile(conn, "private")
        result["profile"] = profile
        if profile is None:
            result["state"] = "needs_profile"
            return result
        fans_available = _has_table(conn, "registered_fans", "private")
        registered_fans = {row[0] for row in conn.execute(
            "SELECT user_id FROM private.registered_fans WHERE owner_id=?", (profile["id"],)
        )} if fans_available else set()
        result["cohorts"]["registered_fan_count"] = len(registered_fans)
        monthly, monthly_snapshot, monthly_members = _monthly(
            conn, profile["id"], result["monthly"]["month"], stamp)
        result["monthly"] = monthly
        if monthly_snapshot is not None:
            result["cohorts"].update(monthly_confirmed_count=len(monthly_members),
                                    overlap_count=len(registered_fans & monthly_members))
        if not _has_table(conn, "snapshots") or not _has_table(conn, "memberships"):
            result["cohorts"]["cohort_count"] = len(registered_fans | monthly_members)
            return result
        month_start = datetime(local.year, local.month, 1, tzinfo=_JAPAN).astimezone(
            timezone.utc).isoformat(timespec="microseconds")
        visitors = {row[0] for row in conn.execute("""SELECT DISTINCT m.listener_id
            FROM snapshots s JOIN memberships m ON m.snapshot_id=s.id
            WHERE s.broadcaster_id=? AND s.observed_at>=? AND s.observed_at<=?""",
            (profile["id"], month_start, stamp))}
        result["cohorts"].update(monthly_visitor_count=len(visitors),
                                cohort_count=len(registered_fans | visitors | monthly_members))
        # SQL-built cohort avoids a variable-length IN list or temporary writes;
        # account fan lists can be large, while this connection stays query-only.
        parts = []
        parameters = []
        if fans_available:
            parts.append("SELECT user_id AS listener_id,1 AS fan_member,0 AS visitor_member,0 AS monthly_member FROM private.registered_fans WHERE owner_id=?")
            parameters.append(profile["id"])
        parts.append("""SELECT DISTINCT m.listener_id,0,1,0 FROM snapshots s
            JOIN memberships m ON m.snapshot_id=s.id
            WHERE s.broadcaster_id=? AND s.observed_at>=? AND s.observed_at<=?""")
        parameters.extend((profile["id"], month_start, stamp))
        if monthly_snapshot is not None:
            parts.append("SELECT listener_id,0,0,1 FROM monthly_dj_listeners WHERE snapshot_id=? AND temperature>0")
            parameters.append(monthly_snapshot)
        ctes = "WITH cohort_entries(listener_id,fan_member,visitor_member,monthly_member) AS (" + " UNION ALL ".join(parts) + """),
            cohort AS (SELECT listener_id,MAX(fan_member) AS fan_member,
                MAX(visitor_member) AS visitor_member,MAX(monthly_member) AS monthly_member
                FROM cohort_entries GROUP BY listener_id),
            first_seen AS (SELECT m.listener_id,date(MIN(s.observed_at),'+9 hours') AS first_day
                FROM snapshots s JOIN memberships m ON m.snapshot_id=s.id
                JOIN cohort c ON c.listener_id=m.listener_id
                WHERE s.broadcaster_id=? AND s.observed_at<=? GROUP BY m.listener_id),
            sightings AS (SELECT s.id,s.observed_at,s.complete,m.listener_id,
                date(s.observed_at,'+9 hours') AS day,
                CAST(strftime('%H',s.observed_at,'+9 hours') AS INTEGER) AS hour,
                fs.first_day,c.fan_member,c.visitor_member,c.monthly_member
                FROM cohort c JOIN memberships m ON m.listener_id=c.listener_id
                JOIN snapshots s ON s.id=m.snapshot_id
                LEFT JOIN first_seen fs ON fs.listener_id=m.listener_id
                WHERE s.observed_at>=? AND s.observed_at<=?) """
        parameters.extend((profile["id"], stamp, start_stamp, stamp))
        return _aggregate(conn, result, ctes, parameters, start_stamp, stamp,
                          monthly_known=monthly_snapshot is not None)


def user_activity(database, user_id, *, days=7, now=None):
    """Return a public listener's observed activity, without any private cohort."""
    if (not isinstance(user_id, str) or not user_id.isascii() or not user_id.isdigit()
            or len(user_id) > 200 or not user_id.strip("0")):
        raise ValueError("user_id requires a positive numeric account ID")
    result, stamp, _, _, start_stamp = _result(
        days, now, profile={"id": user_id, "name": None, "tag": None}, individual=True)
    result.pop("cohorts")
    with _read(database) as conn:
        conn.execute("BEGIN")
        # Public profile labels are optional; only listener membership determines
        # activity. Being the broadcaster never counts as listening here.
        labels = []
        if _has_table(conn, "users"):
            labels.extend(conn.execute("SELECT name,NULL AS tag,name_observed_at AS stamp FROM users WHERE id=?",
                                       (user_id,)).fetchall())
        if _has_table(conn, "profile_users"):
            labels.extend(conn.execute("SELECT name,tag,updated_at AS stamp FROM profile_users WHERE id=?",
                                       (user_id,)).fetchall())
        if not labels:
            return None
        eligible_labels = [row for row in labels if row["stamp"] <= stamp]
        if eligible_labels:
            latest = max(eligible_labels, key=lambda row: row["stamp"])
            result["profile"].update(name=latest["name"], tag=latest["tag"])
        if not _has_table(conn, "snapshots") or not _has_table(conn, "memberships"):
            return result
        ctes = """WITH first_seen AS (
            SELECT date(MIN(s.observed_at),'+9 hours') AS first_day
            FROM memberships m JOIN snapshots s ON s.id=m.snapshot_id
            WHERE m.listener_id=? AND s.observed_at<=?
        ), sightings AS (
            SELECT s.id,s.observed_at,s.complete,m.listener_id,
                date(s.observed_at,'+9 hours') AS day,
                CAST(strftime('%H',s.observed_at,'+9 hours') AS INTEGER) AS hour,
                fs.first_day,0 AS fan_member,0 AS visitor_member,0 AS monthly_member
            FROM memberships m JOIN snapshots s ON s.id=m.snapshot_id
            CROSS JOIN first_seen fs
            WHERE m.listener_id=? AND s.observed_at>=? AND s.observed_at<=?) """
        return _aggregate(conn, result, ctes, [user_id, stamp, user_id, start_stamp, stamp],
                          start_stamp, stamp)
