"""Cross-platform single-instance lock for the bridge.

Guarantees only ONE bridge process forwards messages at a time, no matter how it
was started — boot Scheduled Task (Windows session 0), login shortcut, manual
run, or Docker. The previous dedup relied on a launcher-side process scan, which
*cannot read* a process started in another Windows session, so a second bridge
slipped through and every MAX message was delivered to Telegram twice.

This takes an OS-level lock on a file and holds it for the whole process
lifetime. The OS frees the lock automatically when the process exits — even on a
crash or kill — so a stale lock never wedges a future restart. Fail-open: if the
lock file itself can't be opened, the bridge still runs (availability beats a
theoretical duplicate).
"""
import logging
import os
from pathlib import Path

_logger = logging.getLogger(__name__)

# The locked file handle MUST stay referenced for the process lifetime: closing
# it (or letting it be garbage-collected) releases the OS lock.
_lock_handle = None


def acquire_single_instance(lock_path: Path) -> bool:
    """Try to take the single-instance lock.

    Returns True if this process now holds it (it is the only instance), or
    False if another live instance already holds it (caller should exit).
    """
    global _lock_handle
    try:
        handle = open(lock_path, "a+")  # noqa: SIM115 - kept open for lifetime
    except OSError as exc:
        _logger.warning("Single-instance lock file unavailable (%s); continuing.",
                        exc)
        return True  # fail-open: better to run than to refuse to start
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False  # another instance holds the lock
    _lock_handle = handle
    return True


def release_single_instance() -> None:
    """Release the lock (closes the handle). Mainly for tests/cleanup; in normal
    runs the OS releases it on process exit."""
    global _lock_handle
    if _lock_handle is not None:
        try:
            _lock_handle.close()
        except OSError:
            pass
        _lock_handle = None
