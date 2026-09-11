"""The full public CI sequence, shared by pre-push validation and Actions."""

import glob
import os
from pathlib import Path
import subprocess
import sys


def main():
    os.chdir(Path(__file__).resolve().parents[2])
    if sys.version_info[:2] != (3, 12) or sys.platform != "linux":
        raise SystemExit("Public CI requires Linux and Python 3.12.")
    os.environ.update(
        APP_SECRET_KEY="ci-only-secret-key-not-used-outside-tests",
        PEPPER="ci-only-password-pepper-not-used-outside-tests",
        OPENAI_KEY="ci-only-openai-key-not-used-outside-tests",
        DATABASE="Aurvek.db",
        ENVIRONMENT="test",
        SECURITY_REDIS_MODE="off",
        USE_EMAIL_SERVICE="false",
    )
    commands = [
        [sys.executable, "init_db.py"],
        [sys.executable, "-m", "pytest", "-v", "--tb=short"],
        [sys.executable, "tools/check_i18n.py"],
        ["node", "--test", *sorted(glob.glob("tests/js/*.test.js"))],
    ]
    for command in commands:
        print("Running: " + " ".join(command), flush=True)
        subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
