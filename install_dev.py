#!/usr/bin/env python3
"""Set up a clone of this repo for development: a .venv with the tools in
requirements-dev.txt, and git pointed at the hooks in .githooks. Links no
skills or tools into the home directory; install.py does that.

The hooks run .venv's Python when it exists, so commits use the venv's ruff
and pytest without activating it. To use them by hand: source .venv/bin/activate

    python3 install_dev.py
"""

import subprocess
import sys

from install import REPO, enable_git_hooks, say

VENV = REPO / ".venv"


def has_pip() -> bool:
    python = VENV / "bin" / "python3"
    check = [str(python), "-m", "pip", "--version"]
    return (
        python.exists()
        and subprocess.run(check, capture_output=True, check=False).returncode == 0
    )


def main() -> int:
    print("[llm-toolbox dev]")
    if not has_pip():
        # A venv made without pip can't install anything; start it over.
        make = [sys.executable, "-m", "venv", "--clear", str(VENV)]
        if subprocess.run(make, check=False).returncode != 0:
            # Debian and Ubuntu ship venv's pip bootstrap separately.
            say(
                "FAILED to create .venv with pip -- on Debian/Ubuntu: sudo apt install python3-venv"
            )
            return 1
        say(f"created {VENV}")
    pip = [
        str(VENV / "bin" / "python3"),
        "-m",
        "pip",
        "install",
        "-q",
        "-r",
        str(REPO / "requirements-dev.txt"),
    ]
    if subprocess.run(pip, check=False).returncode != 0:
        say("FAILED to install requirements-dev.txt")
        return 1
    say("installed requirements-dev.txt into .venv")
    return enable_git_hooks()


if __name__ == "__main__":
    sys.exit(main())
