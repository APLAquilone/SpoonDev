"""Invert public DJ monthly rankings into observed listener-to-DJ relations.

Coverage is limited to broadcasters known from live observations. A ranking
entry is a monthly profile relationship, never proof of a current live visit.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import math
import sqlite3
import threading
import time
from urllib.parse import urlencode, urlsplit, quote, parse_qs
from zoneinfo import ZoneInfo
from .collector import FetchError, fetch_snapshot
from . import profiledb

GATEWAY_BASE='https://jp-gw.spooncast.net'


def _entry(row):
    if not isinstance(row,dict) or not isinstance(row.get('user'),dict):
        raise ValueError('ranking entry requires user')
    raw=row['user']; user_id=raw.get('id'); name=raw.get('nickname')
    if isinstance(user_id,bool) or not isinstance(user_id,(str,int)) or not str(user_id).strip():
        raise ValueError('invalid ranking user ID')
    if not isinstance(name,str): raise ValueError('ranking user nickname requires string')
    user={'id':str(user_id),'name':name}
    if raw.get('tag') is not None:
        if not isinstance(raw['tag'],str): raise ValueError('invalid ranking user tag')
        user['tag']=raw['tag']
    temperature=row.get('favoriteTemperature')
    if temperature is not None:
        if type(temperature) not in (int,float): raise ValueError('invalid ranking temperature')
        try: valid=math.isfinite(temperature)
        except OverflowError: valid=False
        if not valid: raise ValueError('nonfinite ranking temperature')
    return user,temperature


def collect_monthly(database,max_djs=100,concurrency=4,max_pages=100,progress_callback=None,stopped_event=None):
    if type(max_djs) is not int or max_djs<0 or type(concurrency) is not int or concurrency<1 or type(max_pages) is not int or max_pages<1:
        raise ValueError('invalid collection limits')
    with sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True) as conn:
        rows=conn.execute('''WITH known AS (SELECT broadcaster_id,MAX(observed_at) AS recent
          FROM snapshots GROUP BY broadcaster_id) SELECT u.id,u.name,
          (SELECT a.tag FROM user_attributes a JOIN snapshots ts ON ts.id=a.snapshot_id
           WHERE a.user_id=u.id ORDER BY ts.observed_at DESC,ts.id DESC LIMIT 1)
          FROM known k JOIN users u ON u.id=k.broadcaster_id ORDER BY k.recent DESC,u.id''').fetchall()
    all_djs=[{'id':u,'name':n,'tag':t} for u,n,t in rows]
    djs=all_djs[:max_djs] if max_djs else all_djs
    cap=len(djs)<len(all_djs)
    now=datetime.now(timezone.utc)
    month=now.astimezone(ZoneInfo('Asia/Tokyo')).strftime('%Y-%m')
    stamp=now.isoformat(timespec='microseconds')
    lock=threading.Lock(); last_request=[None]; limited=[False]; retry_after=[0.0]

    def request(url):
        with lock:
            if stopped_event is not None and stopped_event.is_set():
                raise ValueError('monthly collection stopped')
            if limited[0]: raise ValueError('HTTP 429; remaining monthly requests stopped')
            if last_request[0] is not None:
                delay=0.25-(time.monotonic()-last_request[0])
                if delay>0: time.sleep(delay)
            last_request[0]=time.monotonic()
        result=fetch_snapshot(url)
        if isinstance(result,FetchError):
            if result.status==429:
                with lock:
                    limited[0]=True
                    retry_after[0]=max(retry_after[0],result.retry_after_seconds or 60)
            raise ValueError(result.message)
        return result

    def scan(dj):
        path=f"/favorite-temperatures/djs/{quote(dj['id'],safe='')}/rankings"
        endpoint=GATEWAY_BASE+path
        url=endpoint+'?rankType=MONTHLY'
        seen_urls=set(); entries={}; successful=False
        try:
            for page_number in range(max_pages):
                if url in seen_urls: raise ValueError('pagination loop')
                seen_urls.add(url)
                payload=request(url)
                if not isinstance(payload,dict) or not isinstance(payload.get('results'),list):
                    raise ValueError('ranking response requires results list')
                if payload.get('status_code',200) != 200:
                    raise ValueError('ranking response reported an error')
                page_entries={}
                for row in payload['results']:
                    user,temperature=_entry(row)
                    if user['id'] in entries and entries[user['id']]!=(user,temperature):
                        raise ValueError('conflicting duplicate ranking user')
                    if user['id'] in page_entries and page_entries[user['id']]!=(user,temperature):
                        raise ValueError('conflicting duplicate ranking user')
                    page_entries[user['id']]=(user,temperature)
                entries.update(page_entries); successful=True
                next_value=payload.get('next')
                if not next_value: return dj,entries,True,successful,None
                if not isinstance(next_value,str): raise ValueError('invalid pagination pointer')
                next_url=urlsplit(next_value)
                if next_url.scheme or next_url.netloc:
                    base=urlsplit(GATEWAY_BASE)
                    if (next_url.scheme,next_url.netloc,next_url.path)!=(base.scheme,base.netloc,path) or next_url.fragment:
                        raise ValueError('pagination changed origin or endpoint')
                    # Official JS extracts the cursor and re-adds rankType itself.
                    cursor=parse_qs(next_url.query).get('cursor',[''])[0]
                    if not cursor: raise ValueError('pagination URL lacks cursor')
                else:
                    if next_value.startswith(('/','?','#')): raise ValueError('invalid cursor')
                    cursor=next_value
                url=endpoint+'?'+urlencode({'rankType':'MONTHLY','cursor':cursor})
            raise ValueError('monthly ranking page cap reached')
        except (ValueError,OSError) as exc:
            return dj,entries,False,successful,f"DJ {dj['id']}: {exc}"

    results=[]; grouped={}; successful_djs=0; initialized=False
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures={pool.submit(scan,dj):dj for dj in djs}
        for future in as_completed(futures):
            dj,entries,dj_complete,successful,error=future.result()
            results.append((dj,entries,dj_complete,successful,error))
            if successful:
                if not initialized:
                    profiledb.initialize(database); initialized=True
                profiledb.save_dj_ranking(database,dj,
                    [{'user':user,'temperature':temperature} for user,temperature in entries.values()],
                    month,dj_complete,datetime.now(timezone.utc).isoformat(timespec='microseconds'))
                successful_djs+=1
                grouped.update(entries)
            if len(results)%100==0:
                if progress_callback:
                    progress_callback({'scanned_djs':len(results),'total_djs':len(djs),'indexed_listeners':len(grouped)})
    errors=[error for _,_,_,_,error in results if error]
    complete=bool(djs) and not cap and not errors
    return {'dj_count':successful_djs,'user_count':len(grouped),'complete':complete,
            'errors':errors,'month':month,'source':'monthly_profile','rank_type':'MONTHLY',
            'coverage':'known_broadcasters','known_dj_count':len(all_djs),'selected_dj_count':len(djs),
            'capped':cap,'retry_after':retry_after[0]}
