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
    POST_AGENT_COMMIT_FAILED_REASON_CODE,
    POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE,
    POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE,
    POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE,
    POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
    POST_AGENT_GIT_ADD_FAILED_REASON_CODE,
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceEventRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_creator import PullRequestCreator
from tests.postgres import postgres_test_engine

from .test_executor_error_paths import (
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
    assert "awf-mypy" in commit_details["failed_hooks"]

    # Only one git commit was queued/consumed — no repair retry happened.
    commit_calls = [call for call in fake.calls if "commit" in call.args]
    assert len(commit_calls) == 1


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

    event = await _failed_state_event(factory, ws_id)
    assert event.reason_code == POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE
    assert event.payload is not None
    assert event.payload["details"]["post_agent_commit"]["format_repair_attempted"] is True


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
