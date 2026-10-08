"""Fetch canonical snapshot JSON from explicitly configured authorized adapters.

This is not a Spoon API client: Spoon endpoints, listener visibility, and terms
could not be verified. The endpoint must produce the application's canonical
snapshot object. Persistence performs schema validation before storing it.
Failures are returned explicitly and must never be recorded as empty snapshots.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.parse
import urllib.request


MAX_RESPONSE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class FetchError(Exception):
    url: str
    message: str
    status: int | None = None
    retry_after_seconds: float | None = None

    def __str__(self):
        return self.message


def _validate_url(url: str) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(url)
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("URL must have a hostname and must not contain credentials")
    local = parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError("HTTPS is required except for localhost")
    return parsed


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        before = _validate_url(req.full_url)
        after = _validate_url(newurl)
        if before.hostname != after.hostname or before.port != after.port:
            raise ValueError("Redirect to a different host or port is forbidden")
        if before.scheme == "https" and after.scheme != "https":
            raise ValueError("HTTPS downgrade is forbidden")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_snapshot(url: str, *, timeout: float = 20,
                   max_response_bytes: int = MAX_RESPONSE_BYTES) -> dict | FetchError:
    """Return one JSON object, or an explicit error without fabricating observations."""
    try:
        if timeout <= 0 or max_response_bytes <= 0:
            raise ValueError("Timeout and response limit must be positive")
        _validate_url(url)
        request = urllib.request.Request(url, headers={
            "Accept": "application/json", "User-Agent": "SpoonDev/0.1"
        })
        with urllib.request.build_opener(_SafeRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(max_response_bytes + 1)
            if len(raw) > max_response_bytes:
                raise ValueError("Response exceeds size limit")
            def reject_constant(value):
                raise ValueError("Non-finite JSON number is forbidden")
            payload = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
            if not isinstance(payload, dict):
                raise ValueError("Canonical snapshot must be a JSON object")
            return payload
    except urllib.error.HTTPError as exc:
        retry_after = None
        if exc.code == 429:
            value = exc.headers.get("Retry-After") if exc.headers else None
            if value:
                try:
                    if value.strip().isdigit():
                        retry_after = float(value.strip())
                    else:
                        date = parsedate_to_datetime(value)
                        if date.tzinfo is None:
                            date = date.replace(tzinfo=timezone.utc)
                        retry_after = max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass
        return FetchError(url, f"HTTP {exc.code}", exc.code, retry_after)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        # Do not expose upstream response bodies or credential-bearing redirect URLs.
        if isinstance(exc, urllib.error.URLError):
            message = "Network request failed"
        elif isinstance(exc, OSError):
            message = "Network request failed or timed out"
        else:
            message = str(exc) if not isinstance(exc, json.JSONDecodeError) else "Invalid JSON response"
        return FetchError(url, message)


def fetch_many(urls, concurrency: int = 4, timeout: float = 20,
               max_response_bytes: int = MAX_RESPONSE_BYTES) -> list[dict | FetchError]:
    """Fetch with bounded workers; preserve input order and report each failure."""
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency < 1:
        raise ValueError("Concurrency must be a positive integer")
    if timeout <= 0 or max_response_bytes <= 0:
        raise ValueError("Timeout and response limit must be positive")
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        return list(pool.map(lambda url: fetch_snapshot(
            url, timeout=timeout, max_response_bytes=max_response_bytes), urls))
