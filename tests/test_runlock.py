"""RunLock (src/pipeline/runlock.py) — cross-process flock semantics.
The contention tests spawn a real child interpreter that holds the lock,
because flock is per-open-file-description: two handles in ONE process can
both take it, so an in-process test would prove nothing."""
import os
import subprocess
import sys
import time

import pytest

from src.pipeline.runlock import RunLock, LockHeld

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Child holds the lock, prints its pid, then sleeps until stdin closes (or 30s).
_HOLDER = '''
import sys, os
sys.path.insert(0, %r)
from src.pipeline.runlock import RunLock
lock = RunLock(sys.argv[1])
assert lock.acquire(), "child could not acquire"
print(os.getpid(), flush=True)
sys.stdin.readline()
lock.release()
'''


def _spawn_holder(path):
    proc = subprocess.Popen(
        [sys.executable, '-c', _HOLDER % REPO, str(path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    pid_line = proc.stdout.readline().strip()  # blocks until the child holds it
    assert pid_line, 'child never reported'
    return proc, int(pid_line)


def _end_holder(proc):
    try:
        proc.stdin.write('\n')
        proc.stdin.flush()
    except (OSError, ValueError):
        pass
    proc.wait(timeout=10)


# ── single process ──────────────────────────────────────────────────────────

def test_acquire_writes_pid_and_release(tmp_path):
    lock = RunLock(tmp_path / 'run.lock')
    assert lock.acquire() is True
    assert lock.holder_pid() == os.getpid()
    lock.release()
    lock.release()  # idempotent


def test_creates_parent_dir(tmp_path):
    path = tmp_path / 'a' / 'b' / 'run.lock'
    lock = RunLock(path)
    assert path.parent.is_dir()
    assert lock.acquire() is True
    lock.release()


def test_context_manager(tmp_path):
    path = tmp_path / 'run.lock'
    with RunLock(path) as lock:
        assert lock.holder_pid() == os.getpid()
    # Released on exit: a second take in the same process succeeds.
    with RunLock(path):
        pass


def test_holder_pid_missing_or_garbage(tmp_path):
    path = tmp_path / 'run.lock'
    assert RunLock(path).holder_pid() is None  # file does not exist yet
    path.write_text('not a pid')
    assert RunLock(path).holder_pid() is None


def test_reacquire_same_object_is_true(tmp_path):
    lock = RunLock(tmp_path / 'run.lock')
    assert lock.acquire() is True
    assert lock.acquire() is True
    lock.release()


# ── cross-process contention ────────────────────────────────────────────────

def test_other_process_blocks_acquire(tmp_path):
    path = tmp_path / 'run.lock'
    proc, child_pid = _spawn_holder(path)
    try:
        lock = RunLock(path)
        assert lock.acquire() is False
        assert lock.holder_pid() == child_pid
        # Losing must not have clobbered the holder's pid in the file.
        assert path.read_text().strip() == str(child_pid)
    finally:
        _end_holder(proc)

    # Kernel dropped the flock with the child: parent can take it now.
    lock = RunLock(path)
    assert lock.acquire() is True
    assert lock.holder_pid() == os.getpid()
    lock.release()


def test_context_manager_raises_lockheld_with_pid(tmp_path):
    path = tmp_path / 'run.lock'
    proc, child_pid = _spawn_holder(path)
    try:
        with pytest.raises(LockHeld) as info:
            with RunLock(path):
                pass
        assert info.value.pid == child_pid
        assert str(child_pid) in str(info.value)
        assert isinstance(info.value, RuntimeError)
    finally:
        _end_holder(proc)


def test_lock_survives_holder_crash(tmp_path):
    """A killed holder must not leave a stuck lock (the reason flock beats a pid file)."""
    path = tmp_path / 'run.lock'
    proc, child_pid = _spawn_holder(path)
    proc.kill()
    proc.wait(timeout=10)
    deadline = time.time() + 5
    lock = RunLock(path)
    while not lock.acquire() and time.time() < deadline:
        time.sleep(0.05)
    assert lock.holder_pid() == os.getpid()
    lock.release()
