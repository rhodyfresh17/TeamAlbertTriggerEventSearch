"""Single-instance run lock for the Mac-side enrichment job (M1).

WHY (A.J. 2026-09-07, Phase 2 plan): enrichment_scout.py is launched by
launchd on a schedule AND by hand from the terminal. Two overlapping runs
double-spend the Tavily budget and race on the same SQLite/Supabase rows.
`fcntl.flock` on a small lock file is the simplest cross-process guard:
the kernel releases it automatically if the holder crashes, so there is no
stale-lock cleanup to get wrong. The holder's pid is written into the file
purely for humans ("who has it?") — the flock is the actual lock.

Usage:
    with RunLock('.locks/enrichment.lock'):
        ...            # raises LockHeld(pid=...) if another run is active
"""
from __future__ import annotations

import fcntl
import os
from typing import IO, Optional


class LockHeld(RuntimeError):
    """Raised by `with RunLock(...)` when another process already holds the lock."""

    def __init__(self, path: str, pid: Optional[int] = None):
        self.path = path
        self.pid = pid
        who = f'pid {pid}' if pid else 'another process'
        super().__init__(f'run lock {path} is held by {who}')


class RunLock:
    def __init__(self, path):
        self.path = str(path)
        self._fh: Optional[IO[str]] = None
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def acquire(self) -> bool:
        """Non-blocking take. True on success (pid written); False if held elsewhere."""
        if self._fh is not None:
            return True  # re-entrant within the same object: already ours
        # 'a+' so opening never truncates a file another process is holding.
        fh = open(self.path, 'a+')
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return True

    def holder_pid(self) -> Optional[int]:
        """Pid written by whoever holds (or last held) the lock — best effort."""
        try:
            with open(self.path, 'r') as fh:
                raw = fh.read().strip()
            return int(raw) if raw else None
        except (OSError, ValueError):
            return None

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()

    def __enter__(self) -> 'RunLock':
        if not self.acquire():
            raise LockHeld(self.path, self.holder_pid())
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
