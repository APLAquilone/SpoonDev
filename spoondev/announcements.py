"""Administrator notices kept alongside accounts, separate from listener data.

HTTP handlers select the authenticated role and enforce administrator/CSRF
checks. This module only stores plain text; it never interprets HTML or makes
changes to account credentials or per-account registrations.
"""
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo


_JST = ZoneInfo("Asia/Tokyo")
_IMPORTANCE = ("info", "important", "urgent")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS announcements (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 date TEXT NOT NULL,
 importance TEXT NOT NULL CHECK(importance IN ('info','important','urgent')),
 content TEXT NOT NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 created_by TEXT NOT NULL,
 updated_by TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS announcements_date ON announcements(date,id);
"""
_ORDER = """ORDER BY CASE importance WHEN 'urgent' THEN 2
                    WHEN 'important' THEN 1 ELSE 0 END DESC, date DESC, id DESC"""
_PUBLIC_COLUMNS = "id,date,importance,content,updated_at"


@contextmanager
def _connection(database, *, read_only=False):
    target = Path(database)
    conn = sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True,
                           timeout=10) if read_only else sqlite3.connect(target, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def initialize(database):
    """Add notice storage without migrating or resetting existing accounts."""
    target = Path(database)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _connection(target) as conn:
        conn.executescript(_SCHEMA)
    target.chmod(0o600)


def _now(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("日時にはタイムゾーン付きの datetime を指定してください。")
    return value.astimezone(timezone.utc)


def _id(value):
    if type(value) is not int or not 1 <= value <= 9223372036854775807:
        raise ValueError("お知らせIDは正の整数で指定してください。")
    return value


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("日付は YYYY-MM-DD 形式で指定してください。")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("実在する日付を指定してください。") from exc
    return value


def _actor(value):
    uid = value.get("id") if isinstance(value, dict) else value
    if not isinstance(uid, str) or not re.fullmatch(r"[0-9a-f]{32}", uid):
        raise ValueError("確認済みの管理者アカウントを指定してください。")
    return uid


def list_for_user(database, now=None):
    """Read published notices; scheduled dates use the user's Japan calendar."""
    today = _now(now).astimezone(_JST).date().isoformat()
    with _connection(database, read_only=True) as conn:
        rows = conn.execute(f"SELECT {_PUBLIC_COLUMNS} FROM announcements WHERE date<=? {_ORDER}",
                            (today,))
        return {"announcements": [dict(row) for row in rows]}


def list_admin(database):
    """Include scheduled notices and administrator audit metadata."""
    with _connection(database, read_only=True) as conn:
        return {"announcements": [dict(row) for row in conn.execute(
            f"SELECT * FROM announcements {_ORDER}")]}


def save(database, payload, actor):
    """Create/update a complete notice from a trusted administrator handler.

    An omitted ID creates a notice. For edits an omitted date retains the
    original schedule, so updating text cannot accidentally publish a future
    notice. Importance and content are required in both cases.
    """
    if not isinstance(payload, dict) or payload.keys() - {"id", "date", "importance", "content"}:
        raise ValueError("お知らせの形式を確認してください。")
    uid = _actor(actor)
    notice_id = _id(payload["id"]) if "id" in payload else None
    importance = payload.get("importance")
    if not isinstance(importance, str) or importance not in _IMPORTANCE:
        raise ValueError("重要度は info・important・urgent から指定してください。")
    content = payload.get("content")
    if not isinstance(content, str) or not 1 <= len(content.strip()) <= 5000:
        raise ValueError("内容は1〜5000文字で指定してください。")
    content = content.strip()
    selected_date = _date(payload["date"]) if "date" in payload else None
    now = _now()
    stamp = now.isoformat(timespec="microseconds")
    with _connection(database) as conn:
        conn.execute("BEGIN IMMEDIATE")
        old = None
        if notice_id is not None:
            old = conn.execute("SELECT date FROM announcements WHERE id=?", (notice_id,)).fetchone()
            if old is None:
                raise LookupError("お知らせが見つかりません。")
        selected_date = selected_date or (old["date"] if old else now.astimezone(_JST).date().isoformat())
        if notice_id is None:
            result = conn.execute("""INSERT INTO announcements
              (date,importance,content,created_at,updated_at,created_by,updated_by)
              VALUES(?,?,?,?,?,?,?)""", (selected_date, importance, content, stamp, stamp, uid, uid))
            notice_id = result.lastrowid
        else:
            conn.execute("""UPDATE announcements SET date=?,importance=?,content=?,updated_at=?,updated_by=?
                            WHERE id=?""", (selected_date, importance, content, stamp, uid, notice_id))
        record = conn.execute("SELECT * FROM announcements WHERE id=?", (notice_id,)).fetchone()
        return {"announcement": dict(record)}


def delete(database, notice_id):
    notice_id = _id(notice_id)
    with _connection(database) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("DELETE FROM announcements WHERE id=?", (notice_id,)).rowcount != 1:
            raise LookupError("お知らせが見つかりません。")
    return {"deleted": notice_id}
