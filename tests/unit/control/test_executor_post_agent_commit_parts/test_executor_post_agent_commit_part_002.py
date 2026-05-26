"""Executor-level scenarios for post-agent commit classification + repair.

These tests drive ``WorkspaceExecutor.execute`` through the post-agent
``git add`` / ``git commit`` block. They lock in three behaviors:

  1. Pre-commit hook failures during post-agent commit get a structured
     reason code (``POST_AGENT_COMMIT_PRECOMMIT_FAILED`` /
     ``POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED`` /
     ``POST_AGENT_COMMIT_FAILED``) — distinct from the generic
     ``INFRASTRUCTURE_FAILURE`` default and distinct from
     ``AGENT_IDLE_TIMEOUT`` / ``AGENT_TIMEOUT``.

  2. The format-only failure path runs a scoped ``ruff format`` against
     the intersection of the agent's staged diff and the
     ``Would reformat:`` paths from the pre-commit output, retries the
     commit once, and emits a structured event recording the outcome.

  3. When the agent raised ``AgentRunError`` (timeout / provider failure)
     before the commit step, the agent's original reason code wins on
     the terminal ``workspace.state_changed`` event — the commit-step
     diagnostics are nested under ``payload["details"]["post_agent_commit"]``
     for observability but do NOT overwrite the agent's classification.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populate registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.control.executor.constants import (
    POST_AGENT_COMMIT_FAILED_REASON_CODE,
    POST_AGENT_GIT_ADD_FAILED_REASON_CODE,
)
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import (
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.pr_creator import PullRequestCreator
from tests.postgres import postgres_test_engine
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_001 import (
    _NoopResumeCompose,
    _RecordingValidation,
    _seed_ready,
)


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        session_factory._awf_test_worktrees_root = tmp_path / "work" / "worktrees"  # type: ignore[attr-defined]
        yield session_factory


@pytest.fixture
def fake() -> FakeCommandRunner:
    return FakeCommandRunner()


def _make_executor(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    validation: Any = None,
) -> WorkspaceExecutor:
    return WorkspaceExecutor(
        session_factory=factory,
        runner=fake,
        compose=_NoopResumeCompose(),
        validation=validation or _RecordingValidation(),
        pr_creator=PullRequestCreator(fake),
        config=ExecutorConfig(
            worktrees_root=tmp_path / "work" / "worktrees",
            compose_projects_root=tmp_path / "work" / "compose",
            default_models={
                AgentRuntime.codex: "gpt-5",
                AgentRuntime.claude_code: "sonnet",
                AgentRuntime.gemini: "gemini-2.5-pro",
            },
        ),
    )


def _git_add_suffixes(fake: FakeCommandRunner) -> list[list[str]]:
    return [call.args[call.args.index("add") :] for call in fake.calls if "add" in call.args]


def _precommit_format_only_output(*paths: str) -> str:
    """Mimic ``pre-commit`` framing when only ``awf-ruff-format-check`` fails."""
    lines = [
        "trailing-whitespace.....................................................Passed",
        "ruff check..............................................................Passed",
        "ruff format --check.....................................................Failed",
        "- hook id: awf-ruff-format-check",
        "- exit code: 1",
        "",
    ]
    for path in paths:
        lines.append(f"Would reformat: {path}")
    lines.append(f"{len(paths)} file{'s' if len(paths) != 1 else ''} would be reformatted")
    lines.append("")
    return "\n".join(lines)


def _precommit_mypy_output() -> str:
    return "\n".join(
        [
            "trailing-whitespace.....................................................Passed",
            "ruff check..............................................................Passed",
            "ruff format --check.....................................................Passed",
            "mypy....................................................................Failed",
            "- hook id: awf-mypy",
            "- exit code: 1",
            "",
            "src/awf/foo.py:42: error: Incompatible types",
            "",
        ]
    )


def _precommit_eof_only_output(*paths: str) -> str:
    lines = [
        "fix end of files.......................................................Failed",
        "- hook id: end-of-file-fixer",
        "- exit code: 1",
        "- files were modified by this hook",
        "",
    ]
    for path in paths:
        lines.append(f"Fixing {path}")
    lines.append("")
    return "\n".join(lines)


def _precommit_whitespace_eof_and_format_output(*format_paths: str) -> str:
    lines = [
        "trim trailing whitespace.................................................Failed",
        "- hook id: trailing-whitespace",
        "- exit code: 1",
        "- files were modified by this hook",
        "",
        "Fixing docs/awf-plans/ws_761.md",
        "fix end of files.......................................................Failed",
        "- hook id: end-of-file-fixer",
        "- exit code: 1",
        "- files were modified by this hook",
        "",
        "Fixing docs/awf-plans/ws_761.conformance.json",
        "ruff format --check.....................................................Failed",
        "- hook id: awf-ruff-format-check",
        "- exit code: 1",
        "",
    ]
    for path in format_paths:
        lines.append(f"Would reformat: {path}")
    lines.append("")
    return "\n".join(lines)


def _precommit_ruff_check_and_format_output(*format_paths: str) -> str:
    lines = [
        "fix end of files.......................................................Failed",
        "- hook id: end-of-file-fixer",
        "- exit code: 1",
        "- files were modified by this hook",
        "",
        "Fixing docs/awf-plans/ws_89.conformance.json",
        "ruff check..............................................................Failed",
        "- hook id: awf-ruff-check",
        "- exit code: 1",
        "",
        "F401 fix_test.py imported but unused",
        "ruff format --check.....................................................Failed",
        "- hook id: awf-ruff-format-check",
        "- exit code: 1",
        "",
    ]
    for path in format_paths:
        lines.append(f"Would reformat: {path}")
    lines.append("")
    return "\n".join(lines)


def _precommit_autofixable_ruff_check_output(*paths: str) -> str:
    lines = [
        "ruff check..............................................................Failed",
        "- hook id: awf-ruff-check",
        "- exit code: 1",
        "",
    ]
    for path in paths:
        lines.extend(
            [
                "I001 [*] Import block is un-sorted or un-formatted",
                f"   --> {path}:13:1",
            ]
        )
    lines.extend(
        [
            f"Found {len(paths)} error{'s' if len(paths) != 1 else ''}.",
            f"[*] {len(paths)} fixable with the `--fix` option.",
            "",
        ]
    )
    return "\n".join(lines)


async def _failed_state_event(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> Any:
    async with factory() as session:
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.state_changed",
        )
    failed_event = next(
        (event for event in reversed(events) if event.new_state == "failed"),
        None,
    )
    assert failed_event is not None, (
        f"no failed workspace.state_changed event found for workspace {workspace_id}"
    )
    return failed_event


@pytest.mark.unit
async def test_post_agent_git_add_failure_with_empty_output_keeps_structured_reason(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed_ready(factory)
    fake.queue_result(returncode=0, stdout="adapter ok")  # agent
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=128)  # git add -A FAILS with no output

    executor = _make_executor(fake, factory, tmp_path)
    await executor.execute(ws_id)

    event = await _failed_state_event(factory, ws_id)
    assert event.reason_code == POST_AGENT_GIT_ADD_FAILED_REASON_CODE
    assert event.payload is not None
    details = event.payload["details"]["post_agent_commit"]
    assert details["stage"] == "git add"
    assert "summary" not in details
    assert details["precommit_repair_attempted"] is False


@pytest.mark.unit
async def test_post_agent_commit_non_precommit_failure_uses_generic_reason(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed_ready(factory)
    fake.queue_result(returncode=0, stdout="adapter ok")
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add
    fake.queue_result(returncode=0, stdout="src/foo.py\n")  # cached diff
    fake.queue_result(
        returncode=1, stderr="fatal: empty ident name (for <>) not allowed"
    )  # git commit FAILS — no pre-commit framing

    executor = _make_executor(fake, factory, tmp_path)
    await executor.execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value

    event = await _failed_state_event(factory, ws_id)
    assert event.reason_code == POST_AGENT_COMMIT_FAILED_REASON_CODE
