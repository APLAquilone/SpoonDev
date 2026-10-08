import argparse
import json
import signal
import sys
import threading
from pathlib import Path

from .db import initialize, save_snapshot, broadcaster_listeners, listener_broadcasters


def main(argv=None):
    parser = argparse.ArgumentParser(description="ID-based listener observation database")
    parser.add_argument("--db", default="data/spoondev.sqlite3")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db")
    imp = commands.add_parser("import")
    imp.add_argument("file", type=Path)
    report = commands.add_parser("report")
    report.add_argument("direction", choices=["broadcaster", "listener"])
    report.add_argument("id")
    collect = commands.add_parser("collect", description="Fetch canonical snapshots from a verified adapter, not an unverified Spoon API")
    collect.add_argument("--url", action="append", required=True)
    collect.add_argument("--concurrency", type=int, default=4)
    collect.add_argument("--interval", type=float, default=300)
    collect.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "collect" and (not 1 <= args.concurrency <= 16 or args.interval < 30):
        parser.error("concurrency must be 1..16; interval must be at least 30 seconds")
    try:
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
        else:
            from .collector import fetch_many
            stopped = threading.Event()
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: stopped.set())
            while not stopped.is_set():
                failed = False
                for result in fetch_many(args.url, concurrency=args.concurrency):
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
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
