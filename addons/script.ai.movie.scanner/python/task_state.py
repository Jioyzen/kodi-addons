"""Serialize scanner, image-cache and reset work within this add-on."""
import fcntl
import os
import threading
from contextlib import contextmanager

_state = threading.local()


class TaskBusy(RuntimeError):
    pass


@contextmanager
def task_lock(profile):
    # Nested calls in auto-scan use the same lock; different invocations do not.
    if getattr(_state, 'depth', 0):
        _state.depth += 1
        try:
            yield
        finally:
            _state.depth -= 1
        return
    os.makedirs(profile, exist_ok=True)
    with open(os.path.join(profile, '.task.lock'), 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TaskBusy('Another scanner or cache task is running') from exc
        _state.depth = 1
        try:
            yield
        finally:
            _state.depth = 0
            fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def cache_task(profile):
    """Keep cache state and the shared lock until all workers have drained."""
    with task_lock(profile):
        marker = os.path.join(profile, '.caching')
        with open(marker, 'w') as state:
            state.write('1')
        try:
            yield
        finally:
            for name in ('.caching', '.stop_requested'):
                try:
                    os.remove(os.path.join(profile, name))
                except FileNotFoundError:
                    pass
