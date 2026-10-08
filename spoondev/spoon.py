"""Read-only Spoon adapter using publicly observed live/listener endpoints.

Numeric account IDs identify users; nickname is a mutable display name. Only
visible listener identities are available: ghost listeners cannot be identified.
Pagination is not an atomic snapshot of a changing live room.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import math
import threading
import time
from urllib.parse import urlsplit

from .collector import FetchError, fetch_snapshot


class SpoonRateLimit(ValueError):
    """Abort this collection round; caller waits before beginning another round."""

    def __init__(self, retry_after=60.0):
        self.retry_after = retry_after
        super().__init__(f"Spoon HTTP 429; wait at least {retry_after:g} seconds")


def _user(value):
    if not isinstance(value, dict):
        raise ValueError("Invalid user object")
    uid = value.get("id")
    name = value.get("nickname")
    if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0 or not isinstance(name, str):
        raise ValueError("Invalid numeric user ID or nickname")
    result = {"id": str(uid), "name": name}
    if isinstance(value.get("tag"), str):
        result["tag"] = value["tag"]
    temperature = value.get("favorite_temperature")
    if not isinstance(temperature, bool) and isinstance(temperature, (int, float)) and math.isfinite(temperature):
        result["favorite_temperature"] = temperature
    return result


def collect_spoon(base_url="https://jp-api.spooncast.net", concurrency=4,
                  max_rooms=10, max_pages=100, timeout=20):
    """Return (canonical snapshots, explicit error strings), bounded by room/page caps."""
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 1
           for v in (concurrency, max_pages)) or isinstance(max_rooms, bool) or not isinstance(max_rooms, int) or max_rooms < 0:
        raise ValueError("Concurrency/page cap must be positive integers; room cap must be nonnegative")
    if timeout <= 0:
        raise ValueError("Timeout must be positive")
    base_url = base_url.rstrip("/")
    base = urlsplit(base_url)
    local = base.hostname in {"localhost", "127.0.0.1", "::1"}
    if (base.scheme != "https" and not (local and base.scheme == "http")) or not base.hostname or base.username or base.password or base.path or base.query or base.fragment:
        raise ValueError("Base URL must be an HTTPS origin (localhost HTTP is allowed)")
    request_lock = threading.Lock()
    last_request = [0.0]
    rate_limit = [None]
    rate_lock = threading.Lock()

    def page(url, path):
        target = urlsplit(url)
        if (target.scheme, target.netloc, target.path) != (base.scheme, base.netloc, path) or target.fragment:
            raise ValueError("Pagination URL changed origin or endpoint")
        with request_lock:
            if rate_limit[0] is not None:
                raise rate_limit[0]
            delay = 0.1 - (time.monotonic() - last_request[0])
            if delay > 0:
                time.sleep(delay)
            if rate_limit[0] is not None:
                raise rate_limit[0]
            last_request[0] = time.monotonic()
        result = fetch_snapshot(url, timeout=timeout)
        if isinstance(result, FetchError):
            if result.status == 429:
                delay = result.retry_after_seconds
                with rate_lock:
                    delay = delay if delay is not None else 60.0
                    if rate_limit[0] is None or delay > rate_limit[0].retry_after:
                        rate_limit[0] = SpoonRateLimit(delay)
                raise rate_limit[0]
            raise ValueError(result.message)
        if not isinstance(result, dict) or result.get("status_code") != 200 or not isinstance(result.get("results"), list):
            raise ValueError("Unexpected upstream status or results")
        next_url = result.get("next", "")
        if next_url is not None and not isinstance(next_url, str):
            raise ValueError("Invalid pagination URL")
        return result, next_url or ""

    errors = []
    rooms = []
    room_ids = set()
    seen = set()
    url = base_url + "/lives/"
    try:
        for _ in range(max_pages):
            if url in seen:
                raise ValueError("Live pagination cycle")
            seen.add(url)
            data, next_url = page(url, "/lives/")
            for room in data["results"]:
                rid = room.get("id") if isinstance(room, dict) else None
                if isinstance(rid, bool) or not isinstance(rid, int) or rid <= 0:
                    raise ValueError("Invalid numeric room ID")
                host = _user(room.get("author"))
                if rid not in room_ids:
                    rooms.append((rid, host))
                    room_ids.add(rid)
                if max_rooms and len(rooms) >= max_rooms:
                    break
            if (max_rooms and len(rooms) >= max_rooms) or not next_url:
                url = ""
                break
            url = next_url
        if url:
            errors.append("Live discovery reached page cap; room selection is partial")
    except SpoonRateLimit:
        raise
    except ValueError as exc:
        errors.append(f"Live discovery: {exc}")

    def collect_room(room):
        rid, host = room
        path = f"/lives/{rid}/listeners/"
        url = base_url + path
        seen = set()
        users = {}
        complete = False
        room_errors = []
        try:
            for _ in range(max_pages):
                if url in seen:
                    raise ValueError("Listener pagination cycle")
                seen.add(url)
                data, next_url = page(url, path)
                for listener in data["results"]:
                    user = _user(listener)
                    users[user["id"]] = user
                if not next_url:
                    complete = True
                    break
                url = next_url
            if not complete:
                room_errors.append(f"Room {rid}: listener pagination reached page cap")
        except SpoonRateLimit:
            raise
        except ValueError as exc:
            room_errors.append(f"Room {rid}: {exc}")
        if not complete and not users:
            return None, room_errors
        snapshot = {"room_id": str(rid), "broadcaster": host,
                    "listeners": list(users.values()), "complete": complete,
                    "observed_at": datetime.now(timezone.utc).isoformat()}
        return snapshot, room_errors

    snapshots = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        for snapshot, room_errors in executor.map(collect_room, rooms):
            errors.extend(room_errors)
            if snapshot is not None:
                snapshots.append(snapshot)
    return snapshots, errors
