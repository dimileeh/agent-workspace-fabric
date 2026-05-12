"""Unit tests for ``_classify_post_agent_commit_failure``.

The classifier reads a ``git commit`` ``CommandResult`` and returns a
structured classification that lets the executor distinguish:

  * generic ``git commit`` failures (missing identity, detached HEAD, ...)
  * pre-commit hook failures (``awf-ruff-check``, ``awf-mypy``, ...)
  * format-only pre-commit failures (``awf-ruff-format-check`` reporting
    ``Would reformat: ...``) — the deterministic-repair entrypoint.

These tests run against the helper directly so the contract is locked
in without touching the executor pipeline.
"""

from __future__ import annotations

import pytest

from awf.common.commands import CommandResult
from awf.control.executor import (
    POST_AGENT_COMMIT_FAILED_REASON_CODE,
    POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE,
    POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE,
    _classify_post_agent_commit_failure,
)


def _commit_result(*, stdout: str = "", stderr: str = "", returncode: int = 1) -> CommandResult:
    return CommandResult(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.mark.unit
def test_precommit_ruff_check_failure_uses_precommit_reason_code() -> None:
    stdout = (
        "trailing-whitespace.....................................................Passed\n"
        "ruff check..............................................................Failed\n"
        "- hook id: awf-ruff-check\n"
        "- exit code: 1\n"
        "\n"
        "error: Failed to parse src/awf/foo.py:1:1: SyntaxError\n"
    )
    classification = _classify_post_agent_commit_failure(_commit_result(stdout=stdout))

    assert classification.reason_code == POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE
    assert classification.failed_hooks == ("awf-ruff-check",)
    assert classification.format_repair_files == ()
    assert "awf-ruff-check" in classification.summary


@pytest.mark.unit
def test_precommit_format_only_failure_uses_format_reason_code_and_parses_paths() -> None:
    stdout = (
        "ruff check..............................................................Passed\n"
        "ruff format --check.....................................................Failed\n"
        "- hook id: awf-ruff-format-check\n"
        "- exit code: 1\n"
        "\n"
        "Would reformat: src/awf/control/executor.py\n"
        "Would reformat: src/awf/control/quality_gates.py\n"
        "2 files would be reformatted\n"
    )
    classification = _classify_post_agent_commit_failure(_commit_result(stdout=stdout))

    assert classification.reason_code == POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE
    assert classification.failed_hooks == ("awf-ruff-format-check",)
    assert classification.format_repair_files == (
        "src/awf/control/executor.py",
        "src/awf/control/quality_gates.py",
    )


@pytest.mark.unit
def test_generic_git_failure_uses_generic_reason_code() -> None:
    stderr = "fatal: empty ident name (for <>) not allowed\n"
    classification = _classify_post_agent_commit_failure(_commit_result(stderr=stderr))

    assert classification.reason_code == POST_AGENT_COMMIT_FAILED_REASON_CODE
    assert classification.failed_hooks == ()
    assert classification.format_repair_files == ()
    assert "fatal" in classification.summary.lower()


@pytest.mark.unit
def test_empty_output_uses_generic_reason_code() -> None:
    classification = _classify_post_agent_commit_failure(_commit_result())

    assert classification.reason_code == POST_AGENT_COMMIT_FAILED_REASON_CODE
    assert classification.failed_hooks == ()
    assert classification.format_repair_files == ()


@pytest.mark.unit
def test_format_plus_other_hook_failure_falls_through_to_precommit_reason() -> None:
    stdout = (
        "ruff format --check.....................................................Failed\n"
        "- hook id: awf-ruff-format-check\n"
        "- exit code: 1\n"
        "\n"
        "Would reformat: src/awf/control/executor.py\n"
        "1 file would be reformatted\n"
        "mypy....................................................................Failed\n"
        "- hook id: awf-mypy\n"
        "- exit code: 1\n"
        "\n"
        "src/awf/foo.py:42: error: Incompatible types\n"
    )
    classification = _classify_post_agent_commit_failure(_commit_result(stdout=stdout))

    assert classification.reason_code == POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE
    assert "awf-mypy" in classification.failed_hooks
    assert "awf-ruff-format-check" in classification.failed_hooks
    # Format repair files are still parsed; the executor will decide not to
    # repair because the failure set is wider than format only.
    assert classification.format_repair_files == ("src/awf/control/executor.py",)


@pytest.mark.unit
def test_format_paths_preserved_verbatim_for_executor_intersection() -> None:
    stdout = (
        "ruff format --check.....................................................Failed\n"
        "- hook id: awf-ruff-format-check\n"
        "- exit code: 1\n"
        "\n"
        "Would reformat: legacy/untouched.py\n"
        "Would reformat: src/awf/control/executor.py\n"
        "2 files would be reformatted\n"
    )
    classification = _classify_post_agent_commit_failure(_commit_result(stdout=stdout))

    assert classification.reason_code == POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE
    assert classification.format_repair_files == (
        "legacy/untouched.py",
        "src/awf/control/executor.py",
    )
