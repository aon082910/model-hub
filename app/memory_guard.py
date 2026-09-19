"""Container-memory awareness for long-running background jobs.

Several jobs in this app (batch AI tagging, library scanning, thumbnail
regeneration) process one file/model at a time in a loop that can run for a
long time on a large library. If something in that loop grows memory faster
than expected -- a pathological response from an external AI provider, a
native-library leak, anything we haven't anticipated -- the failure mode
without this module is the Linux OOM killer sending the whole container a
SIGKILL: no Python exception, no traceback, no chance to log why, and
everything else the container was doing (the web UI, the API) dies with it.

Reading the cgroup's own memory usage/limit lets a long-running job notice
it's approaching that ceiling and stop itself cleanly -- a partial batch and
a clear log line instead of a dead container.
"""
import logging

logger = logging.getLogger("modelhub.memory_guard")

DEFAULT_CGROUP_ROOT = "/sys/fs/cgroup"

# How close to the container's memory limit is "too close to keep going".
# Left with real headroom: cgroup usage figures include reclaimable page
# cache, and the goal is to back off well before the kernel's OOM killer
# would act, not to shave the ceiling as fine as possible.
DEFAULT_CRITICAL_RATIO = 0.85


def container_memory_usage(cgroup_root: str = DEFAULT_CGROUP_ROOT):
    """Return (used_bytes, limit_bytes) from the cgroup this process runs in.

    Returns (None, None) if unavailable -- not running under Linux cgroups at
    all, or (very commonly) no memory limit was configured for the container,
    in which case there's no ceiling to compare against and the guard should
    simply not fire.
    """
    try:
        with open(f"{cgroup_root}/memory.max") as f:
            raw_limit = f.read().strip()
        if raw_limit == "max":
            return None, None
        limit = int(raw_limit)
        with open(f"{cgroup_root}/memory.current") as f:
            used = int(f.read().strip())
        return used, limit
    except Exception:
        pass

    # cgroup v1 fallback.
    try:
        with open(f"{cgroup_root}/memory/memory.limit_in_bytes") as f:
            limit = int(f.read().strip())
        # An unset v1 limit reads back as a huge sentinel (close to the max
        # representable value) rather than a real byte count -- unlimited.
        if limit >= (1 << 60):
            return None, None
        with open(f"{cgroup_root}/memory/memory.usage_in_bytes") as f:
            used = int(f.read().strip())
        return used, limit
    except Exception:
        return None, None


def memory_usage_critical(cgroup_root: str = DEFAULT_CGROUP_ROOT, ratio: float = DEFAULT_CRITICAL_RATIO) -> bool:
    """True if this container has a memory limit and is close enough to it
    that a long-running job should stop itself rather than keep going."""
    used, limit = container_memory_usage(cgroup_root)
    if used is None or not limit:
        return False
    return used / limit >= ratio
