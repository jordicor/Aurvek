"""A broken fresh installation must fail the command, not look successful to CI."""

from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("schema", [None, "THIS IS NOT VALID SQL;"])
def test_initializer_returns_failure_for_missing_or_invalid_schema(tmp_path, schema):
    if schema is not None:
        (tmp_path / "aurvek_schema.sql").write_text(schema, encoding="utf-8")
    script = Path(__file__).resolve().parents[1] / "init_db.py"
    result = subprocess.run(
        [sys.executable, str(script)], cwd=tmp_path,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    assert "initialized successfully" not in result.stdout
    assert "Schema file not found:" in result.stdout if schema is None else "Database error:" in result.stdout
