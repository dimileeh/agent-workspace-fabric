#!/usr/bin/env python3
"""Executable validation entrypoint for monitor-origin protected-scope resume.

AWF executes this path directly as a validation command. Delegate to the focused
runtime regression module that owns the behavior under test.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[3]
    completed = subprocess.run(
        [
            "uv",
            "run",
            "--python",
            "3.12",
            "--extra",
            "dev",
            "pytest",
            "tests/unit/runtime/test_pr_monitor_operator_hints_part_008.py",
            "-q",
        ],
        cwd=repo_root,
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
