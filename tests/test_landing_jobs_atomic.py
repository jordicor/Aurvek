from __future__ import annotations

import threading

import pytest

from marketplace.landing import jobs


def test_job_update_never_exposes_partial_json(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    task_id = jobs.generate_task_id()
    jobs.create_job(task_id, 17, "generate")

    real_dump = jobs.json.dump
    partial_written = threading.Event()
    finish_write = threading.Event()

    def slow_dump(value, stream, *, indent=None):
        stream.write('{"partial":')
        stream.flush()
        partial_written.set()
        assert finish_write.wait(timeout=5)
        stream.seek(0)
        stream.truncate()
        return real_dump(value, stream, indent=indent)

    monkeypatch.setattr(jobs.json, "dump", slow_dump)
    result = {}

    writer = threading.Thread(
        target=lambda: result.setdefault(
            "job", jobs.update_job(task_id, status="running")
        )
    )
    writer.start()
    assert partial_written.wait(timeout=5)
    try:
        visible = jobs.get_job(task_id)
        assert visible is not None
        assert visible["status"] == "pending"
    finally:
        finish_write.set()
        writer.join(timeout=5)

    assert not writer.is_alive()
    assert result["job"]["status"] == "running"
    assert jobs.get_job(task_id)["status"] == "running"
    assert list(tmp_path.glob("*.tmp")) == []


def test_all_job_status_writes_use_replace(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    real_replace = jobs.os.replace
    replacements = []

    def record_replace(source, destination):
        replacements.append((source, destination))
        return real_replace(source, destination)

    monkeypatch.setattr(jobs.os, "replace", record_replace)
    task_id = jobs.generate_task_id()
    jobs.create_job(task_id, 18, "generate", timeout_seconds=0)
    running = jobs.update_job(
        task_id,
        status="running",
        pid=None,
    )
    running["updated_at"] = "2000-01-01T00:00:00+00:00"
    timed_out = jobs._check_job_timeout(running)

    assert timed_out["status"] == "timeout"
    assert len(replacements) == 3
    assert all(destination == jobs.get_job_path(task_id) for _, destination in replacements)
    assert all(source.parent == tmp_path for source, _ in replacements)


def test_atomic_job_write_cleans_temporary_file_on_replace_error(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(
        jobs.os,
        "replace",
        lambda source, destination: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        jobs.create_job(jobs.generate_task_id(), 19, "generate")
    assert list(tmp_path.iterdir()) == []
