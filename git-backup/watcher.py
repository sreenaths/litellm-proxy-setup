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
import getpass
import grp
import os
import pwd

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from git import Repo, InvalidGitRepositoryError, GitCommandError, Actor


FILE_TAG_MAP = {
    "workspace/SOUL.md": "soul",
    "workspace/MEMORY.md": "mem", # High-level memory (not raw logs)
    "workspace/IDENTITY.md": "idty",
    "workspace/TOOLS.md": "tools",
    "workspace/USER.md": "usr",
    "workspace/HEARTBEAT.md": "hbt",
    "workspace/BOOTSTRAP.md": "boot",
    "workspace/AGENTS.md": "agts",
    "workspace/projects/": "proj",
    "workspace/memory/": "mem-log", # Raw memory logs (e.g. from soul)

    "openclaw.json": "conf",
    "agents/main/agent/models.json": "conf",
    "cron/": "cron",
}

class BackupManager:
    """
    Manages the backup process: rsync + git commit/push.
    """

    def __init__(self, source: Path, target_repo: Path):
        self.source = source
        self.target_repo = target_repo

    def get_invoking_user_group(self) -> tuple[str, str]:
        # Prefer numeric IDs (more reliable)
        sudo_uid = os.environ.get("SUDO_UID")
        sudo_gid = os.environ.get("SUDO_GID")
        if sudo_uid:
            uid = int(sudo_uid)
            gid = int(sudo_gid) if sudo_gid else os.getgid()
            user = pwd.getpwuid(uid).pw_name
            group = grp.getgrgid(gid).gr_name
        else:
            # Not run under sudo — fall back to normal lookup
            user = getpass.getuser()
            group = grp.getgrgid(os.getgid()).gr_name
        return user, group

    def run_rsync(self) -> None:
        """
        Rsync source -> target_repo, mirroring contents.
        Copies the *contents* of source into the repo root.
        """
        # Trailing slash means "copy contents of dir"
        src = str(self.source.resolve()) + "/"
        dst = str(self.target_repo.resolve()) + "/"

        user, group = self.get_invoking_user_group()
        exclude_file = Path(os.getcwd()) / ".rsyncignore"

        cmd = [
            "rsync",
            "-a",
            "--chown", f"{user}:{group}", # set ownership to current user/group (important on running with sudo)
            "--chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r",
            "--delete",      # delete files in target that don't exist in source
            "--exclude-from", str(exclude_file), # exclude patterns from .rsyncignore
            src,
            dst,
        ]

        # rsync returns 0 on success, non-zero on error
        subprocess.run(cmd, check=True)

    def get_stage_details(self, repo: Repo) -> tuple[int, list[str]]:
        """
        Count changed files after rsync.
        Uses `git status --porcelain` which yields one line per changed path.
        """
        changed = repo.git.status("--porcelain").splitlines()
        # Each line corresponds to a path. That’s a good "file count" for the message.
        files = [line for line in changed if line.strip()]

        tags = set()
        for line in files:
            path = line[3:]  # Skip the status chars
            for prefix, tag in FILE_TAG_MAP.items():
                if path.startswith(prefix):
                    tags.add(tag)
                    break
            else:
                tags.add("misc") # Miscellaneous changes that don't fit known tags

        if not tags:
            tags.add("unknown")

        tags_list = list(sorted(tags))
        if "misc" in tags_list:
            # Move misc to the end
            tags_list.remove("misc")
            tags_list.append("misc")

        return len(files), tags_list


    def commit_and_push_if_needed(self) -> None:
        """
        Check for changes, commit if needed, and push.
        """

        repo = Repo(str(self.target_repo))

        # Stage everything (including deletions)
        repo.git.add(A=True)

        # Get details, and skip if no changes
        file_count, tags = self.get_stage_details(repo)
        if file_count == 0:
            return

        # Commit changes
        msg = f"Updated {file_count} files - {', '.join(tags)}"
        author = Actor("git-backup-watcher", "git-backup-watcher@noreply.local")
        repo.index.commit(msg, author=author, committer=author)

        # Push (assumes a configured remote, typically "origin", and current branch set up)
        try:
            # Prefer pushing active branch to its upstream
            repo.git.push()
            print(f"[watcher] Commit successful - {msg}")
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
    parser.add_argument("--debounce", type=float, default=30.0, help="Debounce seconds (default: 30.0)")
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
