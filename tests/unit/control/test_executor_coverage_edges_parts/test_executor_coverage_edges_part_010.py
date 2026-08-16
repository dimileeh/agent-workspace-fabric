"""Focused unit tests for ``_run_post_agent_autofixable_precommit_repair``.

The deterministic-autofix repair path (``ruff check --fix`` then
``ruff format`` then re-stage then re-commit) is only exercised end-to-end via
the full ``executor.execute`` flow, which left its decision branches (skip when
no staged python files match the autofix set, ``ruff check --fix`` failure,
``ruff format`` failure, ``git add`` re-stage failure, and the success retry)
without direct behavior assertions. These tests call the helper with a fake
``self`` so each branch is asserted by its observable side effects (the recorded
repair event + the raised ``_PostAgentCommitStepError`` stage).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.control.executor import quality_methods
from awf.control.executor.constants import POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE
from awf.control.executor.quality_gates import (
    _PostAgentCommitClassification,
    _PostAgentCommitStepError,
)


def _autofix_classification(
    *,
    repair_files: tuple[str, ...] = ("src/app.py",),
    format_repair_files: tuple[str, ...] = (),
) -> _PostAgentCommitClassification:
    return _PostAgentCommitClassification(
        reason_code="POST_AGENT_COMMIT_AUTOFIX_NEEDED",
        failed_hooks=("ruff-check",),
        format_repair_files=format_repair_files,
        normalizer_repair_files=("src/other.py",),
        autofix_repair_files=repair_files,
        summary="ruff reported fixable diagnostics",
        repair_strategy="agent",
    )


def _fake_self(
    runner: FakeCommandRunner,
    *,
    record_calls: list[dict[str, Any]],
) -> Any:
    from types import SimpleNamespace

    async def _record(**kwargs: Any) -> None:
        record_calls.append(kwargs)

    return SimpleNamespace(
        _runner=runner,
        _record_post_agent_commit_format_repair=_record,
        _repair_agent_git_ownership=AsyncMock(),
    )


@pytest.mark.unit
async def test_autofix_repair_skips_when_no_staged_python_matches(tmp_path: Path) -> None:
    """Autofix files that are not in the staged python set skip the repair.

    The repair must record a ``skipped`` outcome (no ruff subprocess runs) and
    return ``False`` so the caller proceeds to the next repair strategy. The
    recorded event carries the formatter/normalizer paths for observability even
    though no fix ran.
    """
    runner = FakeCommandRunner()
    record_calls: list[dict[str, Any]] = []
    self_obj = _fake_self(runner, record_calls=record_calls)
    classification = _autofix_classification(repair_files=("src/missing.py",))

    result = await quality_methods._run_post_agent_autofixable_precommit_repair(  # noqa: SLF001
        self_obj,
        workspace_id="ws_skip",
        worktree_path=tmp_path,
        commit_result=CommandResult(returncode=1, stdout="", stderr=""),
        classification=classification,
        staged_paths=["docs/readme.md"],
        run_commit=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert result is False
    assert runner.calls == []
    assert record_calls and record_calls[0]["retry_outcome"] == "skipped"
    assert record_calls[0]["repaired_paths"] == []
    assert record_calls[0]["reason_code"] == "POST_AGENT_COMMIT_AUTOFIX_NEEDED"


@pytest.mark.unit
async def test_autofix_repair_raises_when_ruff_check_fix_fails(tmp_path: Path) -> None:
    """A ``ruff check --fix`` subprocess failure records an error and re-raises.

    The first ruff invocation (``check --fix``) returning non-zero must record a
    repair event with ``retry_outcome="error"`` and the dedicated repair-failed
    reason code, then raise ``_PostAgentCommitStepError`` with stage
    ``ruff check --fix`` so the outer handler surfaces the distinct failure.
    """
    runner = FakeCommandRunner()
    runner.queue_result(returncode=2, stdout="", stderr="ruff crashed")
    record_calls: list[dict[str, Any]] = []
    self_obj = _fake_self(runner, record_calls=record_calls)
    classification = _autofix_classification()

    with pytest.raises(_PostAgentCommitStepError) as raised:
        await quality_methods._run_post_agent_autofixable_precommit_repair(  # noqa: SLF001
            self_obj,
            workspace_id="ws_check_fail",
            worktree_path=tmp_path,
            commit_result=CommandResult(returncode=1, stdout="", stderr=""),
            classification=classification,
            staged_paths=["src/app.py"],
            run_commit=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
            git_in_worktree=AsyncMock(
                return_value=CommandResult(returncode=0, stdout="", stderr="")
            ),
        )

    assert raised.value.stage == "ruff check --fix"
    assert record_calls and record_calls[0]["retry_outcome"] == "error"
    assert record_calls[0]["reason_code"] == POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE
    assert record_calls[0]["repaired_paths"] == ["src/app.py"]


@pytest.mark.unit
async def test_autofix_repair_raises_when_ruff_format_fails(tmp_path: Path) -> None:
    """A ``ruff format`` failure (after ``check --fix`` succeeds) re-raises.

    The format step runs over the union of repair + format paths; a non-zero
    format result records the error event and raises with stage
    ``ruff format``.
    """
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="", stderr="")  # ruff check --fix ok
    runner.queue_result(returncode=1, stdout="", stderr="format error")  # ruff format fails
    record_calls: list[dict[str, Any]] = []
    self_obj = _fake_self(runner, record_calls=record_calls)
    classification = _autofix_classification(format_repair_files=("src/other.py",))

    with pytest.raises(_PostAgentCommitStepError) as raised:
        await quality_methods._run_post_agent_autofixable_precommit_repair(  # noqa: SLF001
            self_obj,
            workspace_id="ws_format_fail",
            worktree_path=tmp_path,
            commit_result=CommandResult(returncode=1, stdout="", stderr=""),
            classification=classification,
            staged_paths=["src/app.py", "src/other.py"],
            run_commit=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
            git_in_worktree=AsyncMock(
                return_value=CommandResult(returncode=0, stdout="", stderr="")
            ),
        )

    assert raised.value.stage == "ruff format"
    assert record_calls and record_calls[0]["retry_outcome"] == "error"


@pytest.mark.unit
async def test_autofix_repair_raises_when_restage_git_add_fails(tmp_path: Path) -> None:
    """A failed re-stage ``git add`` after a successful ruff run re-raises.

    Both ruff invocations succeed but ``git_in_worktree`` (re-stage) returns
    non-zero; the repair records the error and raises with stage ``git add``.
    """
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="", stderr="")  # ruff check --fix ok
    runner.queue_result(returncode=0, stdout="", stderr="")  # ruff format ok
    record_calls: list[dict[str, Any]] = []
    self_obj = _fake_self(runner, record_calls=record_calls)
    classification = _autofix_classification()

    with pytest.raises(_PostAgentCommitStepError) as raised:
        await quality_methods._run_post_agent_autofixable_precommit_repair(  # noqa: SLF001
            self_obj,
            workspace_id="ws_add_fail",
            worktree_path=tmp_path,
            commit_result=CommandResult(returncode=1, stdout="", stderr=""),
            classification=classification,
            staged_paths=["src/app.py"],
            run_commit=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
            git_in_worktree=AsyncMock(
                return_value=CommandResult(returncode=128, stdout="", stderr="fatal: not a repo")
            ),
        )

    assert raised.value.stage == "git add"
    assert record_calls and record_calls[0]["retry_outcome"] == "error"
    assert record_calls[0]["restaged_paths"] == ["src/app.py"]


@pytest.mark.unit
async def test_autofix_repair_succeeds_and_records_committed_retry(tmp_path: Path) -> None:
    """A fully successful repair records a committed retry outcome and returns True.

    When ruff check --fix, ruff format, re-stage, and the retry commit all
    succeed, the helper records ``retry_outcome="committed"`` and returns
    ``True`` so the caller knows the workspace does not need further repair.
    """
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="", stderr="")  # ruff check --fix ok
    runner.queue_result(returncode=0, stdout="", stderr="")  # ruff format ok
    record_calls: list[dict[str, Any]] = []
    self_obj = _fake_self(runner, record_calls=record_calls)
    classification = _autofix_classification()

    result = await quality_methods._run_post_agent_autofixable_precommit_repair(  # noqa: SLF001
        self_obj,
        workspace_id="ws_success",
        worktree_path=tmp_path,
        commit_result=CommandResult(returncode=1, stdout="", stderr=""),
        classification=classification,
        staged_paths=["src/app.py"],
        run_commit=AsyncMock(
            return_value=CommandResult(returncode=0, stdout="committed", stderr="")
        ),
        git_in_worktree=AsyncMock(return_value=CommandResult(returncode=0, stdout="", stderr="")),
    )

    assert result is True
    assert record_calls and record_calls[0]["retry_outcome"] == "succeeded"
    assert record_calls[0]["repaired_paths"] == ["src/app.py"]
