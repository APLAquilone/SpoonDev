#!/usr/bin/env python3
"""Install a user-owned launchd collector, independently of web and tunnel.

The generated job is tied to this checkout's absolute paths. No account
database is copied, and other checkouts' services are never stopped.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import sqlite3
import subprocess
import sys
import time


class ServiceError(RuntimeError):
    """A scoped launchd operation failed; leave other services untouched."""


def launchctl(*arguments):
    try:
        return subprocess.run(['launchctl', *arguments], capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceError('launchctl could not complete: ' + str(exc)) from exc


def require_success(result, operation):
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise ServiceError(f'{operation} failed ({result.returncode}): {detail}')


def service_missing(result, label):
    # An inaccessible GUI domain or another inspection failure is not evidence
    # that the job has finished unloading. launchctl print is not a JSON API.
    message = (result.stderr or '') + '\n' + (result.stdout or '')
    return result.returncode != 0 and re.search(
        r'Could not find service\s+[\"\']?' + re.escape(label) + r'[\"\']?(?:\s|$)',
        message) is not None


def loaded_service(target):
    result = launchctl('print', target)
    if result.returncode == 0:
        return result.stdout
    if service_missing(result, target.rsplit('/', 1)[-1]):
        return None
    require_success(result, 'Inspecting collector service')


def verify_loaded(output, config, plist):
    """Only unload a job whose loaded command and paths belong here.

    Reading the plist alone would verify the proposed job, not the job that
    launchd currently runs. Inspect its actual arguments before bootout.
    """
    fields = {}
    for key in ('path', 'program', 'working directory'):
        matches = re.findall(r'^\s*' + re.escape(key) + r'\s*=\s*(.*?)\s*$', output, re.M)
        if len(matches) != 1:
            raise ServiceError('Loaded collector identity is incomplete: ' + key)
        fields[key] = matches[0]
    match = re.search(r'^\s*arguments\s*=\s*\{\s*\n(.*?)^\s*\}', output, re.M | re.S)
    arguments = [line.strip() for line in match.group(1).splitlines()] if match else []
    expected = job(config)
    if (fields['path'] != str(plist) or fields['program'] != config['python']
            or fields['working directory'] != config['root']
            or arguments != expected['ProgramArguments']):
        raise ServiceError('Loaded service belongs to a different command or checkout; no job was stopped')


def unload_service(target, config, plist, *, timeout=30):
    output = loaded_service(target)
    if output is None:
        return
    verify_loaded(output, config, plist)
    result = launchctl('bootout', target)
    if result.returncode:
        # The job may have exited between print and bootout. Confirm absence
        # with a new read; an error code alone never means removal succeeded.
        if loaded_service(target) is None:
            return
        require_success(result, 'Unloading collector service')
    deadline = time.monotonic() + timeout
    while loaded_service(target) is not None:
        if time.monotonic() >= deadline:
            raise ServiceError('Collector service is still unloading; no replacement was started. Retry install after it exits')
        time.sleep(.25)


def save_plist(plist, payload):
    plist.parent.mkdir(parents=True, exist_ok=True)
    temporary = plist.with_suffix('.plist.tmp')
    temporary.write_bytes(plistlib.dumps(payload))
    temporary.chmod(0o600)
    temporary.replace(plist)


def save_marker(marker, config):
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(config, ensure_ascii=False))
    temporary.chmod(0o600)
    temporary.replace(marker)


def install(config, domain, plist, marker):
    target = domain + '/' + config['label']
    require_success(launchctl('print', domain), 'Inspecting the current login GUI domain')
    # Wait for the old job to disappear instead of immediately bootstrapping
    # into a label launchd is still removing. Do not suppress bootout errors.
    unload_service(target, config, plist)
    save_plist(plist, job(config))
    # Explicit install opts this exact service back in if launchd retained a
    # disabled override. Never enable another checkout's label or use sudo.
    require_success(launchctl('enable', target), 'Enabling collector service')
    result = launchctl('bootstrap', domain, str(plist))
    require_success(result, 'Registering collector service')
    output = loaded_service(target)
    if output is None:
        raise ServiceError('Registration returned success but the collector service is not loaded')
    verify_loaded(output, config, plist)
    save_marker(marker, config)


def recovery_commands(domain, target, plist):
    print('Run these read-only checks from the same Mac login account:', file=sys.stderr)
    for command in (['launchctl', 'print', target], ['plutil', '-lint', str(plist)],
                    ['launchctl', 'print-disabled', domain]):
        print('  ' + shlex.join(command), file=sys.stderr)
    print('Do not retry as root. The web server and tunnel were not stopped.', file=sys.stderr)


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
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
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
        try:
            unload_service(target, config, plist)
            plist.unlink(missing_ok=True)
            marker.unlink(missing_ok=True)
        except (OSError, ServiceError) as exc:
            print('Collector operation stopped: ' + str(exc), file=sys.stderr)
            recovery_commands(domain, target, plist)
            return 1
        print('Collector service removed for: ' + str(root))
        return 0
    try:
        verify(root)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.error(str(exc))
    os.umask(0o077)
    try:
        # A separate manual collector keeps its DB lease until its owner stops
        # it; the native supervisor waits for that lease, rather than kills it.
        install(config, domain, plist, marker)
    except (OSError, ServiceError) as exc:
        print('Collector operation stopped: ' + str(exc), file=sys.stderr)
        recovery_commands(domain, target, plist)
        return 1
    print('Collector service installed for: ' + str(root))
    print('Log: data/collector-service.log')
    print('It starts after Mac login and survives closing the web terminal.')
    print('Stop older manual collectors yourself before using the service.')
    print('Service registration is not proof of saved observations; verify that the latest observation advances.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
