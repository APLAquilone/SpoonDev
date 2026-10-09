"""Batched reference listener indices derived from recorded public observations.

These are observation tendencies, not dwell-time estimates, future support
probabilities, or monetary amounts. Unknown observations and missing ranking
entries never become zero-valued evidence.
"""
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import math
import sqlite3
from statistics import mean, median
from zoneinfo import ZoneInfo

from .webdata import _read

WINDOW_DAYS = 28
MIN_SAMPLE_SECONDS = 120
MAX_SAMPLE_SECONDS = 15 * 60
MAX_FETCH_SECONDS = 120
FRESH_GIFT_SECONDS = 24 * 60 * 60
MAX_ROWS = 100000
MAX_DJS = 400
MAX_ENTRIES = 50
_JAPAN = ZoneInfo("Asia/Tokyo")


def _timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _has(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


@contextmanager
def _connection(database):
    if isinstance(database, sqlite3.Connection):
        yield database
    else:
        with _read(database) as conn:
            conn.execute("BEGIN")
            yield conn


def _dicts(cursor, keys):
    return [dict(zip(keys, row)) for row in cursor.fetchall()]


def _empty_stay():
    return {"state": "not_collected", "score": None, "confidence": "low", "window_days": WINDOW_DAYS,
            "observed_days": 0, "dj_count": 0, "pair_count": 0, "observation_count": 0,
            "continued_pairs": 0, "continuation_ratio": None, "multi_day_dj_count": 0,
            "eligible_repeat_djs": 0, "repeat_ratio": None, "long_chain_ratio": None,
            "chain_count": 0, "longest_chain": 0, "updated_at": None, "stale": False,
            "limited": False, "latest_collection_state": None, "last_attempt_at": None,
            "excluded_slow_samples": 0, "interrupted_samples": 0,
            "method": "observational-v1", "reasons": ["no_live_observations"],
            "own": {"dj_id": None, "observed_days": 0, "observation_count": 0,
                    "pair_count": 0, "continued_pairs": 0, "first_seen_at": None, "last_seen_at": None},
            "note": "取得済みの枠での継続・複数日の再観測を示す参考値です。実滞在時間や定着確率ではありません。"}


def _empty_support():
    return {"state": "not_collected", "score": None, "breadth_score": None,
            "breadth_state": "insufficient", "confidence": "low", "own_spoon": None, "own_entry": None,
            "positive_djs": 0, "fresh_positive_djs": 0, "known_djs": 0, "covered_djs": 0,
            "coverage_ratio": None, "updated_at": None, "entries": [], "entries_limit": MAX_ENTRIES,
            "limited": False, "source_period": "unspecified", "method": "confirmed-breadth-v1",
            "reasons": ["no_gift_ranking"],
            "note": "集計期間が未確認のため投げ指数の合成点は算出しません。確認できたDJ別Spoonと応援先の広がりを表示します。"}


def _live_data(conn, ids, start, stamp):
    marks = ",".join("?" for _ in ids)
    rows = _dicts(conn.execute(f"""SELECT s.id,s.room_id,s.broadcaster_id,s.observed_at,s.complete,m.listener_id
        FROM memberships m JOIN snapshots s ON s.id=m.snapshot_id
        WHERE m.listener_id IN ({marks}) AND s.observed_at>=? AND s.observed_at<=?
        ORDER BY s.observed_at DESC,s.id DESC LIMIT ?""", (*ids, start, stamp, MAX_ROWS+1)),
        ("id", "room_id", "dj_id", "observed_at", "complete", "listener_id"))
    limited = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    seen = defaultdict(set)
    room_listeners = defaultdict(set)
    known = defaultdict(set)
    for row in rows:
        seen[row["id"]].add(row["listener_id"])
        room_listeners[(row["dj_id"], row["room_id"])].add(row["listener_id"])
        known[row["listener_id"]].add(row["dj_id"])
    if not rows:
        return {}, known, limited
    timeline = _dicts(conn.execute(f"""WITH related AS (
        SELECT DISTINCT s.broadcaster_id,s.room_id FROM memberships m JOIN snapshots s ON s.id=m.snapshot_id
        WHERE m.listener_id IN ({marks}) AND s.observed_at>=? AND s.observed_at<=?)
        SELECT s.id,s.room_id,s.broadcaster_id,s.observed_at,s.complete
        FROM snapshots s JOIN related r ON r.broadcaster_id=s.broadcaster_id AND r.room_id=s.room_id
        WHERE s.observed_at>=? AND s.observed_at<=?
        ORDER BY s.broadcaster_id,s.room_id,s.observed_at,s.id LIMIT ?""",
        (*ids, start, stamp, start, stamp, MAX_ROWS+1)), ("id", "room_id", "dj_id", "observed_at", "complete"))
    limited = limited or len(timeline) > MAX_ROWS
    attempts = []
    if _has(conn, "observation_attempts"):
        attempts = _dicts(conn.execute(f"""WITH related AS (
            SELECT DISTINCT s.broadcaster_id,s.room_id FROM memberships m JOIN snapshots s ON s.id=m.snapshot_id
            WHERE m.listener_id IN ({marks}) AND s.observed_at>=? AND s.observed_at<=?)
            SELECT a.id,a.room_id,a.broadcaster_id,a.started_at,a.finished_at,a.state,a.snapshot_id,a.collection_run_id
            FROM observation_attempts a JOIN related r ON r.broadcaster_id=a.broadcaster_id AND r.room_id=a.room_id
            WHERE a.finished_at>=? AND a.finished_at<=? ORDER BY a.finished_at,a.id LIMIT ?""",
            (*ids, start, stamp, start, stamp, MAX_ROWS+1)),
            ("id", "room_id", "dj_id", "started_at", "finished_at", "state", "snapshot_id", "run_id"))
        limited = limited or len(attempts) > MAX_ROWS
    boundaries = {attempt["snapshot_id"]: attempt for attempt in attempts[:MAX_ROWS] if attempt["snapshot_id"] is not None}
    by_room = defaultdict(list)
    for row in timeline[:MAX_ROWS]:
        key = (row["dj_id"], row["room_id"])
        # The truncated membership query cannot prove a listener's absence.
        # Suppress all scalar scores when either batch limit is reached.
        row["present"] = seen[row["id"]]
        row["time"] = _timestamp(row["observed_at"])
        boundary = boundaries.get(row["id"])
        row["fetch_seconds"] = (_timestamp(boundary["finished_at"])-_timestamp(boundary["started_at"])).total_seconds() if boundary else None
        row["valid"] = bool(row["complete"]) and (row["fetch_seconds"] is None or row["fetch_seconds"] <= MAX_FETCH_SECONDS)
        row["run_id"] = boundary["run_id"] if boundary else None
        by_room[key].append(row)
    for attempt in attempts[:MAX_ROWS]:
        if attempt["snapshot_id"] is None:
            # A failed fetch has no membership snapshot. Keep it as an unknown
            # interruption instead of silently joining its two neighbours.
            key = (attempt["dj_id"], attempt["room_id"])
            by_room[key].append({"id": -attempt["id"], "observed_at": attempt["finished_at"],
                "time": _timestamp(attempt["finished_at"]), "complete": False, "valid": False,
                "present": set(), "fetch_seconds": None, "run_id": attempt["run_id"]})
    result = defaultdict(list)
    for key, samples in by_room.items():
        samples.sort(key=lambda sample: (sample["time"], sample["id"]))
        clusters = []
        runs = {}
        for sample in samples:
            run_id = sample.get("run_id")
            if sample["id"] < 0:
                clusters.append([sample])
            elif run_id is not None and run_id in runs:
                runs[run_id].append(sample)
            elif (clusters and run_id is None and clusters[-1][0].get("run_id") is None
                  and clusters[-1][0]["id"] >= 0
                  and (sample["time"]-clusters[-1][0]["time"]).total_seconds() < MIN_SAMPLE_SECONDS):
                clusters[-1].append(sample)
            else:
                clusters.append([sample])
                if run_id is not None: runs[run_id] = clusters[-1]
        normalized = []
        for cluster in clusters:
            complete = [row for row in cluster if row["complete"]]
            sample = dict((complete or cluster)[-1])
            evidence = {}
            for row in cluster:
                for uid in row["present"]:
                    evidence[uid] = max(evidence.get(uid, row["time"]), row["time"])
            sample["evidence"] = evidence
            normalized.append(sample)
        normalized.sort(key=lambda sample: (sample["time"], sample["id"]))
        gaps = [(b["time"]-a["time"]).total_seconds() for a, b in zip(normalized, normalized[1:]) if a["valid"] and b["valid"]]
        ordinary = [gap for gap in gaps if MIN_SAMPLE_SECONDS <= gap <= MAX_SAMPLE_SECONDS]
        cadence = median(ordinary) if ordinary else None
        max_gap = min(cadence*1.5, MAX_SAMPLE_SECONDS) if cadence is not None else None
        for uid in room_listeners[key]:
            result[uid].append((key, normalized, max_gap))
    return result, known, limited


def _stay(room_data, uid, own_id, now, limited):
    result = _empty_stay()
    result["own"]["dj_id"] = own_id
    result["limited"] = limited
    if not room_data:
        if limited: result["reasons"] = ["batch_limit"]
        return result
    positive_times = []
    own_times = []
    positive_dj_days = defaultdict(set)
    full_dj_days = defaultdict(set)
    first_dj_seen = {}
    # Macro-average DJ/day cells instead of letting a long-running DJ's
    # thousands of samples dominate the listener's reference score.
    pairs = defaultdict(lambda: [0, 0])
    chains = defaultdict(lambda: [0, 0])
    full_observations = 0
    longest = 0
    own_pairs = [0, 0]
    slow_samples = 0
    interruptions = 0
    for (dj_id, _), samples, max_gap in room_data:
        run_length = 0
        run_day = None

        def end_chain(known_end):
            nonlocal run_length, run_day, longest
            if run_length:
                longest = max(longest, run_length)
                # A short chain interrupted by failure or broadcast end is
                # right-censored. It is not evidence of a short actual stay.
                if known_end or run_length >= 3:
                    cell = chains[(dj_id, run_day)]
                    cell[0] += int(run_length >= 3)
                    cell[1] += 1
            run_length = 0
            run_day = None

        previous = None
        for sample in samples:
            day = sample["time"].astimezone(_JAPAN).date().isoformat()
            evidence_time = sample["evidence"].get(uid)
            if evidence_time is not None:
                positive_times.append(evidence_time)
                if dj_id == own_id: own_times.append(evidence_time)
            slow_samples += int(evidence_time is not None and sample["fetch_seconds"] is not None and sample["fetch_seconds"] > MAX_FETCH_SECONDS)
            interruptions += int(sample["id"] < 0)
            present = uid in sample["present"] and bool(sample["valid"])
            if present:
                full_observations += 1
                positive_dj_days[dj_id].add(day)
                first_dj_seen[dj_id] = min(first_dj_seen.get(dj_id, sample["time"]), sample["time"])
            if sample["valid"]:
                full_dj_days[dj_id].add((day, sample["time"]))
            connected = (previous is not None and max_gap is not None
                         and MIN_SAMPLE_SECONDS <= (sample["time"]-previous["time"]).total_seconds() <= max_gap)
            if not connected or not sample["valid"] or (previous and not previous["valid"]):
                end_chain(False)
            if connected and sample["valid"] and previous["valid"] and uid in previous["present"]:
                cell = pairs[(dj_id, previous["time"].astimezone(_JAPAN).date().isoformat())]
                cell[0] += int(present)
                cell[1] += 1
                if dj_id == own_id:
                    own_pairs[0] += int(present)
                    own_pairs[1] += 1
            if present:
                run_length += 1
                run_day = run_day or day
            else:
                end_chain(bool(sample["valid"]))
            previous = sample
        end_chain(False)
    if not positive_times:
        result["reasons"] = ["batch_limit"] if limited else ["no_live_observations"]
        return result
    days = {time.astimezone(_JAPAN).date() for time in positive_times}
    pair_count = sum(cell[1] for cell in pairs.values())
    continued = sum(cell[0] for cell in pairs.values())
    continuation = mean(cell[0]/cell[1] for cell in pairs.values()) if pairs else None
    chain_count = sum(cell[1] for cell in chains.values())
    long_chain = mean(cell[0]/cell[1] for cell in chains.values()) if chains else None
    eligible = []
    for dj_id, first_seen in first_dj_seen.items():
        opportunities = {day for day, time in full_dj_days[dj_id] if time >= first_seen}
        if len(opportunities) >= 2:
            eligible.append(dj_id)
    repeat = mean(int(len(positive_dj_days[dj]) >= 2) for dj in eligible) if eligible else None
    observed_djs = {key[0] for key, samples, _ in room_data if any(sample["evidence"].get(uid) for sample in samples)}
    result.update(state="insufficient", observed_days=len(days), dj_count=len(observed_djs),
                  pair_count=pair_count, observation_count=len(positive_times), continued_pairs=continued,
                  continuation_ratio=round(continuation, 4) if continuation is not None else None,
                  multi_day_dj_count=sum(len(value) >= 2 for value in positive_dj_days.values()),
                  eligible_repeat_djs=len(eligible), repeat_ratio=round(repeat, 4) if repeat is not None else None,
                  long_chain_ratio=round(long_chain, 4) if long_chain is not None else None,
                  chain_count=chain_count, longest_chain=longest, updated_at=_stamp(max(positive_times)),
                  excluded_slow_samples=slow_samples, interrupted_samples=interruptions,
                  stale=now-max(positive_times) > timedelta(days=7))
    reasons = []
    if limited: reasons.append("batch_limit")
    if pair_count < 10: reasons.append("fewer_than_10_comparisons")
    if len(days) < 3: reasons.append("fewer_than_3_days")
    if len(observed_djs) < 2: reasons.append("single_dj_scope")
    if repeat is None: reasons.append("no_repeat_opportunities")
    if long_chain is None: reasons.append("no_chain_opportunities")
    result["reasons"] = reasons
    if not reasons:
        value = 100*(0.5*continuation + 0.3*repeat + 0.2*long_chain)
        result.update(state="ready", score=max(0, min(100, round(value/5)*5)),
                      confidence="medium" if full_observations/max(1, len(positive_times)) >= 0.7 else "low")
        if pair_count >= 40 and len(days) >= 7 and len(observed_djs) >= 3 and full_observations/max(1, len(positive_times)) >= 0.9 and not result["stale"]:
            result["confidence"] = "high"
    result["own"].update(observed_days=len({time.astimezone(_JAPAN).date() for time in own_times}),
                         observation_count=len(own_times), pair_count=own_pairs[1], continued_pairs=own_pairs[0],
                         first_seen_at=_stamp(min(own_times)) if own_times else None,
                         last_seen_at=_stamp(max(own_times)) if own_times else None)
    return result


def _known_djs(conn, ids, known, stamp, month):
    marks = ",".join("?" for _ in ids)
    if _has(conn, "monthly_dj_snapshots"):
        rows = conn.execute(f"""WITH latest AS (SELECT id,dj_id,ROW_NUMBER() OVER(
            PARTITION BY dj_id ORDER BY observed_at DESC,id DESC) AS rn
            FROM monthly_dj_snapshots WHERE month=? AND observed_at<=?)
            SELECT m.listener_id,l.dj_id FROM monthly_dj_listeners m JOIN latest l ON l.id=m.snapshot_id
            WHERE l.rn=1 AND m.listener_id IN ({marks})""", (month, stamp, *ids)).fetchall()
        for uid, dj_id in rows: known[uid].add(dj_id)
    if _has(conn, "profile_snapshots"):
        rows = conn.execute(f"""WITH latest AS (SELECT id,listener_id,ROW_NUMBER() OVER(
            PARTITION BY listener_id ORDER BY observed_at DESC,id DESC) AS rn
            FROM profile_snapshots WHERE month=? AND observed_at<=? AND listener_id IN ({marks}))
            SELECT l.listener_id,d.broadcaster_id FROM latest l JOIN profile_destinations d ON d.snapshot_id=l.id
            WHERE l.rn=1""", (month, stamp, *ids)).fetchall()
        for uid, dj_id in rows: known[uid].add(dj_id)
    limited = False
    if _has(conn, "gift_dj_snapshots"):
        rows = conn.execute(f"""SELECT DISTINCT m.user_id,s.dj_id FROM gift_dj_members m
            JOIN gift_dj_snapshots s ON s.id=m.snapshot_id WHERE m.user_id IN ({marks}) AND s.observed_at<=?
            LIMIT ?""", (*ids, stamp, MAX_ROWS+1)).fetchall()
        limited = len(rows) > MAX_ROWS
        for uid, dj_id in rows[:MAX_ROWS]: known[uid].add(dj_id)
    return limited


def _gift_data(conn, ids, djs, stamp):
    if not djs or not _has(conn, "gift_dj_snapshots"):
        return {}, {}
    marks = ",".join("?" for _ in djs)
    columns = ("id", "dj_id", "observed_at", "complete", "source_period", "slot")
    rows = _dicts(conn.execute(f"""WITH candidates AS (
        SELECT id,dj_id,observed_at,complete,source_period FROM gift_dj_snapshots
        WHERE dj_id IN ({marks}) AND observed_at<=?), ranked AS (
        SELECT *,ROW_NUMBER() OVER(PARTITION BY dj_id ORDER BY observed_at DESC,id DESC) AS rn FROM candidates), normal AS (
        SELECT *,ROW_NUMBER() OVER(PARTITION BY dj_id ORDER BY observed_at DESC,id DESC) AS rn FROM candidates WHERE complete=1)
        SELECT id,dj_id,observed_at,complete,source_period,'latest' FROM ranked WHERE rn=1
        UNION ALL SELECT id,dj_id,observed_at,complete,source_period,'normal' FROM normal WHERE rn=1""",
        (*djs, stamp)), columns)
    metadata = defaultdict(dict)
    snapshots = set()
    for row in rows:
        metadata[row["dj_id"]][row["slot"]] = row
        snapshots.add(row["id"])
    values = {}
    if snapshots:
        snapshot_marks = ",".join("?" for _ in snapshots)
        user_marks = ",".join("?" for _ in ids)
        rows = conn.execute(f"""SELECT snapshot_id,user_id,total_spoon FROM gift_dj_members
            WHERE snapshot_id IN ({snapshot_marks}) AND user_id IN ({user_marks})""", (*snapshots, *ids)).fetchall()
        values = {(snapshot, uid): amount for snapshot, uid, amount in rows}
    return metadata, values


def _names(conn, djs):
    if not djs: return {}
    marks = ",".join("?" for _ in djs)
    choices = []
    if _has(conn, "users"):
        choices.extend(_dicts(conn.execute(f"SELECT id,name,NULL,name_observed_at FROM users WHERE id IN ({marks})", djs),
                              ("id", "name", "tag", "observed_at")))
    if _has(conn, "profile_users"):
        choices.extend(_dicts(conn.execute(f"SELECT id,name,tag,updated_at FROM profile_users WHERE id IN ({marks})", djs),
                              ("id", "name", "tag", "observed_at")))
    result = {}
    for row in sorted(choices, key=lambda r: r["observed_at"]): result[row["id"]] = row
    return result


def _attempts(conn, djs, stamp):
    if not djs: return {}
    marks = ",".join("?" for _ in djs)
    queries = []
    if _has(conn, "worker_attempts"):
        queries.append(f"""SELECT dj_id,state,started_at,finished_at,summary FROM (
            SELECT *,ROW_NUMBER() OVER(PARTITION BY dj_id ORDER BY started_at DESC,id DESC) AS rn
            FROM worker_attempts WHERE kind='gifts' AND dj_id IN ({marks}) AND started_at<=?) WHERE rn=1""")
    if _has(conn, "collection_runs"):
        queries.append(f"""SELECT target,state,started_at,finished_at,details FROM (
            SELECT *,ROW_NUMBER() OVER(PARTITION BY target ORDER BY started_at DESC,id DESC) AS rn
            FROM collection_runs WHERE kind='gifts' AND target IN ({marks}) AND started_at<=?) WHERE rn=1""")
    result = {}
    for sql in queries:
        for dj_id, state, start, finish, raw in conn.execute(sql, (*djs, stamp)):
            if finish and _timestamp(finish) > _timestamp(stamp):
                state, finish, raw = "running", None, "{}"
            at = finish or start
            if dj_id in result and _timestamp(result[dj_id]["at"]) >= _timestamp(at):
                continue
            try: errors = json.loads(raw).get("errors", [])
            except (ValueError, TypeError, AttributeError): errors = []
            result[dj_id] = {"state": state, "at": at,
                             "errors": [str(error)[:300] for error in errors[:3]] if isinstance(errors, list) else []}
    return result


def _support(uid, known, own_id, metadata, values, names, attempts, now, limited, evaluated_djs):
    result = _empty_support()
    result.update(known_djs=len(known), limited=limited)
    entries = []
    for dj_id in sorted(known):
        records = metadata.get(dj_id, {})
        latest, normal = records.get("latest"), records.get("normal")
        attempt = attempts.get(dj_id)
        entry = {"dj_id": dj_id, "name": names.get(dj_id, {}).get("name", dj_id),
                 "tag": names.get(dj_id, {}).get("tag"), "total_spoon": None,
                 "observed_at": None, "complete": False, "source_period": "unspecified",
                 "state": "not_collected", "source_state": "not_collected", "latest_state": "not_collected",
                 "last_complete_at": normal["observed_at"] if normal else None,
                 "last_complete_spoon": values.get((normal["id"], uid)) if normal else None,
                 "last_attempt_at": attempt["at"] if attempt else None, "errors": attempt["errors"] if attempt else []}
        if dj_id not in evaluated_djs:
            entry.update(state="not_evaluated", source_state="not_evaluated", latest_state="not_evaluated")
        selected = latest
        if latest:
            key = (latest["id"], uid)
            if key in values:
                value = values[key]
                source_state = "ready" if latest["complete"] else "partial"
            elif not latest["complete"] and normal and (normal["id"], uid) in values:
                selected = normal
                value = values[(normal["id"], uid)]
                source_state = "previous_complete"
            else:
                value = None
                source_state = "not_listed" if latest["complete"] else "unknown"
            if value is None and source_state in ("ready", "partial"):
                source_state = "unknown"
            entry.update(total_spoon=value, observed_at=selected["observed_at"], complete=bool(selected["complete"]),
                         source_period=selected["source_period"], state=source_state, source_state=source_state,
                         latest_state="completed" if latest["complete"] else "partial")
            if now-_timestamp(selected["observed_at"]) > timedelta(seconds=FRESH_GIFT_SECONDS):
                entry["state"] = "stale"
        if attempt and (latest is None or attempt["state"] == "running" or _timestamp(attempt["at"]) >= _timestamp(latest["observed_at"])):
            entry["latest_state"] = attempt["state"]
            if not latest and attempt["state"] in ("failed", "partial"):
                entry["state"] = "failed"
        entries.append(entry)
    covered = [entry for entry in entries if (metadata.get(entry["dj_id"], {}).get("latest", {}).get("complete")
               and now-_timestamp(metadata[entry["dj_id"]]["latest"]["observed_at"]) <= timedelta(seconds=FRESH_GIFT_SECONDS))]
    positive = [entry for entry in entries if entry["total_spoon"] is not None and entry["total_spoon"] > 0]
    fresh_positive = [entry for entry in covered if entry["total_spoon"] is not None and entry["total_spoon"] > 0]
    collected = [entry for entry in entries if entry["observed_at"]]
    result.update(positive_djs=len(positive), fresh_positive_djs=len(fresh_positive), covered_djs=len(covered),
                  coverage_ratio=round(len(covered)/len(known), 4) if known else None,
                  updated_at=max((entry["observed_at"] for entry in collected), default=None))
    if collected:
        result.update(state="period_unverified", reasons=["source_period_unverified"])
    elif any(entry["latest_state"] != "not_collected" for entry in entries):
        result.update(state="insufficient", reasons=["no_usable_gift_ranking"])
    comparable_breadth = bool(fresh_positive) or (covered and all(entry["total_spoon"] is not None for entry in covered))
    if not limited and len(known) >= 3 and len(covered) >= 3 and len(covered)/len(known) >= 0.8 and comparable_breadth:
        # This measures only confirmed positive breadth. Unlisted people are
        # not assigned a zero amount, and amounts never weight this score.
        reference = min(100, 100*math.log1p(len(fresh_positive))/math.log1p(5))
        result.update(breadth_score=round(reference/5)*5, breadth_state="ready", confidence="medium")
    if limited: result["reasons"].append("batch_limit")
    own = next((entry for entry in entries if entry["dj_id"] == own_id), None)
    result.update(own_spoon=own["total_spoon"] if own else None, own_entry=own)
    entries.sort(key=lambda entry: (entry["dj_id"] != own_id, entry["total_spoon"] is None,
                                   -(entry["total_spoon"] is not None and entry["total_spoon"] > 0), entry["dj_id"]))
    result["entries"] = entries[:MAX_ENTRIES]
    return result


def listener_insights(database, listener_ids, broadcaster_id=None, now=None):
    """Return public-data reference metrics for at most 100 stable IDs in one batch.

    IDs may be nonnumeric for legacy observation adapters. Broadcaster context
    must be selected by the authenticated caller, never as proof of ownership.
    No private favorites, registrations, account data or global counts are read.
    """
    if not isinstance(listener_ids, list) or len(listener_ids) > 100 or any(
        not isinstance(uid, str) or not uid.strip() or len(uid) > 200 for uid in listener_ids
    ):
        raise ValueError("At most 100 nonempty stable account IDs required")
    if broadcaster_id is not None and (not isinstance(broadcaster_id, str) or not broadcaster_id.strip() or len(broadcaster_id) > 200):
        raise ValueError("Invalid broadcaster context")
    ids = list(dict.fromkeys(listener_ids))
    if not ids: return {}
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None: raise ValueError("now requires timezone")
    now = now.astimezone(timezone.utc)
    stamp, start = _stamp(now), _stamp(now-timedelta(days=WINDOW_DAYS))
    month = now.astimezone(_JAPAN).strftime("%Y-%m")
    with _connection(database) as conn:
        rooms, known, limited_live = _live_data(conn, ids, start, stamp)
        limited_known = _known_djs(conn, ids, known, stamp, month)
        for uid in ids:
            if broadcaster_id: known[uid].add(broadcaster_id)
        all_djs = sorted(set().union(*(known[uid] for uid in ids)))
        if broadcaster_id in all_djs:
            all_djs.remove(broadcaster_id); all_djs.insert(0, broadcaster_id)
        selected = all_djs[:MAX_DJS]
        metadata, values = _gift_data(conn, ids, selected, stamp)
        names, attempts = _names(conn, selected), _attempts(conn, selected, stamp)
        live_state = None
        if _has(conn, "collection_runs"):
            row = conn.execute("""SELECT state,started_at,finished_at FROM collection_runs
                WHERE kind='live' AND started_at<=? ORDER BY started_at DESC,id DESC LIMIT 1""", (stamp,)).fetchone()
            if row:
                live_state = ("running", row[1]) if row[2] and _timestamp(row[2]) > now else (row[0], row[2] or row[1])
        result = {}
        selected_set = set(selected)
        for uid in ids:
            stay = _stay(rooms.get(uid, []), uid, broadcaster_id, now, limited_live)
            if live_state:
                stay.update(latest_collection_state=live_state[0], last_attempt_at=live_state[1])
            support = _support(uid, known[uid], broadcaster_id, metadata, values, names, attempts, now,
                               limited_known or not known[uid] <= selected_set, selected_set)
            support.update(own_observed_days=stay["own"]["observed_days"], own_observation_count=stay["own"]["observation_count"])
            result[uid] = {"stay": stay, "support": support}
        return result
