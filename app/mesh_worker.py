"""Memory-capped worker processes for everything that parses mesh files.

trimesh holds the whole parsed file in memory, at far more than the file's
own size: a 35MB Bambu Studio-style .3mf peaked at 4.1GB to load, a 262MB
binary STL at 2.9GB (measured, see app/thumbnails.py). Doing that inside the
web server process meant one large file anywhere in the library got the whole
app OOM-killed by the kernel -- no traceback, and because the background scan
starts again on every launch, a crash loop that also kept every file after
that one (in any folder) from ever being indexed.

Parsing happens here instead, in spawned worker processes with an RLIMIT_DATA
ceiling. An oversized file then just fails one allocation with an ordinary
Python exception inside the worker -- verified for both lxml's XML parser and
numpy -- and the scanner indexes it from a preview instead. Workers also set
oom_score_adj to 1000 so that if the host itself runs short, the kernel kills
a worker rather than the web server, and a worker that dies or hangs anyway is
replaced without taking anything else with it.

Top-level imports are deliberately light: a spawned worker imports this
module before its initializer runs, and the BLAS thread settings have to be
in place before numpy is first imported.
"""
import ctypes
import gc
import logging
import multiprocessing
import os
import resource
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

from app.memory_guard import container_memory_usage

logger = logging.getLogger("modelhub.mesh_worker")

MiB = 1024 * 1024
GiB = 1024 * MiB
MIN_BUDGET_BYTES = 1 * GiB
MAX_DEFAULT_BUDGET_BYTES = 8 * GiB
WORKER_TIMEOUT_SECONDS = int(os.environ.get("MESH_WORKER_TIMEOUT_SECONDS", "600"))
# Recycle long-lived workers periodically as a backstop against slow leaks in
# native libraries; each respawn costs ~1-2s of imports.
TASKS_PER_WORKER = 200


class MeshWorkerError(RuntimeError):
    """The worker process died or hung while handling a file."""


def _mem_total_bytes():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def mesh_memory_budget_bytes() -> int:
    """Memory ceiling for one mesh worker.

    MESH_WORKER_MEMORY_MB overrides. Otherwise half of whichever is smaller,
    the container's memory limit or the host's total RAM, clamped to 1-8 GB --
    leaving the other half for the web server, the database, and everything
    else on the box. Files that need more than this are indexed from a preview
    (embedded .3mf thumbnail, or a streamed render of a binary STL) instead of
    being fully parsed.
    """
    override = os.environ.get("MESH_WORKER_MEMORY_MB")
    if override:
        try:
            return max(256, int(override)) * MiB
        except ValueError:
            logger.warning("Ignoring invalid MESH_WORKER_MEMORY_MB=%r", override)

    candidates = [c for c in (container_memory_usage()[1], _mem_total_bytes()) if c]
    if not candidates:
        return 4 * GiB
    return int(min(max(min(candidates) // 2, MIN_BUDGET_BYTES), MAX_DEFAULT_BUDGET_BYTES))


def _init_worker(limit_bytes: int) -> None:
    for var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[var] = "1"
    try:
        resource.setrlimit(resource.RLIMIT_DATA, (limit_bytes, limit_bytes))
    except (ValueError, OSError):
        pass
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("1000")
    except OSError:
        pass


def _release_memory() -> None:
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def _portable(exc: Exception) -> Exception:
    # Results cross the process boundary by pickling, and not every exception
    # survives that -- numpy's allocation error needs constructor arguments it
    # doesn't pickle, which the pool would report as a dead worker instead.
    return RuntimeError(f"{type(exc).__name__}: {exc}")


def analyze_file(path_str: str, color: str, budget_bytes: int) -> dict:
    """Worker entry point: full stats + thumbnail for one scanned file."""
    from app.thumbnails import analyze_mesh

    try:
        return analyze_mesh(Path(path_str), color=color, budget_bytes=budget_bytes)
    except Exception as exc:
        raise _portable(exc) from None
    finally:
        _release_memory()


def preview_file(path_str: str, color: str) -> dict:
    """Worker entry point: the no-parse preview only, after a full analysis of
    the same file killed the worker."""
    from app.thumbnails import preview_without_parsing

    try:
        return preview_without_parsing(Path(path_str), color)
    except Exception as exc:
        raise _portable(exc) from None
    finally:
        _release_memory()


def render_thumbnail(path_str: str, color: str, budget_bytes: int):
    """Worker entry point: thumbnail only, for bulk regeneration."""
    from app import thumbnails

    path = Path(path_str)
    if not path.exists():
        raise FileNotFoundError(f"model file is missing on disk: {path}")
    try:
        return thumbnails.render_thumbnail_only(path, color, budget_bytes)
    except Exception as exc:
        raise _portable(exc) from None
    finally:
        _release_memory()


class MeshWorkerPool:
    """A small pool of memory-capped worker processes, replaced if one dies or hangs."""

    def __init__(self, workers: int = 1, budget_bytes: int = None):
        self.workers = max(1, workers)
        self.budget_bytes = budget_bytes or mesh_memory_budget_bytes()
        self._lock = threading.Lock()
        self._executor = None

    def executor(self) -> ProcessPoolExecutor:
        with self._lock:
            if self._executor is None:
                # Spawn, not fork: the server process has live threads (and
                # SQLite/logging locks) that a forked child would inherit mid-use.
                self._executor = ProcessPoolExecutor(
                    max_workers=self.workers,
                    mp_context=multiprocessing.get_context("spawn"),
                    initializer=_init_worker,
                    initargs=(self.budget_bytes,),
                    max_tasks_per_child=TASKS_PER_WORKER,
                )
            return self._executor

    def reset(self, executor: ProcessPoolExecutor = None) -> None:
        """Kill and discard the current workers; the next call starts fresh ones.

        Pass the executor that failed so a reset raced by another thread that
        already replaced it doesn't also throw away the new, healthy one.
        """
        with self._lock:
            current = self._executor
            if current is None or (executor is not None and executor is not current):
                return
            self._executor = None
        # ProcessPoolExecutor has no public way to kill a running task; a hung
        # worker would otherwise keep its memory until it finishes on its own.
        for process in list(getattr(current, "_processes", {}).values()):
            try:
                process.kill()
            except Exception:
                pass
        current.shutdown(wait=False, cancel_futures=True)

    def run(self, fn, *args, timeout: float = WORKER_TIMEOUT_SECONDS):
        executor = self.executor()
        try:
            return executor.submit(fn, *args).result(timeout=timeout)
        except BrokenProcessPool as exc:
            self.reset(executor)
            raise MeshWorkerError("mesh worker process died (most likely out of memory)") from exc
        except FutureTimeout as exc:
            self.reset(executor)
            raise MeshWorkerError(f"mesh worker did not finish within {timeout:.0f}s") from exc

    def shutdown(self) -> None:
        self.reset()


_scan_pool = None
_scan_pool_lock = threading.Lock()


def scan_worker_pool() -> MeshWorkerPool:
    """The single worker used by library scans and extension imports."""
    global _scan_pool
    with _scan_pool_lock:
        if _scan_pool is None:
            _scan_pool = MeshWorkerPool(workers=1)
            logger.info(
                "Mesh worker memory budget: %d MB (set MESH_WORKER_MEMORY_MB to change)",
                _scan_pool.budget_bytes // MiB,
            )
        return _scan_pool


def shutdown_worker_pools() -> None:
    global _scan_pool
    with _scan_pool_lock:
        pool, _scan_pool = _scan_pool, None
    if pool is not None:
        pool.shutdown()
