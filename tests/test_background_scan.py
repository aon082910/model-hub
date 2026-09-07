import asyncio
import threading

import pytest


def test_run_scan_is_offloaded_from_event_loop(monkeypatch):
    """The background scanner must not execute blocking scan work on the event-loop thread."""
    import app.main as main

    event_loop_thread = threading.get_ident()
    scan_thread = None
    scan_started = threading.Event()
    release_scan = threading.Event()

    def fake_run_scan():
        nonlocal scan_thread
        scan_thread = threading.get_ident()
        scan_started.set()
        release_scan.wait(timeout=2)
        return {"found": 0, "added": 0, "updated": 0, "duplicates": 0}

    monkeypatch.setattr(main, "_run_scan", fake_run_scan)
    monkeypatch.setattr(main, "SCAN_INTERVAL_SECONDS", 999999)

    async def exercise():
        task = asyncio.create_task(main._background_scan_loop())
        try:
            # If _run_scan is executed directly on the event loop, this timeout
            # cannot complete while fake_run_scan is waiting.
            started = await asyncio.to_thread(scan_started.wait, 1)
            assert started is True
            assert scan_thread != event_loop_thread
        finally:
            release_scan.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(exercise())


def test_scan_commits_each_file_and_checkpoints_memory_in_batches(monkeypatch, tmp_path):
    """DB writes should be short per-file transactions while memory cleanup stays batched."""
    import app.scanner as scanner

    class FakeSession:
        def __init__(self):
            self.commits = 0
            self.expunges = 0

        def commit(self):
            self.commits += 1

        def expunge_all(self):
            self.expunges += 1

    library = tmp_path / "library"
    library.mkdir()
    for i in range(205):
        (library / f"model-{i}.stl").write_bytes(b"solid test\nendsolid test\n")

    session = FakeSession()

    monkeypatch.setattr(scanner, "LIBRARY_PATH", library)
    monkeypatch.setattr(scanner, "SUPPORTED_EXTENSIONS", {".stl"})
    monkeypatch.setattr(scanner, "_upsert_path", lambda session, path, rel_path, counters: None)
    monkeypatch.setattr(scanner, "_remove_missing_models", lambda session: None)
    monkeypatch.setattr(scanner.gc, "collect", lambda: 0)

    result = scanner.scan_library(session)

    assert result["found"] == 205
    # Each model is committed immediately so SQLite's writer lock is released
    # before the scanner begins expensive work on the next model.
    assert session.commits == 205
    # ORM/GC cleanup remains bounded at 100, 200, and the final partial batch.
    assert session.expunges == 3


def test_upsert_releases_lookup_transaction_before_expensive_processing(monkeypatch, tmp_path):
    """Hashing/mesh work must not run while the initial SQLite read transaction is open."""
    import app.scanner as scanner

    class EmptyResult:
        def first(self):
            return None

    class FakeSession:
        def __init__(self):
            self.rolled_back = False

        def exec(self, statement):
            return EmptyResult()

        def rollback(self):
            self.rolled_back = True

    path = tmp_path / "model.stl"
    path.write_bytes(b"solid test\nendsolid test\n")
    session = FakeSession()

    def fake_hash_file(file_path, chunk_size=1024 * 1024):
        assert session.rolled_back is True
        raise RuntimeError("stop after transaction-boundary assertion")

    monkeypatch.setattr(scanner, "hash_file", fake_hash_file)

    with pytest.raises(RuntimeError, match="transaction-boundary"):
        scanner._upsert_path(session, path, path.name, {})


def test_concurrent_scan_is_rejected(monkeypatch):
    """Only one library scan may run at a time within the process."""
    import app.scanner as scanner

    scan_started = threading.Event()
    release_scan = threading.Event()

    def fake_scan_library(session):
        scan_started.set()
        release_scan.wait(timeout=2)
        return {"found": 0, "added": 0, "updated": 0, "duplicates": 0}

    monkeypatch.setattr(scanner, "_scan_library", fake_scan_library)

    worker_error = []

    def first_scan():
        try:
            scanner.scan_library(object())
        except Exception as exc:
            worker_error.append(exc)

    worker = threading.Thread(target=first_scan)
    worker.start()
    assert scan_started.wait(timeout=1)
    assert scanner.scan_is_running() is True

    try:
        try:
            scanner.scan_library(object())
            assert False, "second scan should have been rejected"
        except scanner.ScanAlreadyRunning as exc:
            assert "already in progress" in str(exc)
    finally:
        release_scan.set()
        worker.join(timeout=2)

    assert worker_error == []
    assert scanner.scan_is_running() is False
