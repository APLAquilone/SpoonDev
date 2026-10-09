"""Personal hourly observations, with current fan and monthly-temperature cohorts.

The charts describe saved sightings in the account's own rooms. They do not
measure actual visits, viewing duration, or current presence. A user is counted
once per Japan calendar day and hour regardless of collection frequency.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .webdata import _read


_JAPAN = ZoneInfo("Asia/Tokyo")
_METRICS = ("all", "fans", "monthly", "other", "overlap", "first", "repeat")
_NOTES = {
    "scope": "連携したご自身の配信で保存されたライブ観測を集計しています。現在の視聴状態や実際の訪問回数ではありません。",
    "hourly": "時間帯別は、日本時間の同じ日・時間帯で同じ人を1回だけ数えた延べ観測人数です。収集回数を人数として加算しません。",
    "average": "観測日平均は延べ観測人数を、人数を確認できた観測日数で割った値です。未観測や部分取得のみで人数不明の日は分母に含めません。部分取得で確認できた人数は下限です。",
    "totals": "期間内の人数は、対象期間全体で重複を除いた観測人数です。時間帯別の合計とは一致しません。",
    "cohorts": "ファンは現在登録されているご自身のファン一覧、月間温度メンバーは今月の最新取得ランキングで温度が0より大きい人です。現在の分類を過去の観測にも適用しています。",
    "overlap": "ファンと月間温度メンバーは重複します。両者を足して全体の人数として扱うことはできません。",
    "other": "その他は登録ファン・確認済み月間温度メンバーのいずれにも含まれない人です。実際にフォローしていないことや新規訪問を意味しません。",
    "first": "初観測はご自身の配信で保存された最初の観測日です。初訪問ではありません。日・時間帯別はその日が初観測日の人、期間全体は期間内に初観測された人を数えます。",
    "unknown": "未観測の時間帯は未観測として表示します。部分取得のみで人が見つからなかった場合も0人とは扱いません。部分取得の人数は確認できた範囲です。",
    "monthly": "月間ランキングが未取得の場合、月間温度メンバーの人数は不明です。部分取得では確認できたメンバーだけを集計します。",
}


def _has_table(conn, name, schema="main"):
    return conn.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _private_profile(private_database):
    if private_database is None:
        return None, set()
    with _read(private_database) as conn:
        if not _has_table(conn, "account_settings"):
            return None, set()
        row = conn.execute("""SELECT spoon_id,spoon_name,spoon_tag
            FROM account_settings WHERE singleton=1""").fetchone()
        if not row or not row["spoon_id"]:
            return None, set()
        profile = {"id": row["spoon_id"], "name": row["spoon_name"], "tag": row["spoon_tag"]}
        fans = set()
        if _has_table(conn, "registered_fans"):
            fans = {row[0] for row in conn.execute(
                "SELECT user_id FROM registered_fans WHERE owner_id=?", (profile["id"],)
            )}
        return profile, fans


def _empty_bin(hour):
    return dict.fromkeys(_METRICS) | {
        "hour": hour, "snapshot_count": 0, "complete_snapshot_count": 0,
        "partial_snapshot_count": 0, "observed_days": 0, "known_days": 0, "partial": False,
    }


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


def summary(database, private_database=None, *, days=7, now=None):
    """Return only the current account's linked DJ, reading both databases.

    ``hourly`` sums daily unique listeners; ``totals`` deduplicates the entire
    period. Unknown and partial-empty cells keep ``None`` instead of inventing
    zero. Fan and monthly counts are independent, overlapping cohorts.
    """
    if type(days) is not int or days not in (7, 28):
        raise ValueError("days requires 7 or 28")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now requires timezone")
    now = now.astimezone(timezone.utc)
    stamp = now.isoformat(timespec="microseconds")
    local = now.astimezone(_JAPAN)
    first_day = local.date() - timedelta(days=days-1)
    start = datetime.combine(first_day, datetime.min.time(), tzinfo=_JAPAN)
    start_stamp = start.astimezone(timezone.utc).isoformat(timespec="microseconds")
    profile, registered_fans = _private_profile(private_database)
    result = {
        "state": "needs_profile" if profile is None else "not_collected",
        "profile": profile, "checked_at": stamp,
        "window": {"days": days, "start_date": first_day.isoformat(),
                   "end_date": local.date().isoformat(), "timezone": "Asia/Tokyo"},
        "monthly": {"state": "not_collected", "month": local.strftime("%Y-%m"),
                    "observed_at": None, "complete": False, "listener_count": 0,
                    "confirmed_listener_count": None},
        "cohorts": {"registered_fan_count": len(registered_fans), "monthly_confirmed_count": None,
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
    if profile is None:
        return result

    with _read(database, private_database) as conn:
        # Pin each database's read snapshot so concurrent collection/imports
        # cannot make the hourly rows, period totals, and cohort counts disagree.
        conn.execute("BEGIN")
        fans_available = private_database is not None and _has_table(conn, "registered_fans", "private")
        registered_fans = {row[0] for row in conn.execute(
            "SELECT user_id FROM private.registered_fans WHERE owner_id=?", (profile["id"],)
        )} if fans_available else set()
        result["cohorts"]["registered_fan_count"] = len(registered_fans)
        monthly, monthly_snapshot, monthly_members = _monthly(
            conn, profile["id"], result["monthly"]["month"], stamp)
        result["monthly"] = monthly
        monthly_known = monthly_snapshot is not None
        if monthly_known:
            result["cohorts"].update(monthly_confirmed_count=len(monthly_members),
                                    overlap_count=len(registered_fans & monthly_members))
        if not _has_table(conn, "snapshots") or not _has_table(conn, "memberships"):
            return result
        fan_match = ("EXISTS(SELECT 1 FROM private.registered_fans f WHERE f.owner_id=? AND f.user_id=m.listener_id)"
                     if fans_available else "0")
        monthly_match = ("EXISTS(SELECT 1 FROM monthly_dj_listeners ml WHERE ml.snapshot_id=? "
                         "AND ml.temperature>0 AND ml.listener_id=m.listener_id)"
                         if monthly_known else "0")
        # All first sightings belong to the same DJ, even when earlier than the
        # selected window. Other broadcasters and future samples cannot affect it.
        ctes = f"""WITH first_seen AS (
            SELECT m.listener_id,date(MIN(s.observed_at),'+9 hours') AS first_day
            FROM snapshots s JOIN memberships m ON m.snapshot_id=s.id
            WHERE s.broadcaster_id=? AND s.observed_at<=? GROUP BY m.listener_id
        ), sightings AS (
            SELECT s.id,s.observed_at,s.complete,m.listener_id,
                date(s.observed_at,'+9 hours') AS day,
                CAST(strftime('%H',s.observed_at,'+9 hours') AS INTEGER) AS hour,
                fs.first_day,{fan_match} AS fan_member,{monthly_match} AS monthly_member
            FROM snapshots s LEFT JOIN memberships m ON m.snapshot_id=s.id
            LEFT JOIN first_seen fs ON fs.listener_id=m.listener_id
            WHERE s.broadcaster_id=? AND s.observed_at>=? AND s.observed_at<=?
        ) """
        parameters = [profile["id"], stamp]
        if fans_available:
            parameters.append(profile["id"])
        if monthly_known:
            parameters.append(monthly_snapshot)
        parameters.extend((profile["id"], start_stamp, stamp))
        counts = """COUNT(DISTINCT listener_id) AS all_count,
            COUNT(DISTINCT CASE WHEN fan_member THEN listener_id END) AS fans_count,
            COUNT(DISTINCT CASE WHEN monthly_member THEN listener_id END) AS monthly_count,
            COUNT(DISTINCT CASE WHEN NOT fan_member AND NOT monthly_member THEN listener_id END) AS other_count,
            COUNT(DISTINCT CASE WHEN fan_member AND monthly_member THEN listener_id END) AS overlap_count,
            COUNT(DISTINCT id) AS snapshot_count,
            COUNT(DISTINCT CASE WHEN complete THEN id END) AS complete_snapshot_count,
            COUNT(DISTINCT CASE WHEN NOT complete THEN id END) AS partial_snapshot_count"""
        rows = conn.execute(ctes + f"""SELECT day,hour,{counts},
            COUNT(DISTINCT CASE WHEN first_day=day THEN listener_id END) AS first_count,
            COUNT(DISTINCT CASE WHEN first_day<day THEN listener_id END) AS repeat_count
            FROM sightings GROUP BY day,hour ORDER BY day,hour""", parameters).fetchall()
        totals = conn.execute(ctes + f"""SELECT {counts},
            COUNT(DISTINCT CASE WHEN first_day>=? THEN listener_id END) AS first_count,
            COUNT(DISTINCT CASE WHEN first_day<? THEN listener_id END) AS repeat_count,
            COUNT(DISTINCT day) AS observed_days,
            COUNT(DISTINCT CASE WHEN listener_id IS NOT NULL THEN day END) AS positive_days,
            MAX(observed_at) AS last_observed_at FROM sightings""",
            parameters + [first_day.isoformat(), first_day.isoformat()]).fetchone()

    def values(row):
        known = row["all_count"] > 0 or row["complete_snapshot_count"] > 0
        return {key: (row[key+"_count"] if known and (monthly_known or key not in ("monthly", "overlap"))
                      else None) for key in _METRICS}

    result["totals"].update(values(totals))
    for key in ("snapshot_count", "complete_snapshot_count", "partial_snapshot_count",
                "observed_days", "positive_days", "last_observed_at"):
        result["totals"][key] = totals[key]
    if totals["snapshot_count"]:
        result["state"] = "partial" if totals["partial_snapshot_count"] else "ready"
    by_day = {day["date"]: day for day in result["daily"]}
    for row in rows:
        cell = by_day[row["day"]]["hours"][row["hour"]]
        cell.update(values(row), snapshot_count=row["snapshot_count"],
                    complete_snapshot_count=row["complete_snapshot_count"],
                    partial_snapshot_count=row["partial_snapshot_count"], observed_days=1,
                    partial=bool(row["partial_snapshot_count"]))
        cell["known_days"] = int(cell["all"] is not None)
        hour = result["hourly"][row["hour"]]
        for key in _METRICS:
            if cell[key] is not None:
                hour[key] = (hour[key] or 0) + cell[key]
        for key in ("snapshot_count", "complete_snapshot_count", "partial_snapshot_count", "observed_days", "known_days"):
            hour[key] += cell[key]
        hour["partial"] = hour["partial"] or cell["partial"]
    return result
