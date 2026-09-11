"""The operator stdout contract is JSON even when lazy services emit diagnostics."""
import io
import json
import logging
import sys
from contextlib import redirect_stderr, redirect_stdout

import pytest

from integrations.applications import interview_copy, prepare_interview


@pytest.mark.parametrize("command", ["prepare", "copy"])
def test_operator_cli_stdout_is_json_and_diagnostics_restore_logging(command, tmp_path, monkeypatch):
    request = {"operation_id": "operation-123", "app_id": "katari",
        "external_user_id": "a933b516-dd16-4026-a444-794109e41616",
        "external_project_id": "e933b516-dd16-4026-a444-794109e41616",
        "source_user_id": 58, "source_conversation_id": 1753,
        "brief": {"revision": 1, "language": "es"}}
    receipt = {"conversation_id": "2253", "subject": "opaque-subject"}
    async def noisy_operation(*args, **kwargs):
        logging.getLogger("app").warning("app diagnostic")
        logging.getLogger().warning("root diagnostic")
        print("late import diagnostic")
        return {"receipt": receipt, "manifest": {"state": "copied"}}
    if command == "prepare":
        module = prepare_interview
        monkeypatch.setattr(module, "prepare_interview", noisy_operation)
    else:
        module = interview_copy
        request.update(expected_subject="opaque-subject", expected_context_id="opaque-context")
        monkeypatch.setattr(module.ApplicationInterviewCopyService, "copy", noisy_operation)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["command", str(path), "--apply"] if command == "prepare"
                        else ["command", "copy", "--request", str(path)])
    out, err = io.StringIO(), io.StringIO()
    # Match the app's shared handler created before the CLI starts.
    handler = logging.StreamHandler(out)
    app, root = logging.getLogger("app"), logging.getLogger()
    monkeypatch.setattr(app, "handlers", [handler])
    monkeypatch.setattr(root, "handlers", [handler])
    with redirect_stdout(out), redirect_stderr(err):
        module.main()
    payload = json.loads(out.getvalue())
    assert payload == ({"receipt": receipt} if command == "prepare" else receipt)
    assert "app diagnostic" in err.getvalue() and "root diagnostic" in err.getvalue()
    assert "late import diagnostic" in err.getvalue()
    assert handler.stream is out  # CLI scope does not alter later web logging.
