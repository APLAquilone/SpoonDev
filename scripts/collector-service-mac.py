#!/usr/bin/env python3
"""Install a user-owned launchd collector, independently of web and tunnel.

The generated job is tied to this checkout's absolute paths. No account
database is copied, and other checkouts' services are never stopped.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import subprocess
import sys


def configuration(root, python):
    root = Path(root).resolve()
    label = 'net.spooninsights.collector.' + hashlib.sha256(str(root).encode()).hexdigest()[:12]
    return {'label': label, 'root': str(root), 'python': str(Path(python).resolve()),
            'db': str(root / 'data/spoondev.sqlite3')}


def job(config):
    root = Path(config['root'])
    return {'Label': config['label'], 'ProgramArguments': [config['python'],
            str(root / 'scripts/supervise-collector.py'), '--db', config['db'],
            '--auth-db', str(root / 'data/accounts.sqlite3')],
            'WorkingDirectory': str(root), 'RunAtLoad': True,
            'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval': 15,
            'ProcessType': 'Background',
            'StandardOutPath': str(root / 'data/collector-service.log'),
            'StandardErrorPath': str(root / 'data/collector-service.log')}


def verify(root):
    if sys.version_info < (3, 12):
        raise ValueError('Activate the spoondev Python 3.12 environment first')
    if not (root / 'scripts/supervise-collector.py').is_file():
        raise ValueError('Update this checkout to v0.3.0 first')
    for database, table in ((root / 'data/spoondev.sqlite3', 'snapshots'),
                            (root / 'data/accounts.sqlite3', 'accounts')):
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as conn:
            count = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            if table == 'accounts' and not count:
                raise ValueError('Create a login account in this checkout first')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'status', 'remove'))
    parser.add_argument('--print-plist', action='store_true', help='Print only; do not install or start a service')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    config = configuration(root, sys.executable)
    if args.print_plist:
        sys.stdout.buffer.write(plistlib.dumps(job(config)))
        return 0
    if sys.platform != 'darwin':
        parser.error('This command is for macOS; use --print-plist to inspect without installing')
    domain = f'gui/{os.getuid()}'
    target = domain + '/' + config['label']
    plist = Path.home() / 'Library/LaunchAgents' / (config['label'] + '.plist')
    marker = root / 'data/collector-service.json'
    if args.action == 'status':
        return subprocess.run(['launchctl', 'print', target]).returncode
    if args.action == 'remove':
        # A root-derived service ID prevents one checkout resetting another.
        subprocess.run(['launchctl', 'bootout', target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        plist.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
        print('Collector service removed for: ' + str(root))
        return 0
    try:
        verify(root)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.error(str(exc))
    os.umask(0o077)
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps(job(config)))
    plist.chmod(0o600)
    # Refresh only this exact job. A separately running collector retains its
    # DB lock until its owner stops it; the service waits rather than kills it.
    subprocess.run(['launchctl', 'bootout', target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    result = subprocess.run(['launchctl', 'bootstrap', domain, str(plist)])
    if result.returncode:
        return result.returncode
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(config, ensure_ascii=False))
    temporary.chmod(0o600)
    temporary.replace(marker)
    print('Collector service installed for: ' + str(root))
    print('Log: data/collector-service.log')
    print('It starts after Mac login and survives closing the web terminal.')
    print('Stop older manual collectors yourself before using the service.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
