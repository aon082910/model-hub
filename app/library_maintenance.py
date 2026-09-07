import threading
from typing import Optional

_maintenance_lock = threading.Lock()
_state_lock = threading.Lock()
_current_task: Optional[str] = None


class LibraryMaintenanceBusy(RuntimeError):
    """Raised when another process-wide library maintenance task is active."""


def acquire_library_maintenance(task: str) -> bool:
    """Try to claim the single process-wide heavy library-maintenance slot."""
    global _current_task
    if not _maintenance_lock.acquire(blocking=False):
        return False
    with _state_lock:
        _current_task = task
    return True


def release_library_maintenance() -> None:
    """Release the process-wide maintenance slot owned by the current task."""
    global _current_task
    with _state_lock:
        _current_task = None
    _maintenance_lock.release()


def current_library_maintenance() -> Optional[str]:
    """Return the active maintenance task name, if any."""
    with _state_lock:
        return _current_task
