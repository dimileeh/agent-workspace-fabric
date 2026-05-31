"""Unit tests for PR monitor pre-commit autofix commit retries."""

from __future__ import annotations

import pytest

from awf.common.commands import CommandResult
from awf.runtime.pr_monitor_runner.commit_autofix import (
    _monitor_precommit_autofix_repair_paths,
)


@pytest.mark.unit
def test_monitor_precommit_autofix_skips_semantic_ruff_check_autofix_paths() -> None:
    output = (
        "ruff check..............................................................Failed\n"
        "- hook id: awf-ruff-check\n"
        "- exit code: 1\n"
        "- files were modified by this hook\n\n"
        "I001 [*] Import block is un-sorted or un-formatted\n"
        "   --> src/awf/mcp/server.py:13:1\n"
        "Found 1 error.\n"
        "[*] 1 fixable with the `--fix` option.\n"
    )
    commit_result = CommandResult(returncode=1, stdout=output, stderr="")

    assert _monitor_precommit_autofix_repair_paths(commit_result) == ()


@pytest.mark.unit
def test_monitor_precommit_autofix_keeps_deterministic_hook_repair_paths() -> None:
    output = (
        "fix end of files................................................Failed\n"
        "- hook id: end-of-file-fixer\n"
        "- exit code: 1\n"
        "- files were modified by this hook\n\n"
        "Fixing docs/awf-plans/ws_761.conformance.json\n"
    )
    commit_result = CommandResult(returncode=1, stdout="", stderr=output)

    assert _monitor_precommit_autofix_repair_paths(commit_result) == (
        "docs/awf-plans/ws_761.conformance.json",
    )
