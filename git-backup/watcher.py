#!/usr/bin/env python3
"""
watcher.py

Watches a source directory and mirrors it into a local git repo using rsync.
After each sync, commits + pushes if there are changes.

Usage:
  sudo uv run python ./watcher.py /home/otheruser/somedir /path/to/local/repo
"""

import argparse
import sys
import subprocess
import threading
import time
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from git import Repo, InvalidGitRepositoryError, GitCommandError


class BackupManager:
    """
    Manages the backup process: rsync + git commit/push.
    """

    def __init__(self, source: Path, target_repo: Path):
        self.source = source
        self.target_repo = target_repo

    def run_rsync(self) -> None:
        """
        Rsync source -> target_repo, mirroring contents.
        Copies the *contents* of source into the repo root.
        """
        # Trailing slash means "copy contents of dir"
        src = str(self.source.resolve()) + "/"
        dst = str(self.target_repo.resolve()) + "/"

        cmd = [
            "rsync",
            "-a",            # archive mode
            "--delete",      # delete files in target that don't exist in source
            "--exclude", ".git/",
            "--exclude", ".git",  # extra safety
            src,
            dst,
        ]

        # rsync returns 0 on success, non-zero on error
        subprocess.run(cmd, check=True)

    def count_updated_files(self, repo: Repo) -> int:
        """
        Count changed files after rsync.
        Uses `git status --porcelain` which yields one line per changed path.
        """
        changed = repo.git.status("--porcelain").splitlines()
        # Each line corresponds to a path. That’s a good "file count" for the message.
        return len([line for line in changed if line.strip()])


    def commit_and_push_if_needed(self) -> None:
        """
        Check for changes, commit if needed, and push.
        """

        repo = Repo(str(self.target_repo))

        updated = self.count_updated_files(repo)
        if updated == 0:
            return

        # Stage everything (including deletions)
        repo.git.add(A=True)

        msg = f"Updated {updated} files"
        repo.index.commit(msg)

        # Push (assumes a configured remote, typically "origin", and current branch set up)
        try:
            # Prefer pushing active branch to its upstream
            repo.git.push()
        except GitCommandError:
            # Fallback: try origin if default push fails
            if "origin" in [r.name for r in repo.remotes]:
                repo.remotes.origin.push()
            else:
                raise

    def sync_once(self) -> None:
        """
        Run a single sync: rsync + git commit/push.
        """

        try:
            self.run_rsync()
        except subprocess.CalledProcessError as e:
            print(f"[watcher] rsync failed: {e}", file=sys.stderr)
            return

        try:
            self.commit_and_push_if_needed()
        except Exception as e:
            print(f"[watcher] git commit/push failed: {e}", file=sys.stderr)


class DebouncedSyncHandler(FileSystemEventHandler):
    """
    Handles file system events and triggers debounced syncs.
    """
    def __init__(self, backup_manager: BackupManager, debounce_seconds: float):
        self.backup_manager = backup_manager
        self.debounce_seconds = debounce_seconds

        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._last_event_ts = 0.0

    def on_any_event(self, event):
        # Ignore noise for .git if source happens to include it (unlikely, but safe)
        if event.src_path and "/.git" in str(event.src_path):
            return

        with self._lock:
            self._last_event_ts = time.time()
            if self._timer is not None:
                self._timer.cancel()

            self._timer = threading.Timer(self.debounce_seconds, self._flush_if_quiet)
            self._timer.daemon = True
            self._timer.start()

    def _flush_if_quiet(self):
        with self._lock:
            now = time.time()
            if now - self._last_event_ts < self.debounce_seconds:
                # Another event arrived; let the newer timer run.
                return

        # Do the work outside the lock
        self.backup_manager.sync_once()


def validate_paths(source: Path, target_repo: Path) -> None:
    """
    Validate the source and target paths.
    """
    if not source.exists() or not source.is_dir():
        raise SystemExit(f"Source path is not a directory: {source}")

    if not target_repo.exists() or not target_repo.is_dir():
        raise SystemExit(f"target_repo path is not a directory: {target_repo}")

    # Ensure it's a git repo
    try:
        Repo(str(target_repo))
    except InvalidGitRepositoryError as e:
        raise SystemExit(f"target_repo is not a git repository: {target_repo}") from e


def main():
    """
    Main entry point for the script.
    """
    parser = argparse.ArgumentParser(description="Rsync+git watcher")
    parser.add_argument("source", help="Source directory (can be in another user's home)")
    parser.add_argument("target_repo", help="Path to local git repo")
    parser.add_argument("--debounce", type=float, default=5.0, help="Debounce seconds (default: 5.0)")
    args = parser.parse_args()

    source = Path(args.source)
    target_repo = Path(args.target_repo)

    validate_paths(source, target_repo)

    backup_manager = BackupManager(source, target_repo)
    event_handler = DebouncedSyncHandler(backup_manager, debounce_seconds=args.debounce)

    # Initial sync on start
    print(f"[watcher] Initial sync: {source} -> {target_repo}")
    backup_manager.sync_once()

    observer = Observer()
    observer.schedule(event_handler, str(source), recursive=True)
    observer.start()

    print(f"[watcher] Watching: {source} (debounce={args.debounce}s)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[watcher] Stopping...")
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
