"""Observed listener-to-DJ Spoon amounts from the public FAN ranking.

The official web topFans modal (A1j4Nd1y.js, verified 2026-10-09) reads
``users/{dj_id}/top_fan/`` and displays ``total_spoon`` in Japan. The request
and response do not identify a time period, so this module deliberately
records ``source_period='unspecified'``. These are Spoon units sent to one
DJ, not a listener's lifetime/global spending or a currency amount.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from itertools import count
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from .collector import FetchError, fetch_snapshot
from .fans import numeric_id

API_BASE = 'https://jp-api.spooncast.net'
SOURCE = 'spoon_top_fan'
SOURCE_PERIOD = 'unspecified'
REQUEST_SPACING = 0.4
_request_lock = threading.Lock()
_last_request = None

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS gift_dj_snapshots (
 id INTEGER PRIMARY KEY, dj_id TEXT NOT NULL, observed_at TEXT NOT NULL,
 complete INTEGER NOT NULL CHECK(complete IN (0,1)),
 source_period TEXT NOT NULL CHECK(source_period='unspecified'));
CREATE TABLE IF NOT EXISTS gift_dj_members (
 snapshot_id INTEGER NOT NULL REFERENCES gift_dj_snapshots(id) ON DELETE CASCADE,
 user_id TEXT NOT NULL, name TEXT NOT NULL, tag TEXT,
 total_spoon INTEGER CHECK(total_spoon IS NULL OR total_spoon>=0),
 PRIMARY KEY(snapshot_id,user_id));
CREATE INDEX IF NOT EXISTS gift_dj_snapshots_latest
 ON gift_dj_snapshots(dj_id,observed_at,id);
CREATE INDEX IF NOT EXISTS gift_dj_members_user
 ON gift_dj_members(user_id,snapshot_id);
'''


@contextmanager
def _connection(database):
    owned = not isinstance(database, sqlite3.Connection)
    conn = sqlite3.connect(database, timeout=30) if owned else database
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        yield conn
    finally:
        if owned:
            conn.close()


def initialize(database):
    """Create additive, separate gift tables without changing live or temperature data."""
    with _connection(database) as conn:
        conn.executescript(_SCHEMA)


def _entry(row):
    if not isinstance(row, dict) or not isinstance(row.get('user'), dict):
        raise ValueError('gift ranking entry requires user')
    user = row['user']
    uid = numeric_id(user.get('id'))
    name = user.get('nickname')
    tag = user.get('tag')
    if not isinstance(name, str) or len(name) > 500:
        raise ValueError('gift ranking nickname requires string')
    if tag is not None and (not isinstance(tag, str) or len(tag) > 500):
        raise ValueError('invalid gift ranking tag')
    amount = row.get('total_spoon')
    # Missing amounts remain unknown. A returned zero is a real zero.
    if amount is not None and (type(amount) is not int or not 0 <= amount <= 2**63-1):
        raise ValueError('gift amount requires nonnegative integer or null')
    return (uid, name, tag, amount)


def _next_url(value, endpoint):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError('invalid gift pagination pointer')
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        expected = urlsplit(endpoint)
        if (parsed.scheme, parsed.netloc, parsed.path) != (
            expected.scheme, expected.netloc, expected.path
        ) or parsed.fragment:
            raise ValueError('gift pagination changed origin or endpoint')
        params = parse_qs(parsed.query, keep_blank_values=True)
        if set(params) != {'cursor'} or len(params['cursor']) != 1:
            raise ValueError('invalid gift pagination query')
        cursor = params['cursor'][0]
    else:
        if value.startswith(('/', '?', '#')) or ':' in value:
            raise ValueError('invalid gift pagination cursor')
        cursor = value
    if not cursor or len(cursor) > 2048:
        raise ValueError('invalid gift pagination cursor')
    return endpoint + '?' + urlencode({'cursor': cursor})


def _request(url, stopped_event):
    global _last_request
    with _request_lock:
        if stopped_event is not None and stopped_event.is_set():
            raise ValueError('gift collection stopped')
        if _last_request is not None:
            delay = REQUEST_SPACING - (time.monotonic() - _last_request)
            if delay > 0:
                if stopped_event is not None:
                    if stopped_event.wait(delay):
                        raise ValueError('gift collection stopped')
                else:
                    time.sleep(delay)
        _last_request = time.monotonic()
    return fetch_snapshot(url)


def _save(database, dj_id, entries, complete, stamp):
    initialize(database)
    with _connection(database) as conn:
        conn.execute('SAVEPOINT gift_write')
        try:
            snapshot_id = conn.execute('''INSERT INTO gift_dj_snapshots
              (dj_id,observed_at,complete,source_period) VALUES (?,?,?,?)''',
              (dj_id, stamp, int(complete), SOURCE_PERIOD)).lastrowid
            conn.executemany('INSERT INTO gift_dj_members VALUES (?,?,?,?,?)',
                             [(snapshot_id, *entry) for entry in entries.values()])
            conn.execute('RELEASE SAVEPOINT gift_write')
        except BaseException:
            conn.execute('ROLLBACK TO SAVEPOINT gift_write')
            conn.execute('RELEASE SAVEPOINT gift_write')
            raise
    return snapshot_id


def collect_gifts(database, dj_id, max_pages=0, stopped_event=None):
    """Collect a single DJ's public ranking; zero follows to its actual end.

    Requests have a bounded response size/timeout and are paced. Pagination
    cannot change host, endpoint or cursor parameters. Partial successful pages
    are explicitly incomplete; a failure before any valid page leaves the last
    stored ranking intact. HTTP 429 immediately ends this collection.
    """
    dj_id = numeric_id(dj_id)
    if type(max_pages) is not int or max_pages < 0:
        raise ValueError('max_pages requires nonnegative integer')
    endpoint = f'{API_BASE}/users/{dj_id}/top_fan/'
    url = endpoint
    entries = {}
    seen = set()
    pages = 0
    complete = False
    errors = []
    retry_after = 0.0
    capped = False
    for _ in (range(max_pages) if max_pages else count()):
        try:
            if url in seen:
                raise ValueError('gift pagination loop')
            seen.add(url)
            payload = _request(url, stopped_event)
            if isinstance(payload, FetchError):
                if payload.status == 429:
                    retry_after = payload.retry_after_seconds or 60.0
                raise ValueError(payload.message)
            if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
                raise ValueError('gift ranking response requires results list')
            if payload.get('status_code', 200) != 200:
                raise ValueError('gift ranking response reported an error')
            page_entries = {}
            for raw in payload['results']:
                entry = _entry(raw)
                uid = entry[0]
                if uid in page_entries and page_entries[uid] != entry:
                    raise ValueError('conflicting duplicate gift ranking user')
                if uid in entries and entries[uid] != entry:
                    raise ValueError('conflicting duplicate gift ranking user')
                page_entries[uid] = entry
            entries.update(page_entries)
            pages += 1
            pointer = payload.get('next')
            if pointer is None or pointer == '':
                complete = True
                break
            url = _next_url(pointer, endpoint)
        except (ValueError, OSError) as exc:
            errors.append(str(exc))
            break
    else:
        capped = True
        errors.append('gift ranking page cap reached')
    stamp = datetime.now(timezone.utc).isoformat(timespec='microseconds')
    snapshot_id = _save(database, dj_id, entries, complete, stamp) if pages else None
    return {'dj_id': dj_id, 'snapshot_id': snapshot_id,
            'observed_at': stamp if snapshot_id is not None else None,
            'user_count': len(entries), 'page_count': pages, 'complete': complete,
            'capped': capped, 'errors': errors, 'retry_after': retry_after,
            'source': SOURCE, 'source_period': SOURCE_PERIOD, 'unit': 'spoon',
            'coverage': 'single_dj_public_ranking'}


def read_ranking(conn, dj_id, *, observed_before=None):
    """Read the latest snapshot using a caller-owned read-only SQLite connection."""
    dj_id = numeric_id(dj_id)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='gift_dj_snapshots'").fetchone() is None:
        return None
    cutoff=' AND observed_at<=?' if observed_before is not None else ''
    params=(dj_id,observed_before) if observed_before is not None else (dj_id,)
    row = conn.execute(f'''SELECT id,observed_at,complete,source_period
      FROM gift_dj_snapshots WHERE dj_id=?{cutoff} ORDER BY observed_at DESC,id DESC LIMIT 1''',
      params).fetchone()
    if row is None:
        return None
    members = conn.execute('''SELECT user_id,name,tag,total_spoon FROM gift_dj_members
      WHERE snapshot_id=? ORDER BY total_spoon IS NULL,total_spoon DESC,user_id''',
      (row[0],)).fetchall()
    return {'snapshot_id': row[0], 'dj_id': dj_id, 'observed_at': row[1],
            'complete': bool(row[2]), 'source_period': row[3], 'source': SOURCE,
            'unit': 'spoon', 'coverage': 'single_dj_public_ranking',
            'rows': [dict(zip(('user_id', 'name', 'tag', 'total_spoon'), member))
                     for member in members]}
