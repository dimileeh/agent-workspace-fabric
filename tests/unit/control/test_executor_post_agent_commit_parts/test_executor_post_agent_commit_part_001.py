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

from awf.adapters import base as adapter_base
from awf.adapters import registry as _registry  # noqa: F401 — populate registry
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.control.executor.constants import (
    POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE,
    POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE,
    POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE,
    POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
    POST_AGENT_GIT_ADD_FAILED_REASON_CODE,
)
from awf.control.quality_gates import PLAN_ONLY_OUTPUT_REASON_CODE
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import (
    PolicyFindingRepository,
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


def _precommit_autofixable_ruff_check_and_format_output(*paths: str) -> str:
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
            "ruff format --check.....................................................Failed",
            "- hook id: awf-ruff-format-check",
            "- exit code: 1",
            "",
        ]
    )
    for path in paths:
        lines.append(f"Would reformat: {path}")
    lines.append("")
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
async def test_post_agent_commit_precommit_failure_uses_precommit_reason_code(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed_ready(factory)
    fake.queue_result(returncode=0, stdout="adapter ok")
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add
    fake.queue_result(returncode=0, stdout="src/awf/foo.py\n")  # cached diff
    fake.queue_result(returncode=1, stdout=_precommit_mypy_output())  # git commit fails
    fake.queue_result(returncode=1, stderr="repair failed")  # targeted agent repair fails
    fake.queue_result(returncode=0)  # git add -A stages repair edits
    fake.queue_result(returncode=0, stdout="src/awf/foo.py\n")  # cached diff after salvage
    fake.queue_result(returncode=1, stdout=_precommit_mypy_output())  # retry still fails

    executor = _make_executor(fake, factory, tmp_path)
    await executor.execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"

    event = await _failed_state_event(factory, ws_id)
    assert event.reason_code == POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE
    assert event.payload is not None
    assert event.payload["reason_code"] == POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE
    details = event.payload["details"]
    assert isinstance(details, dict)
    commit_details = details["post_agent_commit"]
    assert commit_details["reason_code"] == POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE
    assert commit_details["repair_strategy"] == "agent"
    assert commit_details["precommit_repair_attempted"] is True
    assert "awf-mypy" in commit_details["failed_hooks"]

    async with factory() as s:
        repair_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type=POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE,
        )
    assert repair_events
    assert repair_events[-1].reason_code == POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE
    assert repair_events[-1].payload["retry_outcome"] == "error"  # type: ignore[index]
    assert repair_events[-1].payload["restaged_paths"] == ["src/awf/foo.py"]  # type: ignore[index]

    # The failed repair-agent run may still have edited files; AWF stages those
    # partial edits and retries the commit once before giving up.
    commit_calls = [call for call in fake.calls if "commit" in call.args]
    assert len(commit_calls) == 2


@pytest.mark.unit
async def test_post_agent_commit_format_only_failure_repairs_and_retries(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed_ready(factory)
    fake.queue_result(returncode=0, stdout="adapter ok")  # agent
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="src/foo.py\n")  # cached diff
    fake.queue_result(
        returncode=1, stdout=_precommit_format_only_output("src/foo.py")
    )  # git commit fails with format-only
    fake.queue_result(returncode=0)  # ruff format src/foo.py
    fake.queue_result(returncode=0)  # git add -- src/foo.py
    fake.queue_result(returncode=0)  # git commit retry ok
    # After the commit step succeeds, the executor proceeds into the
    # rev-list/ancestor checks. Returning ``0`` here drives it through
    # the "no commits salvaged" branch so we don't need to set up the
    # full validation → push → gh pr create pipeline; the success of the
    # format-repair is observable via the event payload regardless of
    # how the workspace later terminates.
    fake.queue_result(returncode=0, stdout="0\n")  # rev-list count = 0

    executor = _make_executor(fake, factory, tmp_path, validation=_RecordingValidation())
    await executor.execute(ws_id)

    async with factory() as s:
        repair_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type=POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE,
        )

    assert len(repair_events) == 1
    payload = repair_events[0].payload
    assert isinstance(payload, dict)
    assert payload["repaired_paths"] == ["src/foo.py"]
    assert payload["retry_outcome"] == "succeeded"
    # A successful repair keeps the original rewrite-needed reason
    # code — the dedicated repair-failed code is reserved for "error"
    # outcomes.
    assert repair_events[0].reason_code == POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE

    ruff_calls = [call for call in fake.calls if "ruff" in call.args and "format" in call.args]
    assert ruff_calls, "expected a ruff format invocation"
    assert "src/foo.py" in ruff_calls[0].args
    assert "--check" not in ruff_calls[0].args
    # ``ruff format`` must run inside the workspace worktree — otherwise the
    # worktree-relative paths from ``Would reformat:`` resolve against the
    # executor's own cwd and the retry commit re-fails on the same files.
    assert ruff_calls[0].cwd is not None
    assert ruff_calls[0].cwd.endswith(ws_id)

    # Two git commit invocations: the initial failing attempt + the retry.
    commit_calls = [call for call in fake.calls if "commit" in call.args]
    assert len(commit_calls) == 2


@pytest.mark.unit
async def test_post_agent_commit_format_repair_retry_still_fails_marks_precommit(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed_ready(factory)
    fake.queue_result(returncode=0, stdout="adapter ok")  # agent
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="src/foo.py\n")  # cached diff
    fake.queue_result(
        returncode=1, stdout=_precommit_format_only_output("src/foo.py")
    )  # initial commit fails (format only)
    fake.queue_result(returncode=0)  # ruff format src/foo.py
    fake.queue_result(returncode=0)  # git add -- src/foo.py
    fake.queue_result(returncode=1, stdout=_precommit_mypy_output())  # retry fails (mypy)

    executor = _make_executor(fake, factory, tmp_path)
    await executor.execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        repair_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type=POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE,
        )

    assert len(repair_events) == 1
    repair_payload = repair_events[0].payload
    assert isinstance(repair_payload, dict)
    assert repair_payload["repaired_paths"] == ["src/foo.py"]
    assert repair_payload["retry_outcome"] == "failed"
    # A "failed" outcome (repair ran, retry commit hit a non-format
    # hook) is not a repair-pipeline failure — keep the rewrite-needed
    # reason code so the dedicated repair-failed code stays reserved
    # for "error" outcomes only.
    assert repair_events[0].reason_code == POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE

    event = await _failed_state_event(factory, ws_id)
    assert event.reason_code == POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE
    assert event.payload is not None
    assert event.payload["details"]["post_agent_commit"]["format_repair_attempted"] is True


@pytest.mark.unit
async def test_post_agent_commit_format_repair_retry_same_format_hook_marks_repair_failed(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Retry commit re-fails with the same ``awf-ruff-format-check`` hook.

    When ``ruff format`` ran but the retry commit is rejected by the same
    format hook (e.g. ruff couldn't normalize a file), the terminal reason
    code MUST be the dedicated repair-failed code — NOT
    ``POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED``, whose REASON_CATALOG
    entry only describes the empty-intersection skip. Pairing the
    rewrite-needed code with ``format_repair_attempted=True`` would be
    self-contradictory on operator dashboards.
    """
    ws_id = await _seed_ready(factory)
    fake.queue_result(returncode=0, stdout="adapter ok")  # agent
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="src/foo.py\n")  # cached diff
    fake.queue_result(
        returncode=1, stdout=_precommit_format_only_output("src/foo.py")
    )  # initial commit fails (format only)
    fake.queue_result(returncode=0)  # ruff format src/foo.py
    fake.queue_result(returncode=0)  # git add -- src/foo.py
    fake.queue_result(
        returncode=1, stdout=_precommit_format_only_output("src/foo.py")
    )  # retry fails with the SAME format hook

    executor = _make_executor(fake, factory, tmp_path)
    await executor.execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        repair_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type=POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE,
        )

    assert len(repair_events) == 1
    repair_payload = repair_events[0].payload
    assert isinstance(repair_payload, dict)
    assert repair_payload["repaired_paths"] == ["src/foo.py"]
    assert repair_payload["retry_outcome"] == "failed"
    # The repair event itself keeps the rewrite-needed reason code — the
    # dedicated repair-failed code is reserved for "error" outcomes, and
    # ``retry_outcome="failed"`` is not an error outcome.
    assert repair_events[0].reason_code == POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE

    event = await _failed_state_event(factory, ws_id)
    # Terminal reason code MUST be the dedicated repair-failed code, not
    # POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED — the REASON_CATALOG entry
    # for that code describes only the empty-intersection skip, which is
    # self-contradictory once ``format_repair_attempted=True``.
    assert event.reason_code == POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE
    assert event.payload is not None
    assert event.payload["reason_code"] == POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE
    commit_details = event.payload["details"]["post_agent_commit"]
    assert commit_details["stage"] == "git commit"
    assert commit_details["reason_code"] == POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE
    assert commit_details["format_repair_attempted"] is True

    # Two git commit invocations: the initial failing attempt + the retry.
    commit_calls = [call for call in fake.calls if "commit" in call.args]
    assert len(commit_calls) == 2


@pytest.mark.unit
async def test_post_agent_commit_eof_only_hook_modification_restages_and_retries(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed_ready(factory)
    fake.queue_result(returncode=0, stdout="adapter ok")  # agent
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(
        returncode=0,
        stdout=("docs/awf-plans/ws_06.md\ndocs/awf-plans/ws_06.conformance.json\nsrc/awf/api.py\n"),
    )  # cached diff
    fake.queue_result(
        returncode=1,
        stdout=_precommit_eof_only_output(
            "docs/awf-plans/ws_06.conformance.json",
        ),
    )  # git commit fails after EOF hook rewrote an AWF artifact
    fake.queue_result(returncode=0)  # git add -- original staged paths
    fake.queue_result(returncode=0)  # git commit retry ok
    fake.queue_result(returncode=0, stdout="0\n")  # rev-list count = 0

    executor = _make_executor(fake, factory, tmp_path, validation=_RecordingValidation())
    await executor.execute(ws_id)

    async with factory() as s:
        repair_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type=POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE,
        )

    assert len(repair_events) == 1
    assert repair_events[0].reason_code == POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE
    payload = repair_events[0].payload
    assert isinstance(payload, dict)
    assert payload["repair_strategy"] == "deterministic"
    assert payload["retry_outcome"] == "succeeded"
    assert payload["failed_hooks"] == ["end-of-file-fixer"]
    assert payload["restaged_paths"] == [
        "docs/awf-plans/ws_06.md",
        "docs/awf-plans/ws_06.conformance.json",
        "src/awf/api.py",
    ]
    assert payload["repaired_paths"] == []
    assert payload["normalizer_paths"] == ["docs/awf-plans/ws_06.conformance.json"]

    ruff_calls = [call for call in fake.calls if "ruff" in call.args]
    assert not ruff_calls
    restage_calls = [
        call
        for call in fake.calls
        if "add" in call.args and "--" in call.args and "src/awf/api.py" in call.args
    ]
    assert restage_calls


@pytest.mark.unit
async def test_post_agent_commit_mixed_deterministic_hooks_formats_restages_and_retries(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed_ready(factory)
    fake.queue_result(returncode=0, stdout="adapter ok")  # agent
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(
        returncode=0,
        stdout=(
            "docs/awf-plans/ws_761.md\n"
            "docs/awf-plans/ws_761.conformance.json\n"
            "tests/unit/mcp/test_mcp_server.py\n"
        ),
    )  # cached diff
    fake.queue_result(
        returncode=1,
        stdout=_precommit_whitespace_eof_and_format_output(
            "tests/unit/mcp/test_mcp_server.py",
        ),
    )
    fake.queue_result(returncode=0)  # ruff format tests/unit/mcp/test_mcp_server.py
    fake.queue_result(returncode=0)  # git add -- original staged paths + formatter path
    fake.queue_result(returncode=0)  # git commit retry ok
    fake.queue_result(returncode=0, stdout="0\n")  # rev-list count = 0

    executor = _make_executor(fake, factory, tmp_path, validation=_RecordingValidation())
    await executor.execute(ws_id)

    async with factory() as s:
        repair_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type=POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE,
        )

    assert len(repair_events) == 1
    assert repair_events[0].reason_code == POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE
    payload = repair_events[0].payload
    assert isinstance(payload, dict)
    assert payload["repair_strategy"] == "deterministic"
    assert payload["retry_outcome"] == "succeeded"
    assert payload["failed_hooks"] == [
        "trailing-whitespace",
        "end-of-file-fixer",
        "awf-ruff-format-check",
    ]
    assert payload["formatter_paths"] == ["tests/unit/mcp/test_mcp_server.py"]
    assert payload["repaired_paths"] == ["tests/unit/mcp/test_mcp_server.py"]
    assert payload["normalizer_paths"] == [
        "docs/awf-plans/ws_761.md",
        "docs/awf-plans/ws_761.conformance.json",
    ]
    assert payload["restaged_paths"] == [
        "docs/awf-plans/ws_761.md",
        "docs/awf-plans/ws_761.conformance.json",
        "tests/unit/mcp/test_mcp_server.py",
    ]

    ruff_calls = [call for call in fake.calls if "ruff" in call.args and "format" in call.args]
    assert len(ruff_calls) == 1
    assert "tests/unit/mcp/test_mcp_server.py" in ruff_calls[0].args
    commit_calls = [call for call in fake.calls if "commit" in call.args]
    assert len(commit_calls) == 2


@pytest.mark.unit
async def test_post_agent_commit_semantic_precommit_failure_invokes_targeted_agent_repair(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed_ready(factory)
    fake.queue_result(returncode=0, stdout="adapter ok")  # agent
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(
        returncode=0,
        stdout="fix_test.py\nrun_debug.py\nsrc/awf/mcp.py\n",
    )  # cached diff
    fake.queue_result(
        returncode=1,
        stdout=_precommit_ruff_check_and_format_output("run_debug.py"),
    )  # semantic pre-commit failure: must not auto-format-only
    fake.queue_result(returncode=0, stdout="repair ok")  # targeted agent repair
    fake.queue_result(returncode=0)  # git add -A after repair
    fake.queue_result(returncode=0, stdout="src/awf/mcp.py\n")  # cached diff after repair
    fake.queue_result(returncode=0)  # git commit retry ok
    fake.queue_result(returncode=0, stdout="0\n")  # rev-list count = 0

    executor = _make_executor(fake, factory, tmp_path, validation=_RecordingValidation())
    await executor.execute(ws_id)

    async with factory() as s:
        repair_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type=POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE,
        )

    assert len(repair_events) == 1
    assert repair_events[0].reason_code == POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE
    payload = repair_events[0].payload
    assert isinstance(payload, dict)
    assert payload["repair_strategy"] == "agent"
    assert payload["retry_outcome"] == "succeeded"
    assert payload["failed_hooks"] == [
        "end-of-file-fixer",
        "awf-ruff-check",
        "awf-ruff-format-check",
    ]
    assert payload["normalizer_paths"] == ["docs/awf-plans/ws_89.conformance.json"]

    agent_repair_calls = [
        call
        for call in fake.calls
        if call.input_bytes is not None and b"post-agent pre-commit repair" in call.input_bytes
    ]
    assert len(agent_repair_calls) == 1
    prompt = agent_repair_calls[0].input_bytes.decode()
    assert "awf-ruff-check" in prompt
    assert "Do not bypass pre-commit" in prompt
    assert "run_debug.py" in prompt
    assert "Normalizer-rewritten paths, if any:" in prompt
    assert "docs/awf-plans/ws_89.conformance.json" in prompt

    ruff_calls = [call for call in fake.calls if "ruff" in call.args and "format" in call.args]
    assert not ruff_calls
    assert _git_add_suffixes(fake).count(["add", "-A"]) == 2
    commit_calls = [call for call in fake.calls if "commit" in call.args]
    assert len(commit_calls) == 2


@pytest.mark.unit
async def test_post_agent_commit_autofixable_ruff_check_runs_bounded_fix_before_agent(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed_ready(factory)
    fake.queue_result(returncode=0, stdout="adapter ok")  # agent
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="src/awf/mcp/server.py\n")  # cached diff
    fake.queue_result(
        returncode=1,
        stdout=_precommit_autofixable_ruff_check_output("src/awf/mcp/server.py"),
    )  # semantic hook, but every diagnostic is tool-marked fixable
    fake.queue_result(returncode=0)  # ruff check --fix src/awf/mcp/server.py
    fake.queue_result(returncode=0)  # git add -- original staged paths
    fake.queue_result(returncode=0)  # git commit retry ok
    fake.queue_result(returncode=0, stdout="0\n")  # rev-list count = 0

    executor = _make_executor(fake, factory, tmp_path, validation=_RecordingValidation())
    await executor.execute(ws_id)

    async with factory() as s:
        repair_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type=POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE,
        )

    assert len(repair_events) == 1
    assert repair_events[0].reason_code == POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE
    payload = repair_events[0].payload
    assert isinstance(payload, dict)
    assert payload["repair_strategy"] == "deterministic_autofix"
    assert payload["retry_outcome"] == "succeeded"
    assert payload["failed_hooks"] == ["awf-ruff-check"]
    assert payload["repaired_paths"] == ["src/awf/mcp/server.py"]
    assert payload["restaged_paths"] == ["src/awf/mcp/server.py"]

    ruff_fix_calls = [
        call
        for call in fake.calls
        if "ruff" in call.args and "check" in call.args and "--fix" in call.args
    ]
    assert len(ruff_fix_calls) == 1
    assert "src/awf/mcp/server.py" in ruff_fix_calls[0].args
    assert ruff_fix_calls[0].cwd is not None
    assert ruff_fix_calls[0].cwd.endswith(ws_id)

    agent_repair_calls = [
        call
        for call in fake.calls
        if call.input_bytes is not None and b"post-agent pre-commit repair" in call.input_bytes
    ]
    assert agent_repair_calls == []


@pytest.mark.unit
async def test_post_agent_commit_autofixable_ruff_check_also_formats_reported_paths(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed_ready(factory)
    path = "tests/unit/service/test_host_setup_credentials.py"
    fake.queue_result(returncode=0, stdout="adapter ok")  # agent
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout=f"{path}\n")  # cached diff
    fake.queue_result(
        returncode=1,
        stdout=_precommit_autofixable_ruff_check_and_format_output(path),
    )  # both Ruff hooks failed, but check diagnostics are autofixable
    fake.queue_result(returncode=0)  # ruff check --fix path
    fake.queue_result(returncode=0)  # ruff format path
    fake.queue_result(returncode=0)  # git add -- original staged paths
    fake.queue_result(returncode=0)  # git commit retry ok
    fake.queue_result(returncode=0, stdout="0\n")  # stop before validation/PR pipeline

    executor = _make_executor(fake, factory, tmp_path, validation=_RecordingValidation())
    await executor.execute(ws_id)

    async with factory() as s:
        repair_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type=POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE,
        )

    assert len(repair_events) == 1
    assert repair_events[0].reason_code == POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE
    payload = repair_events[0].payload
    assert isinstance(payload, dict)
    assert payload["repair_strategy"] == "deterministic_autofix"
    assert payload["retry_outcome"] == "succeeded"
    assert payload["failed_hooks"] == ["awf-ruff-check", "awf-ruff-format-check"]
    assert payload["repaired_paths"] == [path]
    assert payload["formatter_paths"] == [path]
    assert payload["restaged_paths"] == [path]

    ruff_fix_calls = [
        call
        for call in fake.calls
        if "ruff" in call.args and "check" in call.args and "--fix" in call.args
    ]
    assert len(ruff_fix_calls) == 1
    assert path in ruff_fix_calls[0].args

    ruff_format_calls = [
        call
        for call in fake.calls
        if "ruff" in call.args and "format" in call.args and "--check" not in call.args
    ]
    assert len(ruff_format_calls) == 1
    assert path in ruff_format_calls[0].args
    assert ruff_format_calls[0].cwd is not None
    assert ruff_format_calls[0].cwd.endswith(ws_id)

    agent_repair_calls = [
        call
        for call in fake.calls
        if call.input_bytes is not None and b"post-agent pre-commit repair" in call.input_bytes
    ]
    assert agent_repair_calls == []


@pytest.mark.unit
async def test_post_agent_commit_semantic_repair_stages_new_files_before_policy_checks(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed_ready(factory)
    fake.queue_result(returncode=0, stdout="adapter ok")  # agent
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="src/awf/foo.py\n")  # cached diff
    fake.queue_result(
        returncode=1,
        stdout=_precommit_ruff_check_and_format_output("src/awf/foo.py"),
    )  # semantic pre-commit failure
    fake.queue_result(returncode=0, stdout="repair ok")  # targeted agent repair
    fake.queue_result(returncode=0)  # git add -A after repair
    fake.queue_result(
        returncode=0,
        stdout="src/awf/foo.py\ntests/unit/control/test_foo.py\n",
    )  # cached diff includes file created by repair
    fake.queue_result(returncode=0)  # git commit retry ok
    fake.queue_result(returncode=0, stdout="0\n")  # rev-list count = 0

    executor = _make_executor(fake, factory, tmp_path, validation=_RecordingValidation())
    await executor.execute(ws_id)

    assert _git_add_suffixes(fake).count(["add", "-A"]) == 2

    async with factory() as s:
        repair_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type=POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE,
        )

    assert len(repair_events) == 1
    payload = repair_events[0].payload
    assert isinstance(payload, dict)
    assert payload["repair_strategy"] == "agent"
    assert payload["retry_outcome"] == "succeeded"
    assert payload["restaged_paths"] == [
        "src/awf/foo.py",
        "tests/unit/control/test_foo.py",
    ]


@pytest.mark.unit
async def test_post_agent_commit_semantic_agent_repair_failure_records_partial_commit(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed_ready(factory)
    fake.queue_result(returncode=0, stdout="adapter ok")  # agent
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="fix_test.py\n")  # cached diff
    fake.queue_result(
        returncode=1,
        stdout=_precommit_ruff_check_and_format_output("fix_test.py"),
    )  # semantic pre-commit failure
    fake.queue_result(returncode=1, stderr="repair failed")  # targeted repair fails
    fake.queue_result(returncode=0)  # git add -A stages repair edits
    fake.queue_result(returncode=0, stdout="src/awf/foo.py\n")  # cached diff after salvage
    fake.queue_result(returncode=0)  # git commit retry ok
    fake.queue_result(returncode=0, stdout="0\n")  # rev-list count = 0

    executor = _make_executor(fake, factory, tmp_path, validation=_RecordingValidation())
    await executor.execute(ws_id)

    async with factory() as s:
        repair_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type=POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE,
        )

    assert len(repair_events) == 1
    assert repair_events[0].reason_code == POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE
    payload = repair_events[0].payload
    assert isinstance(payload, dict)
    assert payload["repair_strategy"] == "agent"
    assert payload["retry_outcome"] == "agent_error_partial_commit"
    assert payload["failed_hooks"] == [
        "end-of-file-fixer",
        "awf-ruff-check",
        "awf-ruff-format-check",
    ]
    assert payload["restaged_paths"] == ["src/awf/foo.py"]

    commit_calls = [call for call in fake.calls if "commit" in call.args]
    assert len(commit_calls) == 2


@pytest.mark.unit
async def test_post_agent_commit_semantic_agent_repair_git_add_failure_marks_repair_failed(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed_ready(factory)
    fake.queue_result(returncode=0, stdout="adapter ok")  # agent
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="fix_test.py\n")  # cached diff
    fake.queue_result(
        returncode=1,
        stdout=_precommit_ruff_check_and_format_output("fix_test.py"),
    )  # semantic pre-commit failure
    fake.queue_result(returncode=0, stdout="repair ok")  # targeted repair succeeds
    fake.queue_result(returncode=128, stderr="fatal: not a git repository")  # git add fails

    executor = _make_executor(fake, factory, tmp_path)
    await executor.execute(ws_id)

    event = await _failed_state_event(factory, ws_id)
    assert event.reason_code == POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE
    assert event.payload is not None
    details = event.payload["details"]["post_agent_commit"]
    assert details["stage"] == "git add"
    assert details["repair_strategy"] == "agent"
    assert details["precommit_repair_attempted"] is True

    async with factory() as s:
        repair_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type=POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE,
        )
    assert repair_events
    assert repair_events[-1].reason_code == POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE
    assert repair_events[-1].payload["retry_outcome"] == "error"  # type: ignore[index]


@pytest.mark.unit
async def test_post_agent_commit_semantic_agent_repair_protected_gate_change_is_blocked(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed_ready(factory, owned_paths=["src/awf/**"])
    fake.queue_result(returncode=0, stdout="adapter ok")  # agent
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="fix_test.py\n")  # cached diff
    fake.queue_result(
        returncode=1,
        stdout=_precommit_ruff_check_and_format_output("fix_test.py"),
    )  # semantic pre-commit failure
    fake.queue_result(returncode=0, stdout="repair ok")  # targeted repair succeeds
    fake.queue_result(returncode=0)  # git add -A after repair
    fake.queue_result(returncode=0, stdout="pyproject.toml\n")  # repair changed gate file
    fake.queue_result(
        returncode=0,
        stdout="[tool.coverage.report]\nfail_under = 99\n",
    )  # old protected gate content
    fake.queue_result(
        returncode=0,
        stdout="[tool.coverage.report]\nfail_under = 90\n",
    )  # staged protected gate content

    executor = _make_executor(fake, factory, tmp_path)
    await executor.execute(ws_id)

    event = await _failed_state_event(factory, ws_id)
    assert event.reason_code == "QUALITY_GATE_POLICY_CHANGED"
    assert event.payload is not None
    details = event.payload["details"]["post_agent_commit"]
    assert details["stage"] == "post-agent pre-commit repair policy"
    assert details["repair_strategy"] == "agent"
    assert "protected quality-gate" in details["summary"]

    commit_calls = [call for call in fake.calls if "commit" in call.args]
    assert len(commit_calls) == 1


@pytest.mark.unit
async def test_post_agent_commit_semantic_agent_repair_supply_chain_change_is_blocked(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed_ready(
        factory,
        owned_paths=["src/awf/**"],
        resolved_profile={
            "name": "semantic-repair-supply-chain-block",
            "security": {
                "supply_chain": {
                    "unpinned_dependency_installs": {"mode": "block"},
                    "lockfile_changes_outside_owned_paths": {"mode": "block"},
                }
            },
        },
    )
    fake.queue_result(returncode=0, stdout="adapter ok")  # agent
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="src/awf/mcp.py\n")  # cached diff
    fake.queue_result(
        returncode=1,
        stdout=_precommit_ruff_check_and_format_output("src/awf/mcp.py"),
    )  # semantic pre-commit failure
    fake.queue_result(
        returncode=0,
        stdout="$ npm install left-pad\n",
    )  # targeted repair adds supply-chain evidence
    fake.queue_result(returncode=0)  # git add -A after repair
    fake.queue_result(
        returncode=0,
        stdout="src/awf/mcp.py\npackage-lock.json\n",
    )  # repair changed dependency files

    executor = _make_executor(fake, factory, tmp_path)
    await executor.execute(ws_id)

    event = await _failed_state_event(factory, ws_id)
    assert event.reason_code == "SUPPLY_CHAIN_POLICY_BLOCKED"
    assert event.payload is not None
    details = event.payload["details"]["post_agent_commit"]
    assert details["stage"] == "post-agent pre-commit repair policy"
    assert details["repair_strategy"] == "agent"
    assert "Supply-chain policy blocked workspace output" in details["summary"]

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        findings = await PolicyFindingRepository(s).list_active_for_workspace(ws_id)

    assert ws is not None
    assert ws.status == WorkspaceStatus.failed.value
    assert ws.failure_reason == "policy_failure"
    assert {finding.reason_code for finding in findings} == {
        "SUPPLY_CHAIN_UNPINNED_DEPENDENCY_INSTALL",
        "SUPPLY_CHAIN_LOCKFILE_OUTSIDE_OWNED_PATHS",
    }
    assert all(finding.severity == "blocking" for finding in findings)

    commit_calls = [call for call in fake.calls if "commit" in call.args]
    assert len(commit_calls) == 1


@pytest.mark.unit
async def test_post_agent_commit_semantic_agent_repair_cached_diff_failure_aborts_retry(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed_ready(factory)
    fake.queue_result(returncode=0, stdout="adapter ok")  # agent
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="fix_test.py\n")  # cached diff
    fake.queue_result(
        returncode=1,
        stdout=_precommit_ruff_check_and_format_output("fix_test.py"),
    )  # semantic pre-commit failure
    fake.queue_result(returncode=0, stdout="repair ok")  # targeted repair succeeds
    fake.queue_result(returncode=0)  # git add -A after repair
    fake.queue_result(returncode=128, stderr="fatal: index unreadable")  # cached diff fails

    executor = _make_executor(fake, factory, tmp_path)
    await executor.execute(ws_id)

    event = await _failed_state_event(factory, ws_id)
    assert event.reason_code == POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE
    assert event.payload is not None
    details = event.payload["details"]["post_agent_commit"]
    assert details["stage"] == "git diff --cached"
    assert details["repair_strategy"] == "agent"
    assert "fatal: index unreadable" in details["summary"]

    async with factory() as s:
        repair_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type=POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE,
        )
    assert repair_events
    assert repair_events[-1].reason_code == POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE
    assert repair_events[-1].payload["retry_outcome"] == "error"  # type: ignore[index]

    commit_calls = [call for call in fake.calls if "commit" in call.args]
    assert len(commit_calls) == 1


@pytest.mark.unit
async def test_post_agent_commit_semantic_agent_repair_plan_only_change_is_blocked(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed_ready(factory)
    fake.queue_result(returncode=0, stdout="adapter ok")  # agent
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="fix_test.py\n")  # cached diff
    fake.queue_result(
        returncode=1,
        stdout=_precommit_ruff_check_and_format_output("fix_test.py"),
    )  # semantic pre-commit failure
    fake.queue_result(returncode=0, stdout="repair ok")  # targeted repair succeeds
    fake.queue_result(returncode=0)  # git add -A after repair
    fake.queue_result(
        returncode=0,
        stdout="docs/awf-plans/ws_semantic_repair.md\n",
    )  # repair changed only plan output

    executor = _make_executor(fake, factory, tmp_path)
    await executor.execute(ws_id)

    event = await _failed_state_event(factory, ws_id)
    assert event.reason_code == PLAN_ONLY_OUTPUT_REASON_CODE
    assert event.payload is not None
    details = event.payload["details"]["post_agent_commit"]
    assert details["stage"] == "post-agent pre-commit repair policy"
    assert details["repair_strategy"] == "agent"
    assert "only AWF plan/conformance artifact changes" in details["summary"]

    async with factory() as s:
        repair_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type=POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE,
        )
    assert repair_events
    assert repair_events[-1].reason_code == PLAN_ONLY_OUTPUT_REASON_CODE
    assert repair_events[-1].payload["restaged_paths"] == [  # type: ignore[index]
        "docs/awf-plans/ws_semantic_repair.md"
    ]

    commit_calls = [call for call in fake.calls if "commit" in call.args]
    assert len(commit_calls) == 1


@pytest.mark.unit
async def test_post_agent_commit_semantic_agent_repair_normalizer_only_plan_output_is_blocked(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed_ready(factory)
    fake.queue_result(returncode=0, stdout="adapter ok")  # agent
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="fix_test.py\ndocs/awf-plans/ws_89.conformance.json\n")
    fake.queue_result(
        returncode=1,
        stdout=_precommit_ruff_check_and_format_output("fix_test.py"),
    )  # semantic pre-commit failure includes normalizer plan artifact
    fake.queue_result(returncode=0, stdout="repair ok")  # targeted repair succeeds
    fake.queue_result(returncode=0)  # git add -A after repair
    fake.queue_result(
        returncode=0,
        stdout="docs/awf-plans/ws_89.conformance.json\n",
    )  # only the normalizer-rewritten plan artifact remains staged

    executor = _make_executor(fake, factory, tmp_path)
    await executor.execute(ws_id)

    event = await _failed_state_event(factory, ws_id)
    assert event.reason_code == PLAN_ONLY_OUTPUT_REASON_CODE
    assert event.payload is not None
    details = event.payload["details"]["post_agent_commit"]
    assert details["stage"] == "post-agent pre-commit repair policy"
    assert details["repair_strategy"] == "agent"
    assert "only AWF plan/conformance artifact changes" in details["summary"]

    async with factory() as s:
        repair_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type=POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE,
        )
    assert repair_events
    assert repair_events[-1].reason_code == PLAN_ONLY_OUTPUT_REASON_CODE
    assert repair_events[-1].payload["restaged_paths"] == [  # type: ignore[index]
        "docs/awf-plans/ws_89.conformance.json"
    ]

    commit_calls = [call for call in fake.calls if "commit" in call.args]
    assert len(commit_calls) == 1


@pytest.mark.unit
async def test_post_agent_commit_semantic_agent_repair_retry_failure_remains_visible(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed_ready(factory)
    fake.queue_result(returncode=0, stdout="adapter ok")  # agent
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="fix_test.py\n")  # cached diff
    fake.queue_result(
        returncode=1,
        stdout=_precommit_ruff_check_and_format_output("fix_test.py"),
    )  # semantic pre-commit failure
    fake.queue_result(returncode=0, stdout="repair ok")  # targeted repair succeeds
    fake.queue_result(returncode=0)  # git add -A after repair
    fake.queue_result(returncode=0, stdout="src/awf/foo.py\n")  # cached diff after repair
    fake.queue_result(returncode=1, stdout=_precommit_mypy_output())  # retry still fails

    executor = _make_executor(fake, factory, tmp_path)
    await executor.execute(ws_id)

    event = await _failed_state_event(factory, ws_id)
    assert event.reason_code == POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE
    assert event.payload is not None
    details = event.payload["details"]["post_agent_commit"]
    assert details["repair_strategy"] == "agent"
    assert details["precommit_repair_attempted"] is True
    assert "awf-mypy" in details["failed_hooks"]

    async with factory() as s:
        repair_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type=POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE,
        )
    assert repair_events
    assert repair_events[-1].reason_code == POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE
    assert repair_events[-1].payload["retry_outcome"] == "failed"  # type: ignore[index]


@pytest.mark.unit
async def test_post_agent_commit_semantic_agent_repair_records_final_deterministic_cascade(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed_ready(factory)
    fake.queue_result(returncode=0, stdout="adapter ok")  # agent
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="fix_test.py\n")  # cached diff
    fake.queue_result(
        returncode=1,
        stdout=_precommit_ruff_check_and_format_output("fix_test.py"),
    )  # semantic pre-commit failure
    fake.queue_result(returncode=0, stdout="repair ok")  # targeted repair succeeds
    fake.queue_result(returncode=0)  # git add -A after repair
    fake.queue_result(
        returncode=0,
        stdout="src/awf/foo.py\ndocs/awf-plans/ws_89.conformance.json\n",
    )
    fake.queue_result(
        returncode=1,
        stdout=_precommit_eof_only_output("docs/awf-plans/ws_89.conformance.json"),
    )  # retry now only needs deterministic EOF repair
    fake.queue_result(returncode=0)  # git add -- deterministic repair paths
    fake.queue_result(returncode=0)  # final commit retry ok
    fake.queue_result(returncode=0, stdout="0\n")  # rev-list count = 0

    executor = _make_executor(fake, factory, tmp_path, validation=_RecordingValidation())
    await executor.execute(ws_id)

    async with factory() as s:
        repair_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type=POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE,
        )

    assert len(repair_events) == 2
    payloads = [event.payload for event in repair_events if isinstance(event.payload, dict)]
    agent_payload = next(
        (payload for payload in payloads if payload.get("repair_strategy") == "agent"),
        None,
    )
    deterministic_payload = next(
        (payload for payload in payloads if payload.get("repair_strategy") == "deterministic"),
        None,
    )
    assert agent_payload is not None
    assert deterministic_payload is not None
    assert agent_payload["repair_strategy"] == "agent"
    assert agent_payload["retry_outcome"] == "failed"
    assert agent_payload["failed_hooks"] == [
        "end-of-file-fixer",
        "awf-ruff-check",
        "awf-ruff-format-check",
    ]
    assert agent_payload["restaged_paths"] == [
        "src/awf/foo.py",
        "docs/awf-plans/ws_89.conformance.json",
    ]
    assert deterministic_payload["repair_strategy"] == "deterministic"
    assert deterministic_payload["retry_outcome"] == "succeeded"


@pytest.mark.unit
async def test_post_agent_commit_format_repair_ruff_subprocess_failure_marks_repair_failed(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed_ready(factory)
    fake.queue_result(returncode=0, stdout="adapter ok")  # agent
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="src/foo.py\n")  # cached diff
    fake.queue_result(
        returncode=1, stdout=_precommit_format_only_output("src/foo.py")
    )  # initial commit fails (format only)
    fake.queue_result(
        returncode=127, stderr="uv: command not found"
    )  # ruff format subprocess itself crashes

    executor = _make_executor(fake, factory, tmp_path)
    await executor.execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"
        repair_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type=POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE,
        )

    # The repair event must record the subprocess crash so dashboards can
    # distinguish it from the skipped/succeeded/failed outcomes.
    assert len(repair_events) == 1
    repair_payload = repair_events[0].payload
    assert isinstance(repair_payload, dict)
    assert repair_payload["repaired_paths"] == ["src/foo.py"]
    assert repair_payload["retry_outcome"] == "error"
    # The repair event itself must surface the dedicated repair-failed
    # reason code — sharing POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED would
    # make the event stream indistinguishable from the original
    # rewrite-needed classification.
    assert repair_events[0].reason_code == POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE

    event = await _failed_state_event(factory, ws_id)
    # The terminal reason code MUST be the dedicated repair-failed code,
    # not POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED (whose catalog entry
    # describes only the empty-intersection skip case).
    assert event.reason_code == POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE
    assert event.payload is not None
    assert event.payload["reason_code"] == POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE
    commit_details = event.payload["details"]["post_agent_commit"]
    assert commit_details["stage"] == "ruff format"
    assert commit_details["reason_code"] == POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE
    assert commit_details["format_repair_attempted"] is True

    # No retry commit was attempted — ruff format failed before the
    # second commit could run.
    commit_calls = [call for call in fake.calls if "commit" in call.args]
    assert len(commit_calls) == 1


@pytest.mark.unit
async def test_post_agent_commit_format_repair_re_stage_failure_emits_repair_event(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """``ruff format`` succeeds but the re-stage ``git add`` fails.

    The repair attempt must still be recorded as a
    ``workspace.post_agent_commit_format_repair`` event so the event
    stream is consistent with
    ``details["post_agent_commit"]["format_repair_attempted"]`` —
    otherwise the attempt dies silently between ruff and the retry
    commit and dashboards see no record of what happened.
    """
    ws_id = await _seed_ready(factory)
    fake.queue_result(returncode=0, stdout="adapter ok")  # agent
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="src/foo.py\n")  # cached diff
    fake.queue_result(
        returncode=1, stdout=_precommit_format_only_output("src/foo.py")
    )  # initial commit fails (format only)
    fake.queue_result(returncode=0)  # ruff format src/foo.py succeeds
    fake.queue_result(
        returncode=128, stderr="fatal: not a git repository"
    )  # git add -- src/foo.py FAILS

    executor = _make_executor(fake, factory, tmp_path)
    await executor.execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        repair_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type=POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE,
        )

    # Exactly one repair event with ``retry_outcome="error"`` — the
    # re-stage sub-step failed before the retry commit could run.
    assert len(repair_events) == 1
    repair_payload = repair_events[0].payload
    assert isinstance(repair_payload, dict)
    assert repair_payload["repaired_paths"] == ["src/foo.py"]
    assert repair_payload["retry_outcome"] == "error"
    # The repair event itself must surface the dedicated repair-failed
    # reason code so dashboards can distinguish a re-stage failure from
    # the original rewrite-needed classification.
    assert repair_events[0].reason_code == POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE

    event = await _failed_state_event(factory, ws_id)
    # The terminal reason code MUST be the dedicated repair-failed code,
    # not POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED — the re-stage step is
    # part of the repair pipeline once ``format_repair_attempted=True``.
    assert event.reason_code == POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE
    assert event.payload is not None
    assert event.payload["reason_code"] == POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE
    commit_details = event.payload["details"]["post_agent_commit"]
    assert commit_details["stage"] == "git add"
    assert commit_details["reason_code"] == POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE
    # ``format_repair_attempted`` agrees with the emitted event.
    assert commit_details["format_repair_attempted"] is True

    # No retry commit was attempted — the re-stage failed before the
    # second commit could run.
    commit_calls = [call for call in fake.calls if "commit" in call.args]
    assert len(commit_calls) == 1


@pytest.mark.unit
async def test_post_agent_commit_format_only_skips_files_outside_diff(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed_ready(factory)
    fake.queue_result(returncode=0, stdout="adapter ok")  # agent
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="src/foo.py\n")  # cached diff
    fake.queue_result(
        returncode=1, stdout=_precommit_format_only_output("legacy/untouched.py")
    )  # commit fails; reformat path is outside the staged diff

    executor = _make_executor(fake, factory, tmp_path)
    await executor.execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        repair_events = await WorkspaceEventRepository(s).list(
            workspace_id=ws_id,
            event_type=POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE,
        )

    assert len(repair_events) == 1
    repair_payload = repair_events[0].payload
    assert isinstance(repair_payload, dict)
    assert repair_payload["repaired_paths"] == []
    assert repair_payload["retry_outcome"] == "skipped"

    event = await _failed_state_event(factory, ws_id)
    assert event.reason_code == POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE
    # A "skipped" repair event was emitted, so the failed-state payload must
    # agree — both signals describe the same attempt.
    assert event.payload is not None
    assert event.payload["details"]["post_agent_commit"]["format_repair_attempted"] is True

    # No ruff format invocation should have happened.
    ruff_calls = [call for call in fake.calls if "ruff" in call.args]
    assert not ruff_calls
    # No retry commit either.
    commit_calls = [call for call in fake.calls if "commit" in call.args]
    assert len(commit_calls) == 1


@pytest.mark.unit
async def test_post_agent_commit_failure_preserves_agent_idle_timeout_reason(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _IdleTimeoutAdapter(adapter_base.AgentAdapter):
        runtime = AgentRuntime.codex

        @property
        def name(self) -> AgentRuntime:
            return AgentRuntime.codex

        def get_provider(self, model: str | None) -> str:
            return "openai"

        def _cli_args(self, *, model: str | None) -> list[str]:
            return []

        async def run(self, *, prompt: str, **kwargs: Any) -> adapter_base.AgentRunResult:
            del prompt, kwargs
            raise adapter_base.AgentRunError(
                agent=self.name,
                result=CommandResult(
                    returncode=124,
                    stdout="",
                    stderr="idle timeout exceeded after 600s",
                ),
                reason_code="AGENT_IDLE_TIMEOUT",
                details={"provider": "openai", "model": "gpt-5"},
            )

    monkeypatch.setitem(adapter_base._REGISTRY, AgentRuntime.codex, _IdleTimeoutAdapter)

    ws_id = await _seed_ready(factory)
    # Agent adapter raises directly; no adapter command is consumed.
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="src/foo.py\n")  # cached diff
    fake.queue_result(returncode=1, stdout=_precommit_mypy_output())  # commit fails

    executor = _make_executor(fake, factory, tmp_path)
    await executor.execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        # The agent's classification wins: the workspace mirrors the
        # no-commit agent-failure path rather than mis-classifying the
        # upstream agent timeout as infrastructure.
        assert ws.failure_reason == "agent_failure"

    event = await _failed_state_event(factory, ws_id)
    # The agent's original reason wins on the terminal event.
    assert event.reason_code == "AGENT_IDLE_TIMEOUT"
    assert event.payload is not None
    assert event.payload["reason_code"] == "AGENT_IDLE_TIMEOUT"
    details = event.payload["details"]
    assert isinstance(details, dict)
    # The commit-step diagnostics are nested for observability but do
    # NOT overwrite the agent's reason.
    commit_details = details["post_agent_commit"]
    assert commit_details["reason_code"] == POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE
    assert commit_details["repair_strategy"] == "agent_skipped"
    assert commit_details["precommit_repair_attempted"] is False
    assert "awf-mypy" in commit_details["failed_hooks"]


@pytest.mark.unit
async def test_post_agent_git_add_failure_uses_git_add_reason_code(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws_id = await _seed_ready(factory)
    fake.queue_result(returncode=0, stdout="adapter ok")  # agent
    fake.queue_result(returncode=0, stdout="awf/x\n")  # drift check
    fake.queue_result(returncode=128, stderr="fatal: not a git repository")  # git add -A FAILS

    executor = _make_executor(fake, factory, tmp_path)
    await executor.execute(ws_id)

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"

    event = await _failed_state_event(factory, ws_id)
    assert event.reason_code == POST_AGENT_GIT_ADD_FAILED_REASON_CODE

    # No commit was attempted, no repair either.
    commit_calls = [call for call in fake.calls if "commit" in call.args]
    assert not commit_calls
    ruff_calls = [call for call in fake.calls if "ruff" in call.args]
    assert not ruff_calls
