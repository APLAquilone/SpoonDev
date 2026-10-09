"""Short-lived current public live-directory cache, separate from historical observations."""
from datetime import datetime,timezone
import threading
import time
from urllib.parse import urlsplit
from .collector import fetch_snapshot,FetchError
from .spoon import _user

BASE='https://jp-api.spooncast.net'
_lock=threading.Lock()
_cached=None
_expires=0.0
_error=None

class LiveStatusError(ValueError):
    pass


def current_public_lives():
    global _cached,_expires,_error
    with _lock:
        if time.monotonic()<_expires:
            if _error:raise LiveStatusError(_error)
            return _cached
        url=BASE+'/lives/';seen=set();users={};last_request=0.0;wait=30
        try:
            while url:
                parsed=urlsplit(url)
                if (parsed.scheme,parsed.netloc,parsed.path)!=('https','jp-api.spooncast.net','/lives/') or parsed.fragment:
                    raise LiveStatusError('配信一覧の取得先が変わりました。')
                if url in seen:raise LiveStatusError('配信一覧のページが循環しています。')
                seen.add(url)
                delay=.2-(time.monotonic()-last_request)
                if delay>0:time.sleep(delay)
                last_request=time.monotonic()
                data=fetch_snapshot(url,timeout=8)
                if isinstance(data,FetchError):
                    if data.status==429:wait=max(wait,data.retry_after_seconds or 60)
                    raise LiveStatusError('配信状態を確認できませんでした。時間をおいて再確認します。')
                if not isinstance(data,dict) or data.get('status_code')!=200 or not isinstance(data.get('results'),list):
                    raise LiveStatusError('配信一覧の形式が変わりました。')
                for room in data['results']:
                    if not isinstance(room,dict) or type(room.get('id')) is not int or room['id']<=0:
                        raise LiveStatusError('配信一覧の形式が変わりました。')
                    host=_user(room.get('author'))
                    users[host['id']]={'user_id':host['id'],'room_id':str(room['id'])}
                next_url=data.get('next')
                if next_url is not None and not isinstance(next_url,str):
                    raise LiveStatusError('配信一覧の次ページが不正です。')
                url=next_url or ''
            result={'users':list(users.values()),'checked_at':datetime.now(timezone.utc).isoformat(),
                    'coverage':'public_live_directory'}
        except (ValueError,OSError) as exc:
            _cached=None;_error=str(exc);_expires=time.monotonic()+wait
            raise LiveStatusError(_error) from exc
        _cached=result;_error=None;_expires=time.monotonic()+30
        return result
