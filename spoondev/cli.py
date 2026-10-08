import argparse
import json
import signal
import sqlite3
import sys
import threading
from pathlib import Path

from .db import initialize, save_snapshot, broadcaster_listeners, listener_broadcasters, temperature_history


def main(argv=None):
    parser = argparse.ArgumentParser(description="ID-based listener observation database")
    parser.add_argument("--db", default="data/spoondev.sqlite3")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db")
    web = commands.add_parser("serve", help="Start the read-only search website")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8080)
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
    monthly.add_argument('--max-pages',type=int,default=100)
    monthly.add_argument('--concurrency',type=int,default=4)
    monthly.add_argument('--interval',type=float,default=3600)
    monthly.add_argument('--once',action='store_true')
    args = parser.parse_args(argv)
    if args.command == 'serve' and not 0 <= args.port <= 65535:
        parser.error('port must be 0..65535')
    if args.command in {"collect", "collect-spoon",'collect-monthly'} and (not 1 <= args.concurrency <= 16 or args.interval < 30):
        parser.error("concurrency must be 1..16; interval must be at least 30 seconds")
    if args.command == "collect-spoon" and (args.max_rooms < 0 or args.max_pages < 1):
        parser.error("max-rooms must be nonnegative; max-pages must be positive")
    if args.command == 'collect-monthly' and (args.max_djs < 0 or args.max_pages < 1):
        parser.error('max-djs must be nonnegative; max-pages must be positive')
    try:
        if args.command == 'serve':
            from .web import serve
            serve(args.db,args.host,args.port)
            return 0
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
                if args.command == 'collect-monthly':
                    from .monthly import collect_monthly
                    summary=collect_monthly(args.db,max_djs=args.max_djs,concurrency=args.concurrency,max_pages=args.max_pages,
                        progress_callback=lambda progress: print(json.dumps(progress),flush=True),stopped_event=stopped)
                    print(json.dumps(summary,ensure_ascii=False),flush=True)
                    if args.once:
                        return 1 if summary['errors'] else 0
                    stopped.wait(max(args.interval,summary.get('retry_after',0)))
                    continue
                if args.command == "collect-spoon":
                    from .spoon import collect_spoon, SpoonRateLimit
                    try:
                        results, errors = collect_spoon(concurrency=args.concurrency, max_rooms=args.max_rooms, max_pages=args.max_pages)
                    except SpoonRateLimit as exc:
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
                for result in results:
                    if isinstance(result, Exception):
                        failed = True
                        print(f"Collection failed: {result}", file=sys.stderr)
                        continue
                    try:
                        print(json.dumps({"snapshot_id": save_snapshot(args.db, result)}), flush=True)
                    except (ValueError, TypeError, KeyError) as exc:
                        failed = True
                        print(f"Invalid snapshot: {exc}", file=sys.stderr)
                if args.once:
                    return 1 if failed else 0
                stopped.wait(args.interval)
        return 0
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
