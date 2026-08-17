from __future__ import annotations

import asyncio
import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_wizard_is_disabled_by_default_even_if_claude_is_on_path(monkeypatch):
    from marketplace.landing import sandbox
    from marketplace.landing import wizard

    monkeypatch.delenv("LANDING_WIZARD_ENABLED", raising=False)
    monkeypatch.setenv("PATH", "/tmp/fake-claude-bin")

    status = sandbox.get_wizard_sandbox_status()
    assert status.enabled is False
    assert status.available is False
    assert wizard.is_claude_available()[0] is False


def test_enabling_without_a_pinned_verified_runner_still_fails_closed(monkeypatch):
    from marketplace.landing.sandbox import get_wizard_sandbox_status

    monkeypatch.setenv("LANDING_WIZARD_ENABLED", "true")
    monkeypatch.delenv("LANDING_WIZARD_SANDBOX_RUNNER", raising=False)
    monkeypatch.delenv("LANDING_WIZARD_SANDBOX_RUNNER_SHA256", raising=False)

    status = get_wizard_sandbox_status()
    assert status.enabled is True
    assert status.available is False
    assert status.runner_path is None


def test_wizard_command_has_no_host_bypass_or_shell_tool():
    from marketplace.landing.wizard import _claude_command

    command = _claude_command(15)
    joined = " ".join(command)
    assert "bypassPermissions" not in joined
    assert "Bash" not in joined
    assert "acceptEdits" in command
    assert "Write,Read,Edit" in command


def test_start_job_refuses_to_create_worker_without_sandbox(monkeypatch, tmp_path):
    from marketplace.landing import jobs

    monkeypatch.delenv("LANDING_WIZARD_ENABLED", raising=False)
    before = set(jobs.JOBS_DIR.glob("worker_*.py"))
    result = jobs.start_job(
        prompt_id=1,
        job_type="generate",
        prompt_dir=str(tmp_path),
        params={"description": "test"},
    )
    after = set(jobs.JOBS_DIR.glob("worker_*.py"))

    assert result["success"] is False
    assert result["error_code"] == "WIZARD_SANDBOX_UNAVAILABLE"
    assert after == before


def test_sandbox_runner_gets_minimal_environment_and_workspace_only(
    monkeypatch,
    tmp_path,
):
    from marketplace.landing import sandbox

    captured = {}

    monkeypatch.setenv("APP_SECRET_KEY", "must-not-be-inherited")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-be-inherited")
    monkeypatch.setattr(
        sandbox,
        "get_wizard_sandbox_status",
        lambda: sandbox.WizardSandboxStatus(
            enabled=True,
            available=True,
            reason="verified",
            runner_path="/opt/aurvek/bin/wizard-sandbox",
        ),
    )

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    result = sandbox.run_claude_in_sandbox(
        ["claude", "--allowedTools", "Write,Read,Edit"],
        prompt="create a page",
        workspace=tmp_path,
        timeout=30,
    )

    assert result.returncode == 0
    assert captured["command"][:5] == [
        "/opt/aurvek/bin/wizard-sandbox",
        "--workspace",
        str(tmp_path.resolve()),
        "--timeout-seconds",
        "30",
    ]
    assert captured["command"][5:] == [
        "--",
        "claude",
        "--allowedTools",
        "Write,Read,Edit",
    ]
    assert captured["cwd"] == str(tmp_path.resolve())
    expected_env = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC"}
    if os.name == "nt" and os.getenv("SystemRoot"):
        expected_env["SystemRoot"] = os.environ["SystemRoot"]
    assert captured["env"] == expected_env
    assert "APP_SECRET_KEY" not in captured["env"]
    assert "ANTHROPIC_API_KEY" not in captured["env"]
    assert captured["close_fds"] is True


def test_sandbox_rejects_symlinks_before_invocation(monkeypatch, tmp_path):
    from marketplace.landing import sandbox

    target = tmp_path / "outside.txt"
    target.write_text("secret", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        (workspace / "escape").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")

    monkeypatch.setattr(
        sandbox,
        "get_wizard_sandbox_status",
        lambda: sandbox.WizardSandboxStatus(
            enabled=True,
            available=True,
            reason="verified",
            runner_path="/opt/aurvek/bin/wizard-sandbox",
        ),
    )

    with pytest.raises(sandbox.WizardSandboxViolation):
        sandbox.run_claude_in_sandbox(
            ["claude"],
            prompt="test",
            workspace=workspace,
            timeout=30,
        )


@pytest.mark.asyncio
async def test_prompt_wizard_security_guard_is_fail_closed(monkeypatch):
    from marketplace.routes import prompt_landing_builder

    async def unavailable(text):
        return {
            "checked": False,
            "allowed": True,
            "reason": "not configured",
            "threat_level": "none",
            "threats": [],
        }

    monkeypatch.setattr(prompt_landing_builder, "check_security", unavailable)
    response = await prompt_landing_builder._run_security_check(
        "normal request",
        prompt_id=1,
        label="landing wizard",
    )
    assert response is not None
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_publication_gate_fails_closed_without_private_pipeline(
    monkeypatch,
    tmp_path,
):
    from marketplace.routes import prompt_landing_builder

    monkeypatch.setattr(
        prompt_landing_builder,
        "_PROMPT_PIPELINE_DIR",
        tmp_path / "missing",
    )

    errors = await prompt_landing_builder._get_landing_publication_errors(
        1,
        "<main>Safe landing</main>",
    )

    assert errors == [
        prompt_landing_builder.PUBLICATION_VERIFICATION_UNAVAILABLE
    ]


@pytest.mark.asyncio
async def test_publication_gate_preserves_private_verifier_result(
    monkeypatch,
    tmp_path,
):
    from marketplace.routes import prompt_landing_builder

    truth = {"id": 7}

    def fake_import(name):
        if name.endswith("truth_sheet"):
            return SimpleNamespace(load_truth_sheet=lambda prompt_id: truth)
        return SimpleNamespace(
            check_publication_readiness=lambda content, loaded_truth: [
                f"blocked:{content}:{loaded_truth['id']}"
            ]
        )

    monkeypatch.setattr(prompt_landing_builder, "_PROMPT_PIPELINE_DIR", tmp_path)
    monkeypatch.setattr(prompt_landing_builder, "import_module", fake_import)

    errors = await prompt_landing_builder._get_landing_publication_errors(
        7,
        "landing",
    )

    assert errors == ["blocked:landing:7"]


@pytest.mark.asyncio
@pytest.mark.parametrize("truth_result", [None, RuntimeError("database unavailable")])
async def test_publication_gate_blocks_missing_or_failed_truth(
    monkeypatch,
    tmp_path,
    truth_result,
):
    from marketplace.routes import prompt_landing_builder

    def load_truth_sheet(prompt_id):
        if isinstance(truth_result, Exception):
            raise truth_result
        return truth_result

    def fake_import(name):
        if name.endswith("truth_sheet"):
            return SimpleNamespace(load_truth_sheet=load_truth_sheet)
        return SimpleNamespace(check_publication_readiness=lambda content, truth: [])

    monkeypatch.setattr(prompt_landing_builder, "_PROMPT_PIPELINE_DIR", tmp_path)
    monkeypatch.setattr(prompt_landing_builder, "import_module", fake_import)

    errors = await prompt_landing_builder._get_landing_publication_errors(
        1,
        "landing",
    )

    assert errors == [
        prompt_landing_builder.PUBLICATION_VERIFICATION_UNAVAILABLE
    ]


@pytest.mark.asyncio
async def test_revoke_landing_publication_clears_flags_and_cache(monkeypatch):
    from marketplace.routes import prompt_landing_builder

    executed = []
    committed = False

    class Cursor:
        async def fetchone(self):
            return ("public-17",)

    class Connection:
        async def execute(self, sql, params):
            executed.append((" ".join(sql.split()), params))
            return Cursor()

        async def commit(self):
            nonlocal committed
            committed = True

    @asynccontextmanager
    async def fake_connection():
        yield Connection()

    invalidated = []
    monkeypatch.setattr(prompt_landing_builder, "get_db_connection", fake_connection)
    monkeypatch.setattr(
        prompt_landing_builder,
        "invalidate_landing_cache",
        invalidated.append,
    )

    await prompt_landing_builder._revoke_landing_publication(17)

    assert executed[0] == (
        "UPDATE PROMPTS SET has_landing_page = 0, landing_trusted = 0 WHERE id = ?",
        (17,),
    )
    assert committed is True
    assert invalidated == ["public-17"]


@pytest.mark.asyncio
async def test_persist_home_revokes_before_write_and_verification(
    monkeypatch,
    tmp_path,
):
    from marketplace.routes import prompt_landing_builder

    home_path = tmp_path / "home.html"
    events = []

    async def revoke(prompt_id):
        events.append("revoke")
        assert prompt_id == 23
        assert not home_path.exists()

    async def verify(prompt_id, content):
        events.append("verify")
        assert prompt_id == 23
        assert home_path.read_text(encoding="utf-8") == content
        return ["blocked"]

    class Connection:
        async def execute(self, sql, params):
            events.append("section-config")
            return None

        async def commit(self):
            events.append("commit")

    @asynccontextmanager
    async def fake_connection(*args, **kwargs):
        yield Connection()

    monkeypatch.setattr(prompt_landing_builder, "PRIMARY_APP_DOMAIN", "")
    monkeypatch.setattr(
        prompt_landing_builder,
        "create_prompt_directory",
        lambda *args: str(tmp_path),
    )
    monkeypatch.setattr(prompt_landing_builder, "get_db_connection", fake_connection)
    monkeypatch.setattr(prompt_landing_builder, "_revoke_landing_publication", revoke)
    monkeypatch.setattr(
        prompt_landing_builder,
        "_get_landing_publication_errors",
        verify,
    )

    response = await prompt_landing_builder._persist_landing_page(
        prompt_id=23,
        section="home",
        content="<main>new content</main>",
        use_default_template=False,
        prompt_info={"created_by_username": "creator", "name": "Prompt"},
        is_admin=True,
    )

    assert response.status_code == 200
    assert events == ["revoke", "section-config", "commit", "verify"]
    assert home_path.read_text(encoding="utf-8") == "<main>new content</main>"


@pytest.mark.asyncio
async def test_landing_save_lock_serializes_same_prompt(monkeypatch, tmp_path):
    from marketplace.routes import prompt_landing_builder

    monkeypatch.setattr(prompt_landing_builder, "_LANDING_SAVE_LOCK_DIR", tmp_path)
    monkeypatch.setattr(
        prompt_landing_builder,
        "_LANDING_SAVE_LOCK_TIMEOUT_SECONDS",
        2,
    )

    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    second_entered = asyncio.Event()

    async def first_save():
        async with prompt_landing_builder._landing_save_lock(31):
            first_entered.set()
            await release_first.wait()

    async def second_save():
        second_started.set()
        async with prompt_landing_builder._landing_save_lock(31):
            second_entered.set()

    first_task = asyncio.create_task(first_save())
    await first_entered.wait()
    second_task = asyncio.create_task(second_save())
    await second_started.wait()

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(second_entered.wait(), timeout=0.1)

    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert second_entered.is_set()


def test_no_bypass_permissions_remains_in_wizard_sources():
    root = Path(__file__).resolve().parents[1]
    source = (root / "marketplace" / "landing" / "wizard.py").read_text(encoding="utf-8")
    assert "bypassPermissions" not in source
