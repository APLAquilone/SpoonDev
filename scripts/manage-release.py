"""Local release backups and explicit code rollback; never restore private DBs.

The public and Dev checkouts must be separate repositories. All operations are
local: this program does not push main, publish a tunnel, or copy Dev data.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def repository(root):
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"Repository must be a real directory: {root}")
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root.resolve():
        raise ValueError(f"Expected a repository root: {root}")
    if git(root, "status", "--porcelain"):
        raise ValueError(f"Commit or save uncommitted changes first: {root}")
    if git(root, "ls-files", "--", "data"):
        raise ValueError(f"Production data must not be tracked by Git: {root}/data")


def write_json(path, value):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".manifest-", delete=False) as handle:
        temporary = Path(handle.name)
        os.chmod(temporary, 0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def launcher(root):
    """Verify a launcher PID exactly as reload-public-mac.sh does."""
    path = root / "data/public-run.json"
    if not path.is_file():
        return None
    state = json.loads(path.read_text())
    if not isinstance(state, dict):
        raise ValueError("Invalid public launcher metadata; no process was signaled")
    pid = state.get("pid")
    if type(pid) is not int or pid <= 1 or state.get("root") != str(root):
        raise ValueError("Invalid public launcher metadata; no process was signaled")
    try:
        started = subprocess.check_output(["ps", "-p", str(pid), "-o", "lstart="], text=True).strip()
        command = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True).strip()
    except subprocess.CalledProcessError:
        return None
    if started != state.get("started") or "start-public-mac.sh" not in command:
        raise ValueError("Public launcher identity mismatch; no process was signaled")
    if not (root / "scripts/reload-public-mac.sh").is_file():
        raise ValueError("This launcher cannot reload safely; stop the public web launcher first")
    return state


def deployment_mode(root):
    state = launcher(root)
    if state:
        return state
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", 8080))
        except OSError as exc:
            raise ValueError("Port 8080 is occupied without a verified public launcher. Stop only the web process first; collectors may keep running.") from exc
    return None


def databases(root):
    """Find supported SQLite files without traversing symlinks or old backups."""
    folder = root / "data"
    if folder.is_symlink():
        raise ValueError("The data directory must not be a symlink")
    found = []
    if not folder.exists():
        return found
    for current, dirs, files in os.walk(folder, followlinks=False):
        current = Path(current)
        for name in list(dirs):
            directory = current / name
            if directory.is_symlink():
                raise ValueError(f"Symlink in production data is unsupported: {directory}")
            if current == folder and name == "backups":
                dirs.remove(name)
        for name in files:
            path = current / name
            if path.is_symlink():
                raise ValueError(f"Symlink in production data is unsupported: {path}")
            if path.suffix in (".sqlite3", ".sqlite", ".db"):
                with path.open("rb") as handle:
                    if handle.read(16) != b"SQLite format 3\x00":
                        raise ValueError(f"Not a readable SQLite database: {path}")
                found.append(path)
    return sorted(found)


def copy_database(source, destination, timeout):
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Every intermediate directory is private, including nested account UUIDs.
    for folder in (destination.parent, *destination.parents):
        folder.chmod(0o700)
        if folder.name == "databases":
            break
    destination.touch(mode=0o600, exist_ok=False)
    deadline = time.monotonic() + timeout
    last_progress = time.monotonic()

    def progress(_status, remaining, total):
        nonlocal last_progress
        now = time.monotonic()
        if now >= deadline:
            raise TimeoutError(f"SQLite backup timed out: {source}. Retry during quieter collection or increase --backup-timeout.")
        if now - last_progress >= 10:
            print(f"  {total-remaining:,}/{total:,} pages copied", flush=True)
            last_progress = now

    # A native SQLite backup reads a consistent snapshot with an active WAL
    # collector; copying only the .sqlite3 file would lose WAL writes.
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=15)) as src:
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst, pages=512, progress=progress, sleep=0.05)
            dst.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10000)
            result = dst.execute("PRAGMA quick_check(1)").fetchone()[0]
            if result != "ok":
                raise ValueError(f"Backup validation failed for {source}: {result}")
    destination.chmod(0o600)


def backup(root, release_commit, timeout, source=None):
    previous = git(root, "rev-parse", "HEAD")
    files = databases(root)
    folder = root / "data/backups"
    if folder.is_symlink():
        raise ValueError("The backups directory must not be a symlink")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    identifier = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + previous[:10] + "-" + secrets.token_hex(3)
    target = folder / identifier
    target.mkdir(mode=0o700)
    estimate = sum(p.stat().st_size + (Path(str(p) + "-wal").stat().st_size if Path(str(p) + "-wal").exists() else 0) for p in files)
    if shutil.disk_usage(target).free < estimate * 1.2 + 64 * 1024 * 1024:
        raise ValueError("Not enough free disk space for a production backup; code was not updated")
    before_ref = "refs/sci/releases/" + identifier + "/before"
    after_ref = "refs/sci/releases/" + identifier + "/after"
    subprocess.run(["git", "update-ref", before_ref, previous], cwd=root, check=True)
    subprocess.run(["git", "update-ref", after_ref, release_commit], cwd=root, check=True)
    manifest = {"version": 1, "state": "backup_in_progress", "production_root": str(root),
                "source_root": str(source) if source else None,
                "previous_commit": previous, "release_commit": release_commit,
                "previous_branch": git(root, "branch", "--show-current"),
                "git_refs": {"before": before_ref, "after": after_ref},
                "started_at": stamp(), "databases": []}
    path = target / "manifest.json"
    write_json(path, manifest)
    completed = set()
    try:
        # Include private DBs added during backup, without promising an atomic
        # instant across independently written account and observation DBs.
        for _ in range(3):
            pending = [p for p in databases(root) if p not in completed]
            if not pending:
                break
            for database in pending:
                relative = database.relative_to(root)
                copied = target / "databases" / relative
                print(f"Backing up {relative} ({database.stat().st_size:,} bytes)", flush=True)
                began = stamp()
                copy_database(database, copied, timeout)
                manifest["databases"].append({"source": str(relative), "backup": str(copied.relative_to(target)),
                                              "started_at": began, "completed_at": stamp(), "bytes": copied.stat().st_size})
                completed.add(database)
                write_json(path, manifest)
        if set(databases(root)) - completed:
            raise ValueError("Private databases are being added during backup; retry after account creation completes")
    except BaseException:
        manifest.update(state="backup_failed", completed_at=stamp())
        write_json(path, manifest)
        raise
    manifest.update(state="backup_complete", completed_at=stamp())
    write_json(path, manifest)
    print(f"Backup ready: {target}", flush=True)
    return target, manifest


def reload_or_instructions(root, running):
    if running:
        subprocess.run(["bash", "scripts/reload-public-mac.sh"], cwd=root, check=True)
        print("The tunnel was kept running; verify web startup in the public terminal.")
    else:
        print(f"Web is stopped. Start it from {root}: bash scripts/start-public-mac.sh")
        print("A newly started Quick Tunnel has a new URL.")


def release(root, timeout):
    if not root.name.endswith("-dev"):
        raise ValueError("Run release-mac.sh inside the separate SpoonDev-dev checkout")
    target = root.with_name(root.name[:-4])
    repository(root)
    repository(target)
    source_common = (root / git(root, "rev-parse", "--git-common-dir")).resolve()
    target_common = (target / git(target, "rev-parse", "--git-common-dir")).resolve()
    if source_common == target_common:
        raise ValueError("Dev and production must be independent repositories")
    running = deployment_mode(target)
    commit = git(root, "rev-parse", "HEAD")
    if git(root, "ls-tree", "-r", "--name-only", commit, "--", "data"):
        raise ValueError("The incoming commit contains tracked data; production was preserved")
    subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"], cwd=root, check=True)
    repository(root)
    if git(root, "rev-parse", "HEAD") != commit:
        raise ValueError("Dev commit changed during tests; retry the release")
    subprocess.run(["git", "fetch", "--no-tags", str(root), commit], cwd=target, check=True)
    subprocess.run(["git", "merge-base", "--is-ancestor", "HEAD", commit], cwd=target, check=True)
    saved, manifest = backup(target, commit, timeout, source=root)
    repository(target)
    if git(target, "rev-parse", "HEAD") != manifest["previous_commit"]:
        raise ValueError("Production commit changed during backup; code was not released")
    subprocess.run(["git", "merge", "--ff-only", commit], cwd=target, check=True)
    manifest.update(state="released", released_at=stamp())
    write_json(saved / "manifest.json", manifest)
    try:
        reload_or_instructions(target, running)
    except subprocess.CalledProcessError:
        manifest.update(state="released_reload_failed")
        write_json(saved / "manifest.json", manifest)
        print(f"Code updated but reload failed. Backup and rollback reference: {saved}", file=sys.stderr)
        raise
    print(f"Released {commit}; previous {manifest['previous_commit']}. Production data was preserved.")


def rollback(root, selected, timeout):
    if root.name.endswith("-dev"):
        raise ValueError("Run rollback-public-mac.sh in production, not Dev")
    repository(root)
    selected = selected.resolve()
    base = (root / "data/backups").resolve()
    if selected.parent != base or not (selected / "manifest.json").is_file():
        raise ValueError("Select one production backup directory under data/backups")
    manifest = json.loads((selected / "manifest.json").read_text())
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or manifest.get("production_root") != str(root):
        raise ValueError("Backup belongs to a different production checkout")
    previous = manifest.get("previous_commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", previous) or git(root, "cat-file", "-t", previous) != "commit":
        raise ValueError("Backup does not identify an available commit")
    if git(root, "ls-tree", "-r", "--name-only", previous, "--", "data"):
        raise ValueError("Rollback commit contains tracked data; production was preserved")
    if git(root, "rev-parse", "HEAD") != manifest.get("release_commit"):
        raise ValueError("Production is not at this backup's release commit; choose the matching release backup")
    if manifest.get("state") not in ("released", "released_reload_failed"):
        raise ValueError("This directory does not describe a completed release")
    running = deployment_mode(root)
    saved, rollback_manifest = backup(root, previous, timeout)
    repository(root)
    if git(root, "rev-parse", "HEAD") != rollback_manifest["previous_commit"]:
        raise ValueError("Production changed during backup; rollback was not applied")
    # Explicit rollback only. --keep refuses to overwrite local changes; ignored
    # data files and all database schemas/contents remain exactly where they are.
    subprocess.run(["git", "reset", "--keep", previous], cwd=root, check=True)
    rollback_manifest.update(state="code_rolled_back", rolled_back_at=stamp(), rollback_of=str(selected))
    write_json(saved / "manifest.json", rollback_manifest)
    reload_or_instructions(root, running)
    print(f"Code rolled back to {previous}. Databases were NOT restored or deleted.")
    print(f"Pre-rollback backup: {saved}")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("release", "backup", "rollback"))
    parser.add_argument("backup_directory", nargs="?")
    parser.add_argument("--backup-timeout", type=float, default=600,
                        help="Maximum backup plus validation seconds per SQLite DB (default: 600)")
    args = parser.parse_args()
    if sys.version_info < (3, 12):
        parser.error("Activate the spoondev Python 3.12 environment first")
    if not 1 <= args.backup_timeout <= 86400:
        parser.error("backup-timeout must be 1..86400 seconds")
    if (args.operation == "rollback") != bool(args.backup_directory):
        parser.error("Only rollback requires a backup directory")
    root = Path.cwd().resolve()
    try:
        if args.operation == "release":
            release(root, args.backup_timeout)
        elif args.operation == "rollback":
            rollback(root, Path(args.backup_directory), args.backup_timeout)
        else:
            if root.name.endswith("-dev"):
                raise ValueError("Run backup-public-mac.sh in production, not Dev")
            repository(root)
            backup(root, git(root, "rev-parse", "HEAD"), args.backup_timeout)
    except (ValueError, OSError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        print(f"Release operation stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
