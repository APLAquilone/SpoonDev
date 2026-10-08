"""Official web user-search route and public numeric profile lookup.

The search route is verified from Spoon's web client, its legacy redirect,
and a successful live search response. Parsing fails on unexpected schemas. Results are the
upstream search selection, not a complete directory of all Spoon accounts.
"""

from collections import OrderedDict
import threading
import time
from urllib.parse import urlencode, urlsplit

from .collector import FetchError, fetch_snapshot


class DirectoryError(ValueError):
    pass


GATEWAY = "https://jp-gw.spooncast.net"
_slots = threading.BoundedSemaphore(2)
_spacing = threading.Lock()
_last_start = 0.0
_retry_until = 0.0
_cache_lock = threading.Lock()
_cache = OrderedDict()


def _read(url, timeout):
    global _last_start, _retry_until
    with _slots:
        with _spacing:
            if time.monotonic() < _retry_until:
                raise DirectoryError('Spoonの待機指定に従っています。時間をおいて再検索してください。')
            remaining = 0.25 - (time.monotonic() - _last_start)
            if remaining > 0:
                time.sleep(remaining)
            _last_start = time.monotonic()
        result = fetch_snapshot(url, timeout=timeout)
    if isinstance(result, FetchError):
        detail = result.message
        if result.status == 429:
            with _spacing:
                _retry_until=max(_retry_until,time.monotonic()+(result.retry_after_seconds or 60))
            detail += f"; wait at least {result.retry_after_seconds or 60:g} seconds"
        raise DirectoryError(detail)
    if not isinstance(result, dict):
        raise DirectoryError("Unexpected directory response")
    status = result.get("status_code")
    if status is not None and status != 200:
        raise DirectoryError("Upstream directory reported a failure")
    if not isinstance(result.get("results"), list):
        raise DirectoryError("Directory response has no valid results list")
    return result


def _user(value):
    if not isinstance(value, dict):
        raise DirectoryError("Invalid directory user object")
    uid = value.get("id")
    name = value.get("nickname")
    tag = value.get("tag")
    if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0 or not isinstance(name, str):
        raise DirectoryError("Invalid directory user ID or nickname")
    if tag is not None and not isinstance(tag, str):
        raise DirectoryError("Invalid directory profile ID")
    return {"id": str(uid), "name": name, "tag": tag, "last_seen_at": None}


def _next(value):
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise DirectoryError("Invalid search pagination URL")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.netloc != "jp-gw.spooncast.net" or parsed.path != "/search/user" or parsed.fragment:
        raise DirectoryError("Search pagination changed origin or endpoint")
    return value


def search_users(q, offset=0, limit=50, timeout=8):
    """Search Spoon, following only verified-origin cursor pages to the offset."""
    if not isinstance(q, str) or not q.strip() or len(q) > 200:
        raise DirectoryError("Search requires a nonempty query of at most 200 characters")
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 1000:
        raise DirectoryError("Search offset must be between 0 and 1000")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50 or timeout <= 0:
        raise DirectoryError("Invalid search limit or timeout")
    q = q.strip()
    if q.isascii() and q.isdigit() and int(q) > 0:
        return {'users':[resolve_user(q,timeout)] if offset == 0 else [],
                'has_more':False,'source':'spoon_public_search'}
    cache_key = (q, offset, limit)
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 300:
            _cache.move_to_end(cache_key)
            result = cached[1]
            return {**result, "users": [dict(user) for user in result["users"]]}
    url = GATEWAY + "/search/user?" + urlencode({"keyword": q, "page_size": 50, "v": 2})
    seen = set()
    users = []
    has_more = False
    for _ in range(20):
        if url in seen:
            raise DirectoryError("Search pagination cycle")
        seen.add(url)
        data = _read(url, timeout)
        users.extend(_user(value) for value in data["results"])
        next_url = _next(data.get("next"))
        if len(users) >= offset + limit or not next_url:
            has_more = len(users) > offset + limit or bool(next_url)
            break
        url = next_url
    else:
        raise DirectoryError("Search pagination reached its safety limit")
    result = {"users": users[offset:offset + limit], "has_more": has_more,
              "source": "spoon_public_search"}
    with _cache_lock:
        _cache[cache_key] = (time.monotonic(), result)
        _cache.move_to_end(cache_key)
        while len(_cache) > 128:
            _cache.popitem(last=False)
    return {**result, "users": [dict(user) for user in result["users"]]}


def resolve_user(user_id, timeout=8):
    """Resolve a stable numeric Spoon account ID without login."""
    uid = str(user_id)
    if len(uid) > 20 or not uid.isascii() or not uid.isdigit() or int(uid) <= 0 or timeout <= 0:
        raise DirectoryError("User ID must be a positive numeric ID")
    data = _read(f"https://jp-api.spooncast.net/users/{int(uid)}/", timeout)
    matches = [_user(value) for value in data["results"]]
    if len(matches) != 1 or matches[0]["id"] != str(int(uid)):
        raise DirectoryError("Profile response did not match the requested user ID")
    return matches[0]
