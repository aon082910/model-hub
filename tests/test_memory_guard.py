"""Unit coverage for app.memory_guard's cgroup-usage parsing.

No real /sys/fs/cgroup dependency here -- each test builds a fake cgroup
directory under tmp_path and points cgroup_root at it, so this runs the same
whether or not the test process itself happens to be inside a container.
"""
from app import memory_guard


def _write(path, name, content):
    (path / name).write_text(content)


def test_no_limit_configured_returns_none(tmp_path):
    _write(tmp_path, "memory.max", "max\n")
    _write(tmp_path, "memory.current", "12345\n")

    used, limit = memory_guard.container_memory_usage(str(tmp_path))

    assert used is None
    assert limit is None
    assert memory_guard.memory_usage_critical(str(tmp_path)) is False


def test_cgroup_v2_usage_and_limit_parsed(tmp_path):
    _write(tmp_path, "memory.max", "209715200\n")   # 200MiB
    _write(tmp_path, "memory.current", "104857600\n")  # 100MiB, exactly half

    used, limit = memory_guard.container_memory_usage(str(tmp_path))

    assert used == 104857600
    assert limit == 209715200


def test_critical_ratio_threshold(tmp_path):
    _write(tmp_path, "memory.max", "1000\n")
    _write(tmp_path, "memory.current", "849\n")  # just under 0.85
    assert memory_guard.memory_usage_critical(str(tmp_path)) is False

    _write(tmp_path, "memory.current", "851\n")  # just over 0.85
    assert memory_guard.memory_usage_critical(str(tmp_path)) is True


def test_cgroup_v1_fallback(tmp_path):
    v1_dir = tmp_path / "memory"
    v1_dir.mkdir()
    _write(v1_dir, "memory.limit_in_bytes", "1000\n")
    _write(v1_dir, "memory.usage_in_bytes", "900\n")
    # No cgroup v2 files present at tmp_path itself -- forces the fallback.

    used, limit = memory_guard.container_memory_usage(str(tmp_path))

    assert used == 900
    assert limit == 1000
    assert memory_guard.memory_usage_critical(str(tmp_path)) is True


def test_cgroup_v1_unset_sentinel_treated_as_unlimited(tmp_path):
    v1_dir = tmp_path / "memory"
    v1_dir.mkdir()
    _write(v1_dir, "memory.limit_in_bytes", str(9223372036854771712) + "\n")
    _write(v1_dir, "memory.usage_in_bytes", "900\n")

    used, limit = memory_guard.container_memory_usage(str(tmp_path))

    assert used is None
    assert limit is None


def test_missing_cgroup_files_returns_none(tmp_path):
    empty = tmp_path / "does-not-exist"

    used, limit = memory_guard.container_memory_usage(str(empty))

    assert used is None
    assert limit is None
    assert memory_guard.memory_usage_critical(str(empty)) is False
