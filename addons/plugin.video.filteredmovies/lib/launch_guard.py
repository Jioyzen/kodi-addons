"""Prevent repeated remote presses from opening duplicate modal walls."""
import os
from contextlib import contextmanager


@contextmanager
def single_window(profile):
    import fcntl
    os.makedirs(profile, exist_ok=True)
    with open(os.path.join(profile, 'wall-window.lock'), 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
