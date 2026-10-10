#!/usr/bin/env python3
"""Inspect, or gracefully stop, only this checkout's verified collector.

Default operation is read-only. --stop requires the PID explicitly selected
by the operator. No web server, tunnel, launcher, manual collector, or unknown
process is signaled. No SIGKILL is used. This helper does not install/start a
replacement: chain collector-service-mac.py install after successful stopping.

The process checks are unit-tested here; actual macOS stopping is not tested
from the Linux cloud workspace. Insufficient identity evidence fails closed.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
import sqlite3
import subprocess
import sys
import time


class UnsafeCollector(ValueError):
    """Collector identity is absent or could not be established safely."""


def paths(root):
    root = Path(root).resolve()
    if any(character in str(root) for character in ('\n', '\r', '\0')):
        raise UnsafeCollector('Unsupported checkout path')
    database = root / 'data/spoondev.sqlite3'
    auth_database = root / 'data/accounts.sqlite3'
    if any(path.is_symlink() for path in (root / 'data', database, auth_database)):
        raise UnsafeCollector('Collector data paths must not be symlinks to another installation')
    return root, database, auth_database


def state(database):
    """Read persistent identity and progress without creating/migrating a DB."""
    with closing(sqlite3.connect(Path(database).as_uri() + '?mode=ro', uri=True, timeout=5)) as conn:
        row = conn.execute('''SELECT pid,owner_token,started_at,heartbeat_at,state,stopped_at
            FROM worker_state WHERE singleton=1''').fetchone()
        if row is None:
            raise UnsafeCollector('No automatic collector identity is recorded')
        result = dict(zip(('pid', 'owner_token', 'started_at', 'heartbeat_at', 'state', 'stopped_at'), row))
        result['last_listener_observation'] = conn.execute('SELECT MAX(observed_at) FROM snapshots').fetchone()[0]
        result['recent_snapshot_count'] = conn.execute("""SELECT COUNT(*) FROM snapshots
            WHERE observed_at>=? AND observed_at<=?""", (
            datetime.fromtimestamp(time.time()-1800, timezone.utc).isoformat(timespec='microseconds'),
            datetime.now(timezone.utc).isoformat(timespec='microseconds'))).fetchone()[0]
        return result


def run_inspection(arguments):
    env = dict(os.environ, LC_ALL='C', TZ='UTC')
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=5, env=env)
    if result.returncode:
        raise UnsafeCollector('Process evidence unavailable: ' + arguments[0])
    return result.stdout


def process(pid):
    # UID, parent, start time and full command are captured in the same read.
    output = run_inspection(['ps', '-ww', '-p', str(pid), '-o', 'uid=,ppid=,lstart=,command='])
    parts = output.strip().split(None, 7)
    if len(parts) != 8:
        raise UnsafeCollector('Unrecognized process identity')
    try:
        started = datetime.strptime(' '.join(parts[2:7]), '%a %b %d %H:%M:%S %Y').replace(tzinfo=timezone.utc)
        return {'uid': int(parts[0]), 'parent': int(parts[1]), 'started': started.isoformat(), 'command': parts[7]}
    except (ValueError, TypeError) as exc:
        raise UnsafeCollector('Unrecognized process start time') from exc


def file_records(output):
    """Parse lsof fields, including paths with spaces, without shell splitting."""
    pid = None
    record = None
    records = []
    for line in output.splitlines():
        if not line:
            continue
        field, value = line[0], line[1:]
        if field in ('p', 'f'):
            if record is not None:
                records.append(record)
            record = None
            if field == 'p':
                try:
                    pid = int(value)
                except ValueError as exc:
                    raise UnsafeCollector('Unrecognized lsof process ID') from exc
            else:
                record = {'pid': pid, 'fd': value}
        elif record is not None:
            record[field] = value
    if record is not None:
        records.append(record)
    return records


def process_cwd(pid):
    output = run_inspection(['lsof', '-a', '-p', str(pid), '-d', 'cwd', '-F', 'pfn'])
    records = [record for record in file_records(output)
               if record['pid'] == pid and record['fd'] == 'cwd' and record.get('n')]
    if len(records) != 1:
        raise UnsafeCollector('Collector working directory could not be verified')
    return Path(records[0]['n']).resolve()


def holds_open_lock(pid, lock_path):
    output = run_inspection(['lsof', '-a', '-p', str(pid), '-F', 'pfn', '--', str(lock_path)])
    return any(record['pid'] == pid and re.fullmatch(r'[0-9]+[A-Za-z]*', record['fd'])
               and record.get('n') == str(lock_path) for record in file_records(output))


def lock_busy(database):
    """Probe the existing OS lease; never create, truncate, or unlink its file."""
    lock_path = Path(str(database) + '.worker.lock')
    if lock_path.is_symlink():
        raise UnsafeCollector('Worker lease must not be a symlink')
    fd = os.open(lock_path, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def command_kind(command, root, database, auth_database):
    """Accept only the two dedicated automatic collector entry points."""
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise UnsafeCollector('Unrecognized collector command') from exc
    if not tokens or not re.fullmatch(r'python(?:[0-9]+(?:\.[0-9]+)*)?', Path(tokens[0]).name):
        raise UnsafeCollector('The recorded process is not a dedicated Python collector')
    parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
    if tokens[1:3] == ['-m', 'spoondev']:
        parser.add_argument('--db', default='data/spoondev.sqlite3')
        commands = parser.add_subparsers(dest='command', required=True)
        collector = commands.add_parser('collect-auto', add_help=False, exit_on_error=False)
        arguments = tokens[3:]
        kind = 'collect-auto'
    elif len(tokens) >= 2 and (root / tokens[1]).resolve() == root / 'scripts/supervise-collector.py':
        collector = parser
        collector.add_argument('--db', default='data/spoondev.sqlite3')
        collector.add_argument('--skip-loaded-service', action='store_true')
        arguments = tokens[2:]
        kind = 'supervisor'
    else:
        raise UnsafeCollector('Process is not collect-auto or this checkout\'s supervisor')
    collector.add_argument('--auth-db', default='data/accounts.sqlite3')
    collector.add_argument('--concurrency', type=int, default=4)
    collector.add_argument('--live-interval', type=float, default=300)
    collector.add_argument('--ranking-interval', type=float, default=3600)
    try:
        args, unknown = parser.parse_known_args(arguments)
    except (argparse.ArgumentError, SystemExit) as exc:
        raise UnsafeCollector('Unrecognized automatic collector arguments') from exc
    if unknown:
        raise UnsafeCollector('Unrecognized automatic collector arguments')
    if (root / args.db).resolve() != database or (root / args.auth_db).resolve() != auth_database:
        raise UnsafeCollector('Collector database paths belong to another installation')
    return kind


def verify(root, selected_pid=None):
    root, database, auth_database = paths(root)
    recorded = state(database)
    pid = recorded['pid']
    if (type(pid) is not int or pid <= 1 or pid == os.getpid() or
            not isinstance(recorded['owner_token'], str) or
            not re.fullmatch(r'[0-9a-f]{32}', recorded['owner_token']) or
            recorded['state'] != 'running' or recorded['stopped_at'] is not None):
        raise UnsafeCollector('No running automatic collector owner is recorded')
    if selected_pid is not None and selected_pid != pid:
        raise UnsafeCollector('Collector PID changed; no process was signaled')
    current = process(pid)
    if current['uid'] != os.getuid() or process_cwd(pid) != root:
        raise UnsafeCollector('Collector user or working directory does not match this checkout')
    kind = command_kind(current['command'], root, database, auth_database)
    try:
        worker_started = datetime.fromisoformat(recorded['started_at'])
        if worker_started.utcoffset() is None:
            raise ValueError('Missing timezone')
        age = (worker_started - datetime.fromisoformat(current['started'])).total_seconds()
        if age < -2 or (kind == 'collect-auto' and age > 30) or worker_started.timestamp() > time.time()+5:
            raise ValueError('Start time mismatch')
    except (ValueError, TypeError) as exc:
        raise UnsafeCollector('Process start time does not match the recorded worker owner') from exc
    lock_path = Path(str(database) + '.worker.lock')
    if not holds_open_lock(pid, lock_path) or not lock_busy(database):
        raise UnsafeCollector('The recorded collector does not hold this database\'s OS lease')
    # Account DB must exist, but this tool does not read or alter credentials.
    if not auth_database.is_file():
        raise UnsafeCollector('The checkout account database is missing')
    return {'root': str(root), 'database': str(database), 'recorded': recorded,
            'process': current, 'kind': kind}


def identity(evidence):
    recorded = evidence['recorded']
    return (evidence['root'], evidence['database'], recorded['pid'], recorded['owner_token'],
            recorded['started_at'], evidence['process'], evidence['kind'])


def process_exists(pid):
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        raise UnsafeCollector('Process exit could not be verified') from exc
    return True


def stop(root, selected_pid, *, timeout=60):
    if type(selected_pid) is not int or selected_pid <= 1:
        raise UnsafeCollector('--stop requires an explicit valid --pid')
    if not math.isfinite(timeout) or not 1 <= timeout <= 300:
        raise UnsafeCollector('Stop timeout must be 1..300 seconds')
    evidence = verify(root, selected_pid)
    # Recheck process start, command, cwd, open lease, DB owner token and PID
    # immediately before signaling. A reused PID or new lease owner fails closed.
    checked = verify(root, selected_pid)
    if identity(evidence) != identity(checked):
        raise UnsafeCollector('Collector identity changed; no process was signaled')
    os.kill(selected_pid, signal.SIGTERM)
    print(f'Graceful stop requested for verified collector PID {selected_pid}. Web and tunnel remain running.', flush=True)
    deadline = time.monotonic() + timeout
    database = Path(evidence['database'])
    while True:
        try:
            current = process(selected_pid)
        except UnsafeCollector:
            if process_exists(selected_pid):
                raise UnsafeCollector('Process evidence became unavailable while stopping; no further signals sent')
            current = None
        if current is not None and current != evidence['process']:
            raise UnsafeCollector('Collector process identity changed while stopping; no further signals sent')
        if current is None and not lock_busy(database):
            print('Collector exited and its OS lease was released. Start the replacement service next.', flush=True)
            return evidence
        if time.monotonic() >= deadline:
            raise UnsafeCollector('Collector did not exit and release its lease in time. No forced termination was attempted; do not start another collector yet.')
        time.sleep(0.25)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stop', action='store_true', help='Gracefully stop a verified collector; default only inspects')
    parser.add_argument('--pid', type=int, help='Expected collector PID; required with --stop')
    parser.add_argument('--timeout', type=float, default=60, help='Maximum graceful-stop wait, 1..300 seconds')
    args = parser.parse_args(argv)
    if sys.platform != 'darwin':
        parser.error('This helper is for macOS only')
    root = Path(__file__).resolve().parents[1]
    try:
        if args.stop:
            stop(root, args.pid, timeout=args.timeout)
        else:
            print(json.dumps(verify(root, args.pid), ensure_ascii=False, indent=2))
            print('Inspection only. No process was signaled; a fresh heartbeat alone does not prove listener collection is progressing.')
        return 0
    except (UnsafeCollector, OSError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        print('Collector operation stopped: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
