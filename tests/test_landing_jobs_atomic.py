from __future__ import annotations

import threading
import os
from types import SimpleNamespace

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


@pytest.mark.parametrize("failure", ["create", "serialize", "spawn"])
def test_start_failure_does_not_leave_a_blocking_pending_job(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(jobs, "get_wizard_sandbox_status", lambda: SimpleNamespace(available=True))
    params = {"description": "test"}
    if failure == "create":
        real_create = jobs.create_job

        def create_then_fail(**kwargs):
            real_create(**kwargs)
            raise OSError("preparation failed")

        monkeypatch.setattr(jobs, "create_job", create_then_fail)
    elif failure == "serialize":
        params["description"] = object()
    else:
        monkeypatch.setattr(jobs.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("spawn failed")))

    result = jobs.start_job(17, "generate", str(tmp_path), params)
    assert result["success"] is False
    record = jobs.get_job(result["task_id"])
    if record:
        assert record["status"] == "failed"
        assert record["completed_at"]
    assert jobs.get_active_job_for_prompt(17) is None
    assert not list(tmp_path.glob("worker_*.py"))


@pytest.mark.parametrize("pack_id,target,active_lookup", [
    (None, "landing", jobs.get_active_job_for_prompt),
    (20, "landing", jobs.get_active_job_for_pack),
    (None, "welcome", jobs.get_active_welcome_job_for_prompt),
    (20, "welcome", jobs.get_active_welcome_job_for_pack),
])
def test_pending_expiry_is_consistent_for_read_and_active_lookup(
    tmp_path, monkeypatch, pack_id, target, active_lookup,
):
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    job = jobs.create_job("orphan", 17 if pack_id is None else 0, "generate", pack_id=pack_id, target=target)
    job["updated_at"] = "2000-01-01T00:00:00+00:00"
    jobs._write_job_atomic(jobs.get_job_path("orphan"), job)
    assert active_lookup(pack_id or 17) is None
    assert jobs.get_job("orphan")["status"] == "timeout"


def test_cleanup_is_bounded_and_keeps_active_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    for task_id, status in [("pending", "pending"), ("running", "running"), ("done", "completed")]:
        jobs.create_job(task_id, 17, "generate")
        jobs.update_job(task_id, status=status)
        os.utime(jobs.get_job_path(task_id), (0, 0))
    assert jobs.cleanup_old_jobs() == 1
    assert jobs.get_job_path("pending").exists()
    assert jobs.get_job_path("running").exists()

    for index in range(105):
        task_id = f"old-{index}"
        jobs.create_job(task_id, 17, "generate")
        jobs.update_job(task_id, status="failed")
        os.utime(jobs.get_job_path(task_id), (0, 0))
    deleted = jobs.cleanup_old_jobs()
    assert 0 < deleted <= 100
    assert len(list(tmp_path.glob("old-*.json"))) == 105 - deleted


def test_start_uses_real_worker_process_and_records_unavailable_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path / "jobs")
    jobs.JOBS_DIR.mkdir()
    monkeypatch.setattr(jobs, "get_wizard_sandbox_status", lambda: SimpleNamespace(available=True))
    # Only the parent readiness is simulated; the real child keeps the feature
    # disabled and must persist the sandbox failure without invoking a provider.
    monkeypatch.setenv("LANDING_WIZARD_ENABLED", "false")
    real_popen = jobs.subprocess.Popen
    children = []

    def launch(command, **kwargs):
        child = real_popen(command, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(jobs.subprocess, "Popen", launch)
    result = jobs.start_job(17, "generate", str(tmp_path), {"description": "test"})
    assert result["success"] is True
    assert children[0].wait(timeout=30) == 1
    job = jobs.get_job(result["task_id"])
    assert job["status"] == "failed"
    assert job["pid"] == children[0].pid
    assert job["completed_at"]
    assert not list(jobs.JOBS_DIR.glob("worker_*.py"))
