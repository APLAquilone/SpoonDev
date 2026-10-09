import argparse
import json
import math
import signal
import sqlite3
import sys
import threading
from pathlib import Path

from .db import initialize, save_snapshot, broadcaster_listeners, listener_broadcasters, temperature_history


def main(argv=None):
    from . import __version__
    parser = argparse.ArgumentParser(description="ID-based listener observation database")
    parser.add_argument('--version',action='version',version='SCI '+__version__)
    parser.add_argument("--db", default="data/spoondev.sqlite3")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db")
    web = commands.add_parser("serve", help="Start the broadcaster workspace")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8080)
    web.add_argument('--auth', action='store_true')
    web.add_argument('--auth-db', default='data/accounts.sqlite3')
    web.add_argument('--public-url')
    account=commands.add_parser('create-user',help='Create or reset an account (password prompted locally)')
    account.add_argument('username')
    account.add_argument('--auth-db',default='data/accounts.sqlite3')
    account.add_argument('--reset',action='store_true')
    account.add_argument('--label',default='利用者')
    account.add_argument('--claim-local-data',action='store_true')
    role=commands.add_parser('set-role',help='Grant/revoke console administrator rights')
    role.add_argument('username')
    role.add_argument('role',choices=['admin','user'])
    role.add_argument('--auth-db',default='data/accounts.sqlite3')
    imp = commands.add_parser("import")
    imp.add_argument("file", type=Path)
    report = commands.add_parser("report")
    report.add_argument("direction", choices=["broadcaster", "listener"])
    report.add_argument("id")
    history = commands.add_parser("history", help="Temperature observations for one broadcaster/listener pair")
    history.add_argument("broadcaster_id")
    history.add_argument("listener_id")
    collect = commands.add_parser("collect", description="Fetch canonical snapshots from a verified adapter, not an unverified Spoon API")
    collect.add_argument("--url", action="append", required=True)
    collect.add_argument("--concurrency", type=int, default=4)
    collect.add_argument("--interval", type=float, default=300)
    collect.add_argument("--once", action="store_true")
    spoon = commands.add_parser("collect-spoon", help="Collect public Japanese Spoon live listeners and temperatures")
    spoon.add_argument("--concurrency", type=int, default=4)
    spoon.add_argument("--interval", type=float, default=300)
    spoon.add_argument("--max-rooms", type=int, default=10, help="Maximum rooms per round; use 0 for all listed rooms")
    spoon.add_argument("--max-pages", type=int, default=100)
    spoon.add_argument("--once", action="store_true")
    monthly = commands.add_parser('collect-monthly', help='Index current-month public DJ rankings as listener appearances')
    monthly.add_argument('--max-djs',type=int,default=100,help='0 scans all known broadcasters')
    monthly.add_argument('--max-pages',type=int,default=0,help='0 follows all pages (default); positive value sets an optional cap')
    monthly.add_argument('--concurrency',type=int,default=4)
    monthly.add_argument('--interval',type=float,default=3600)
    monthly.add_argument('--once',action='store_true')
    monthly.add_argument('--dj-id',action='append',help='Collect only these broadcaster IDs; repeat for more')
    gift=commands.add_parser('collect-gifts',help='Collect public DJ-specific Spoon rankings (period unverified)')
    gift.add_argument('--dj-id',action='append',required=True)
    gift.add_argument('--max-pages',type=int,default=0)
    gift.add_argument('--interval',type=float,default=3600)
    gift.add_argument('--once',action='store_true')
    automatic = commands.add_parser('collect-auto', help='Run one shared collector for live rooms and registered DJs')
    automatic.add_argument('--auth-db', default='data/accounts.sqlite3')
    automatic.add_argument('--concurrency', type=int, default=4)
    automatic.add_argument('--live-interval', type=float, default=300)
    automatic.add_argument('--ranking-interval', type=float, default=3600)
    args = parser.parse_args(argv)
    if args.command == 'serve' and not 0 <= args.port <= 65535:
        parser.error('port must be 0..65535')
    if args.command == 'collect-auto' and (not 1 <= args.concurrency <= 16 or
            any(not math.isfinite(value) or value < 30 for value in (args.live_interval, args.ranking_interval))):
        parser.error('concurrency must be 1..16; collection intervals must be at least 30 seconds')
    if args.command in {"collect", "collect-spoon",'collect-monthly'} and (not 1 <= args.concurrency <= 16 or not math.isfinite(args.interval) or args.interval < 30):
        parser.error("concurrency must be 1..16; interval must be at least 30 seconds")
    if args.command == "collect-spoon" and (args.max_rooms < 0 or args.max_pages < 1):
        parser.error("max-rooms must be nonnegative; max-pages must be positive")
    if args.command=='collect-gifts' and (args.max_pages<0 or not math.isfinite(args.interval) or args.interval<30): parser.error('max-pages must be nonnegative; interval at least30')
    if args.command == 'collect-monthly' and (args.max_djs < 0 or args.max_pages < 0):
        parser.error('max-djs and max-pages must be nonnegative')
    if args.command in {'collect-gifts','collect-monthly'} and args.dj_id is not None:
        from .fans import numeric_id
        try:
            args.dj_id=list(dict.fromkeys(numeric_id(uid) for uid in args.dj_id))
        except ValueError as exc:
            parser.error(str(exc))
    active_run=None
    try:
        if args.command == 'set-role':
            from . import accounts
            print(json.dumps(accounts.set_role(args.auth_db,args.username,args.role),ensure_ascii=False))
            return 0
        if args.command == 'create-user':
            import getpass
            from . import accounts
            if args.claim_local_data and args.username.strip().lower()!='kitomoya':
                raise ValueError('--claim-local-data は kitomoya の既存登録移行専用です。追加ユーザーには付けないでください。')
            password=getpass.getpass('Password (12+ characters): ')
            if password!=getpass.getpass('Confirm password: '): raise ValueError('パスワードが一致しません。')
            uid=accounts.create(args.auth_db,args.username,password,args.reset,label=args.label)
            if args.claim_local_data: accounts.claim(args.auth_db,uid,args.db)
            print('Account saved: '+args.username)
            return 0
        if args.command == 'serve':
            from .web import serve
            serve(args.db,args.host,args.port,auth=args.auth,auth_database=args.auth_db,public_url=args.public_url)
            return 0
        if args.command == 'collect-auto':
            # A mistyped account path must not quietly create another, empty
            # account database or start collecting for the wrong installation.
            auth_path = Path(args.auth_db).resolve()
            if not auth_path.is_file():
                raise ValueError('Create a login account first; the automatic collector needs an existing --auth-db.')
            with sqlite3.connect(auth_path.as_uri() + '?mode=ro', uri=True) as connection:
                if not connection.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]:
                    raise ValueError('Create a login account before starting the automatic collector.')
            from .worker import run, WorkerAlreadyRunning
            stopped = threading.Event()
            previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
            try:
                for sig in previous:
                    signal.signal(sig, lambda *_: stopped.set())
                print(json.dumps({'state': 'starting', 'database': args.db,
                    'live_interval': args.live_interval, 'ranking_interval': args.ranking_interval}), flush=True)
                try:
                    result = run(args.db, args.auth_db, stop_event=stopped, concurrency=args.concurrency,
                        live_interval=args.live_interval, ranking_interval=args.ranking_interval)
                except WorkerAlreadyRunning:
                    result = {'state': 'already_running', 'message': 'Existing automatic collector kept running.'}
                print(json.dumps(result, ensure_ascii=False), flush=True)
                return 0
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
        Path(args.db).parent.mkdir(parents=True, exist_ok=True)
        initialize(args.db)
        if args.command == "init-db":
            print(json.dumps({"database": args.db, "initialized": True}))
        elif args.command == "import":
            payload = json.loads(args.file.read_text(encoding="utf-8"))
            print(json.dumps({"snapshot_id": save_snapshot(args.db, payload)}))
        elif args.command == "report":
            query = broadcaster_listeners if args.direction == "broadcaster" else listener_broadcasters
            print(json.dumps(query(args.db, args.id), ensure_ascii=False, indent=2))
        elif args.command == "history":
            print(json.dumps(temperature_history(args.db,args.broadcaster_id,args.listener_id), ensure_ascii=False, indent=2))
        else:
            from .collector import fetch_many
            stopped = threading.Event()
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: stopped.set())
            while not stopped.is_set():
                failed = False
                if args.command=='collect-gifts':
                    from .gifts import collect_gifts
                    from .collection_status import begin,finish
                    retry=0
                    for uid in args.dj_id:
                        if stopped.is_set():break
                        rid=begin(args.db,'gifts',uid,args.interval)
                        active_run=rid
                        summary=collect_gifts(args.db,uid,max_pages=args.max_pages,stopped_event=stopped)
                        finish(args.db,rid,summary)
                        active_run=None
                        print(json.dumps(summary,ensure_ascii=False),flush=True)
                        failed=failed or bool(summary['errors'])
                        retry=max(retry,summary.get('retry_after',0))
                        if retry or stopped.is_set():break
                    if args.once:return 1 if failed else 0
                    stopped.wait(max(args.interval,retry));continue
                if args.command == 'collect-monthly':
                    from .monthly import collect_monthly
                    from .collection_status import begin,finish
                    rid=begin(args.db,'monthly',','.join(args.dj_id or []),args.interval)
                    active_run=rid
                    summary=collect_monthly(args.db,max_djs=args.max_djs,concurrency=args.concurrency,max_pages=args.max_pages,
                        progress_callback=lambda progress: print(json.dumps(progress),flush=True),stopped_event=stopped,**({'dj_ids':args.dj_id} if args.dj_id else {}))
                    finish(args.db,rid,summary)
                    active_run=None
                    print(json.dumps(summary,ensure_ascii=False),flush=True)
                    if args.once:
                        return 1 if summary['errors'] else 0
                    stopped.wait(max(args.interval,summary.get('retry_after',0)))
                    continue
                if args.command == "collect-spoon":
                    from .spoon import collect_spoon, SpoonRateLimit
                    from .collection_status import begin,finish
                    rid=begin(args.db,'live','',args.interval)
                    active_run=rid
                    try:
                        results, errors = collect_spoon(concurrency=args.concurrency, max_rooms=args.max_rooms, max_pages=args.max_pages)
                    except SpoonRateLimit as exc:
                        finish(args.db,rid,{'errors':['HTTP429'],'retry_after':exc.retry_after},failed=True)
                        active_run=None
                        print(f"Rate limited; retry delay {exc.retry_after} seconds", file=sys.stderr, flush=True)
                        if args.once:
                            return 1
                        stopped.wait(max(args.interval, exc.retry_after))
                        continue
                    for error in errors:
                        print(f"Collection failed: {error}", file=sys.stderr)
                    failed = bool(errors)
                else:
                    results = fetch_many(args.url, concurrency=args.concurrency)
                saved_count=0
                for result in results:
                    if isinstance(result, Exception):
                        failed = True
                        print(f"Collection failed: {result}", file=sys.stderr)
                        continue
                    try:
                        print(json.dumps({"snapshot_id": save_snapshot(args.db, result)}), flush=True)
                        saved_count+=1
                    except (ValueError, TypeError, KeyError) as exc:
                        failed = True
                        print(f"Invalid snapshot: {exc}", file=sys.stderr)
                if args.command=='collect-spoon':
                    finish(args.db,rid,{'complete':not failed and all(r.get('complete') is True for r in results if isinstance(r,dict)),'room_count':saved_count,'errors':errors,
                      'max_rooms':args.max_rooms,'max_pages':args.max_pages,
                      'coverage':'selected_public_rooms' if args.max_rooms else 'listed_public_rooms'})
                    active_run=None
                if args.once:
                    return 1 if failed else 0
                stopped.wait(args.interval)
        return 0
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if active_run is not None:
            from .collection_status import finish
            try:
                finish(args.db,active_run,{'errors':['Collection ended before its result was saved.']},failed=True)
            except (OSError,ValueError,TypeError,sqlite3.Error) as exc:
                print(f'Could not save collection outcome: {exc}',file=sys.stderr)
