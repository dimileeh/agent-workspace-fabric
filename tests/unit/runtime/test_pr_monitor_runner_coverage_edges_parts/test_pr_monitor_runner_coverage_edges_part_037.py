"""Continuation coverage tests for PR monitor runner CI-fix salvage/rollback edges."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunError
from awf.adapters.provider_failures import AGENT_PROVIDER_CAPACITY_EXHAUSTED
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.enums import AgentRuntime
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import CheckFailure
from awf.runtime.pr_monitor_runner import ci_ops as pr_ci_ops
from awf.runtime.pr_monitor_runner import remote_repair as pr_remote_repair
from awf.runtime.pr_monitor_runner.types import ProviderRecoveryRetryError
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Create an isolated async session factory for CI-fix edge tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _patch_ci_repair_salvage_success(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tmp_path: Path,
    operation_start_head: str,
) -> None:
    """Mock successful CI-repair salvage for provider-recovery re-raise tests."""

    async def _mock_salvage_success(self: object, **kwargs: object) -> dict[str, object]:
        del self, kwargs
        return {
            "repair_salvage": {
                "patch_path": str(tmp_path / "artifacts/salvage/ws.patch"),
                "patch_sha256": "a" * 64,
                "patch_bytes": 10,
                "affected_paths": ["src/fix.py"],
                "phase": "ci_repair_commit_sink",
                "operation_type": "ci_repair",
                "operation_id": None,
                "operation_start_head": operation_start_head,
                "created_at": "2026-07-02T00:00:00+00:00",
            }
        }

    monkeypatch.setattr(pr_ci_ops, "_salvage_ci_repair_dirty_output", _mock_salvage_success)


@pytest.mark.unit
async def test_ci_fix_commit_sink_provider_recovery_attaches_salvage_metadata(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for PRRT_kwDOSJAM6s6N7A9i: direct commit-sink provider recovery.

    When ``_commit_dirty_worktree`` raises a provider-recovery control-flow
    exception, salvage metadata must be attached before re-raise so finished
    monitor operations include ``repair_salvage`` like the stranded path.
    """
    from unittest.mock import AsyncMock

    from awf.runtime.pr_monitor_runner import types as monitor_types

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    operation_start_head = "abc1234567890def"
    expected_stderr = "MODEL_CAPACITY_EXHAUSTED"
    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.codex,
            result=CommandResult(
                returncode=1,
                stdout="partial fix written\n",
                stderr=expected_stderr,
            ),
            reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
            details={"provider": "openai", "model": "gpt-5.3-codex-spark"},
        )
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # pre-existing dirty guard
    cmd.queue_result(returncode=0, stdout=f"{operation_start_head}\n")  # op start HEAD
    cmd.queue_result(returncode=0, stdout=f"{operation_start_head}\n")  # post-raise HEAD
    cmd.queue_result(returncode=0)  # rollback: git reset --hard
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=tmp_path / "artifacts",
    )

    async def _repair_agent_runtime_ownership(
        logger: object,
        workspace_id: str,
        worktree_path: Path,
        reason: str,
        event_name: str,
        reason_code: str,
    ) -> bool:
        del logger, workspace_id, worktree_path, event_name, reason_code
        return True

    monkeypatch.setattr(
        pr_remote_repair,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    repair_salvage = {
        "patch_path": str(tmp_path / "artifacts/salvage/ws.patch"),
        "patch_sha256": "b" * 64,
        "patch_bytes": 10,
        "affected_paths": ["src/fix.py"],
        "phase": "ci_repair_commit_sink",
        "operation_type": "ci_repair",
        "operation_id": None,
        "operation_start_head": operation_start_head,
        "created_at": "2026-07-02T00:00:00+00:00",
    }

    async def _mock_salvage_success(self: object, **kwargs: object) -> dict[str, object]:
        del self, kwargs
        return {"repair_salvage": repair_salvage}

    monkeypatch.setattr(pr_ci_ops, "_salvage_ci_repair_dirty_output", _mock_salvage_success)

    raised_exc = monitor_types.ProviderRecoveryRetryError(
        "provider recovery raised inside the CI fix commit sink"
    )
    monkeypatch.setattr(runner, "_commit_dirty_worktree", AsyncMock(side_effect=raised_exc))

    with pytest.raises(ProviderRecoveryRetryError) as exc_info:
        await runner._run_ci_fix(
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            failures=(
                CheckFailure(name="test", conclusion="FAILURE", log_excerpt="pytest failed"),
            ),
            compose_project=f"awf_{workspace_id}",
            compose_file=tmp_path / "compose.yml",
            workspace_id=workspace_id,
            remote_branch=f"awf/{workspace_id}",
        )

    assert exc_info.value.details is not None
    assert exc_info.value.details["repair_salvage"] == repair_salvage
    assert exc_info.value.details["phase"] == "ci_repair_commit_sink"
    assert exc_info.value.details["provider_error_stderr"] == expected_stderr


@pytest.mark.unit
async def test_ci_fix_commit_sink_provider_recovery_cleans_untracked_residue_before_re_raise(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for PRRT_kwDOSJAM6s6Khuvf — untracked residue must be cleaned.

    ``git reset --hard`` only resets HEAD/index/tracked working-tree files; it
    does NOT remove untracked files. The protected-scope repair agent (or the
    CI-repair agent) can leave untracked repair output behind, and
    ``_pre_existing_dirty_repair_worktree_result`` (which enumerates untracked
    paths via ``--untracked-files=all``) treats untracked files as dirty, so
    the next monitor cycle trips ``PRE_EXISTING_DIRTY_WORKTREE`` instead of
    retrying the provider recovery. The rollback must therefore also clean
    untracked residue — mirroring the fix-pass residue rollback
    (``PRRT_kwDOSJAM6s6Kc_Ak``) and the finalize residue rollback
    (``PRRT_kwDOSJAM6s6KewGH``), both of which run ``_pre_push_validation_cleanup``
    (which invokes ``git clean -ffd`` for non-ignored untracked paths).
    """
    from unittest.mock import AsyncMock

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    # Mark the worktree as a git worktree so ``check_validation_worktree_clean``
    # (invoked inside ``_pre_push_validation_cleanup``) does not short-circuit
    # to ``skipped`` and actually drives the cleanup git commands.
    (worktree).mkdir(parents=True, exist_ok=True)
    (worktree / ".git").write_text("gitdir: /tmp/fake.git\n", encoding="utf-8")
    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.codex,
            result=CommandResult(
                returncode=1,
                stdout="partial fix written\n",
                stderr="MODEL_CAPACITY_EXHAUSTED",
            ),
            reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
            details={"provider": "openai", "model": "gpt-5.3-codex-spark"},
        )
    )
    operation_start_head = "abc1234567890abcdef1234567890abcdef"
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # pre-existing dirty guard
    cmd.queue_result(returncode=0, stdout=f"{operation_start_head}\n")  # op start HEAD
    # post-raise HEAD (captured inside the provider-recovery ``except`` clause
    # AFTER the sink raised; the agent did not self-commit in the sink, so it
    # equals the operation-start HEAD).
    cmd.queue_result(returncode=0, stdout=f"{operation_start_head}\n")
    # rollback: git reset --hard <post_raise_head>
    cmd.queue_result(returncode=0)
    # ``_pre_push_validation_cleanup`` -> ``check_validation_worktree_clean``:
    # the protected-scope repair agent left an untracked residue file behind.
    cmd.queue_result(returncode=0, stdout="?? src/generated_repair.py\n")
    # ``git clean -ffd -- src/generated_repair.py`` removes the untracked residue.
    cmd.queue_result(returncode=0)
    # post-clean verify status
    cmd.queue_result(returncode=0, stdout="")
    # HEAD verification: ``rev-parse <restore_ref>`` + ``rev-parse HEAD``.
    cmd.queue_result(returncode=0, stdout=f"{operation_start_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{operation_start_head}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _repair_agent_runtime_ownership(
        logger: object,
        workspace_id: str,
        worktree_path: Path,
        reason: str,
        event_name: str,
        reason_code: str,
    ) -> bool:
        del logger, workspace_id, worktree_path, event_name, reason_code
        return True

    monkeypatch.setattr(
        pr_remote_repair,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    _patch_ci_repair_salvage_success(
        monkeypatch,
        tmp_path=tmp_path,
        operation_start_head=operation_start_head,
    )

    raised_exc = ProviderRecoveryRetryError(
        "provider recovery raised inside the CI fix commit sink"
    )
    monkeypatch.setattr(runner, "_commit_dirty_worktree", AsyncMock(side_effect=raised_exc))

    push_result = await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="test", conclusion="FAILURE", log_excerpt="pytest failed"),),
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
    )

    # The rollback MUST remove untracked residue via ``git clean -ffd`` (or the
    # equivalent validation cleanup path) before finishing. In this lightweight
    # test double the follow-on cleanup verification may still fail, which now
    # stays terminal instead of re-raising provider recovery
    # (PRRT_kwDOSJAM6s6N8a5t).
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any("clean" in call and "-ffd" in call for call in joined_calls), joined_calls
    if push_result.reason_code == "REPAIR_DIRTY_COMMIT_FAILED":
        assert push_result.details is not None
        assert push_result.details["rollback_error"]["cause"] == "cleanup_failed"
    else:
        raise AssertionError(f"unexpected push result: {push_result!r}")


@pytest.mark.unit
async def test_ci_fix_commit_sink_provider_recovery_rolls_back_to_post_agent_head_not_operation_start_head(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for PRRT_kwDOSJAM6s6Klf74 — preserve self-committed CI fixes.

    When the CI-repair agent advances HEAD itself (commits its own CI fix) and
    then leaves additional dirty protected-scope residue, ``_commit_dirty_worktree``
    can raise a provider-recovery control-flow exception (from
    ``_repair_protected_scope_changes_before_commit``) BEFORE making its own
    commit. Rolling back to ``operation_start_head`` would drop the agent's
    already-committed CI fix along with the residue, so the provider retry
    starts from the old tree and may lose or redo valid repair work. The
    rollback must anchor against the post-agent/pre-sink HEAD instead,
    mirroring the fix-pass (``fix_start_head``) and finalize
    (``finalize_start_head``) rollbacks.
    """
    from unittest.mock import AsyncMock

    from awf.runtime.pr_monitor_runner import types as monitor_types

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.codex,
            result=CommandResult(
                returncode=1,
                stdout="partial fix written\n",
                stderr="MODEL_CAPACITY_EXHAUSTED",
            ),
            reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
            details={"provider": "openai", "model": "gpt-5.3-codex-spark"},
        )
    )
    operation_start_head = "abc1234567890def"
    # The CI-repair agent committed its own CI fix and advanced HEAD past the
    # operation-start HEAD before the commit sink ran. The rollback must
    # preserve this commit, not discard it back to ``operation_start_head``.
    agent_commit_head = "fedcba0987654321"
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # pre-existing dirty guard
    cmd.queue_result(returncode=0, stdout=f"{operation_start_head}\n")  # op start HEAD
    # post-raise HEAD (captured inside the provider-recovery ``except`` clause
    # AFTER the sink raised; the commit sink raised before making its own
    # commit, so HEAD has not moved since the agent's self-commit and the
    # post-raise HEAD equals the agent's committed HEAD).
    cmd.queue_result(returncode=0, stdout=f"{agent_commit_head}\n")
    # rollback: git reset --hard <post_raise_head> (NOT operation_start_head)
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _repair_agent_runtime_ownership(
        logger: object,
        workspace_id: str,
        worktree_path: Path,
        reason: str,
        event_name: str,
        reason_code: str,
    ) -> bool:
        del logger, workspace_id, worktree_path, event_name, reason_code
        return True

    monkeypatch.setattr(
        pr_remote_repair,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    _patch_ci_repair_salvage_success(
        monkeypatch,
        tmp_path=tmp_path,
        operation_start_head=operation_start_head,
    )

    raised_exc = monitor_types.ProviderRecoveryRetryError(
        "provider recovery raised inside the CI fix commit sink"
    )
    # The commit sink itself raises the provider-recovery exception before
    # making its own commit (e.g. from ``_repair_protected_scope_changes_before_commit``
    # -> ``_handle_provider_agent_run_error``). The agent's already-committed
    # CI fix MUST be preserved by anchoring the rollback against the
    # post-agent HEAD.
    monkeypatch.setattr(runner, "_commit_dirty_worktree", AsyncMock(side_effect=raised_exc))

    with pytest.raises(ProviderRecoveryRetryError):
        await runner._run_ci_fix(
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            failures=(
                CheckFailure(name="test", conclusion="FAILURE", log_excerpt="pytest failed"),
            ),
            compose_project=f"awf_{workspace_id}",
            compose_file=tmp_path / "compose.yml",
            workspace_id=workspace_id,
            remote_branch=f"awf/{workspace_id}",
        )

    # The rollback MUST reset to the post-raise HEAD
    # (``agent_commit_head``), NOT ``operation_start_head`` — preserving the
    # CI-repair agent's already-committed fix so the provider retry starts
    # from the agent-advanced tree instead of redoing or losing valid work.
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(
        "reset" in call and "--hard" in call and agent_commit_head in call for call in joined_calls
    ), joined_calls
    assert not any(
        "reset" in call and "--hard" in call and operation_start_head in call
        for call in joined_calls
    ), joined_calls


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc_cls_name",
    [
        "ProviderRecoveryRetryError",
        "ProviderRecoveryFallbackError",
        "ProviderRecoveryAuthError",
    ],
)
async def test_ci_fix_commit_sink_provider_recovery_rolls_back_to_post_raise_head_not_pre_sink_head(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc_cls_name: str,
) -> None:
    """Regression for PRRT_kwDOSJAM6s6KpAD6 — preserve in-sink protected-scope self-commits.

    The protected-scope repair agent runs INSIDE ``_commit_dirty_worktree`` (via
    ``_repair_protected_scope_changes_before_commit``) and may self-commit,
    advancing HEAD past the pre-sink HEAD snapshot BEFORE the commit sink raises
    a provider-recovery control-flow exception. Capturing the rollback anchor
    BEFORE the sink (a pre-try ``post_agent_head``) is stale against that in-sink
    self-commit: ``git reset --hard <pre_sink_head>`` would drop the valid
    protected-scope repair self-commit, so the provider retry starts from the old
    tree and loses or redoes valid repair work.

    The rollback must anchor against the HEAD captured AFTER the sink raised
    (inside the provider-recovery ``except`` block), mirroring the dirty-finalize
    path (``_rollback_finalize_dirty_residue_before_provider_recovery``,
    regression ``PRRT_kwDOSJAM6s6KnWkn``), which already established the
    post-raise anchoring contract for in-sink self-commits. The pre-try capture
    is therefore removed from ``_run_ci_fix``; the anchor is resolved inside
    the ``except`` clause after the sink raised.

    This test simulates the in-sink self-commit by advancing a mutable HEAD cell
    inside the mocked ``_commit_dirty_worktree`` side effect BEFORE it raises, so
    the pre-try capture (buggy code) sees the stale pre-sink HEAD while the
    post-raise capture (fixed code) sees the advanced HEAD.
    """
    from unittest.mock import AsyncMock

    from awf.runtime.pr_monitor_runner import types as monitor_types

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.codex,
            result=CommandResult(
                returncode=1,
                stdout="partial fix written\n",
                stderr="MODEL_CAPACITY_EXHAUSTED",
            ),
            reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
            details={"provider": "openai", "model": "gpt-5.3-codex-spark"},
        )
    )
    operation_start_head = "abc1234567890def"
    # The CI-repair agent did NOT self-commit, so the pre-sink HEAD still equals
    # ``operation_start_head``. The protected-scope repair agent INSIDE the
    # commit sink then self-commits and advances HEAD to
    # ``in_sink_self_commit_head`` BEFORE the sink raises. The rollback must
    # anchor against the post-raise HEAD (``in_sink_self_commit_head``), NOT the
    # stale pre-sink HEAD (``operation_start_head``) — otherwise the in-sink
    # self-commit is dropped.
    in_sink_self_commit_head = "1111122222333344"
    head_cell: dict[str, str] = {"sha": operation_start_head}

    async def _fake_rev_parse_head(worktree_path_arg: Path) -> str | None:
        del worktree_path_arg
        return head_cell["sha"]

    async def _commit_sink_side_effect(*_args: object, **_kwargs: object) -> bool:
        # Simulate the protected-scope repair agent self-committing inside the
        # sink and advancing HEAD BEFORE the provider-recovery exception is
        # raised (e.g. from ``_repair_protected_scope_changes_before_commit``
        # -> ``_handle_provider_agent_run_error``).
        head_cell["sha"] = in_sink_self_commit_head
        raise raised_exc

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # pre-existing dirty guard
    cmd.queue_result(returncode=0, stdout=f"{operation_start_head}\n")  # op start HEAD
    # rollback: ``git reset --hard <post_raise_head>`` (``in_sink_self_commit_head``
    # under the fixed code; the stale ``operation_start_head`` under the buggy
    # pre-try capture).
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _repair_agent_runtime_ownership(
        logger: object,
        workspace_id: str,
        worktree_path: Path,
        reason: str,
        event_name: str,
        reason_code: str,
    ) -> bool:
        del logger, workspace_id, worktree_path, event_name, reason_code
        return True

    monkeypatch.setattr(
        pr_remote_repair,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    _patch_ci_repair_salvage_success(
        monkeypatch,
        tmp_path=tmp_path,
        operation_start_head=operation_start_head,
    )
    # Replace ``_rev_parse_head`` with the mutable-cell mock so the pre-try
    # capture (buggy code) and the post-raise capture (fixed code) observe
    # different HEADs without consuming FakeCommandRunner queue slots.
    monkeypatch.setattr(runner, "_rev_parse_head", _fake_rev_parse_head)

    raised_exc = getattr(monitor_types, exc_cls_name)(
        "provider recovery raised after protected-scope repair self-committed inside the CI fix commit sink"
    )
    monkeypatch.setattr(
        runner, "_commit_dirty_worktree", AsyncMock(side_effect=_commit_sink_side_effect)
    )

    with pytest.raises(type(raised_exc)):
        await runner._run_ci_fix(
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            failures=(
                CheckFailure(name="test", conclusion="FAILURE", log_excerpt="pytest failed"),
            ),
            compose_project=f"awf_{workspace_id}",
            compose_file=tmp_path / "compose.yml",
            workspace_id=workspace_id,
            remote_branch=f"awf/{workspace_id}",
        )

    # The rollback MUST reset to the post-raise HEAD
    # (``in_sink_self_commit_head``), NOT the stale pre-sink HEAD
    # (``operation_start_head``) — preserving the protected-scope repair
    # agent's in-sink self-commit so the provider retry starts from the
    # advanced tree instead of dropping valid repair work.
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(
        "reset" in call and "--hard" in call and in_sink_self_commit_head in call
        for call in joined_calls
    ), joined_calls
    assert not any(
        "reset" in call and "--hard" in call and operation_start_head in call
        for call in joined_calls
    ), joined_calls


@pytest.mark.unit
async def test_ci_fix_commit_sink_provider_recovery_rollback_skipped_when_post_agent_head_unavailable(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for PRRT_kwDOSJAM6s6N8a5t — missing anchor stays terminal.

    If the post-raise HEAD cannot be resolved (``git rev-parse HEAD`` fails or
    returns empty inside the provider-recovery ``except`` clause), the rollback
    must be SKIPPED instead of restoring against the wrong ref
    (``operation_start_head`` or the stale pre-sink HEAD), mirroring the
    finalize rollback's ``restore_ref is None`` guard. A missing anchor makes a
    safe ``git reset --hard`` impossible — return a terminal dirty-commit result
    instead of re-raising provider recovery while residue remains stranded.
    """
    from unittest.mock import AsyncMock

    from awf.runtime.pr_monitor_runner import types as monitor_types

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.codex,
            result=CommandResult(
                returncode=1,
                stdout="partial fix written\n",
                stderr="MODEL_CAPACITY_EXHAUSTED",
            ),
            reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
            details={"provider": "openai", "model": "gpt-5.3-codex-spark"},
        )
    )
    operation_start_head = "abc1234567890def"
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # pre-existing dirty guard
    cmd.queue_result(returncode=0, stdout=f"{operation_start_head}\n")  # op start HEAD
    # post-raise HEAD resolution FAILS (rev-parse errors) -> ``_rev_parse_head``
    # returns None, so the rollback is skipped.
    cmd.queue_result(returncode=128, stderr="fatal: not a git repository\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _repair_agent_runtime_ownership(
        logger: object,
        workspace_id: str,
        worktree_path: Path,
        reason: str,
        event_name: str,
        reason_code: str,
    ) -> bool:
        del logger, workspace_id, worktree_path, event_name, reason_code
        return True

    monkeypatch.setattr(
        pr_remote_repair,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    _patch_ci_repair_salvage_success(
        monkeypatch,
        tmp_path=tmp_path,
        operation_start_head=operation_start_head,
    )

    raised_exc = monitor_types.ProviderRecoveryRetryError(
        "provider recovery raised inside the CI fix commit sink"
    )
    monkeypatch.setattr(runner, "_commit_dirty_worktree", AsyncMock(side_effect=raised_exc))

    warnings: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.ci_ops._log.warning",
        lambda event, **fields: warnings.append((event, fields)),
    )

    push_result = await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="test", conclusion="FAILURE", log_excerpt="pytest failed"),),
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
    )

    assert push_result.reason_code == "REPAIR_DIRTY_COMMIT_FAILED"
    assert push_result.details is not None
    rollback_error = push_result.details["rollback_error"]
    assert rollback_error["cause"] == "missing_anchor"

    # No ``git reset --hard`` runs — the missing anchor makes a safe restore
    # impossible, so the residue strands visibly instead of being discarded
    # against the wrong ref.
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert not any("reset" in call and "--hard" in call for call in joined_calls), joined_calls
    # The skip is logged so triage can see why the rollback was not attempted.
    assert any(
        event == "monitor.ci_fix_provider_recovery_rollback_skipped_no_anchor"
        for event, _ in warnings
    ), warnings


@pytest.mark.unit
async def test_salvage_ci_repair_dirty_output_unexpected_exception_returns_salvage_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for PRRT_kwDOSJAM6s6N6A4y — unexpected salvage failures are contained."""
    import subprocess
    from types import SimpleNamespace

    from awf.service.repair_salvage import REPAIR_SALVAGE_UNEXPECTED

    worktrees_root = tmp_path / "worktrees"
    worktrees_root.mkdir()
    artifacts_root = tmp_path / "artifacts"
    self = SimpleNamespace(_worktrees_root=worktrees_root, _artifacts_root=artifacts_root)

    def _raising_capture(**kwargs: object) -> object:
        del kwargs
        raise subprocess.TimeoutExpired(cmd=["git", "diff"], timeout=30.0)

    monkeypatch.setattr(
        "awf.service.repair_salvage.capture_ci_repair_salvage",
        _raising_capture,
    )

    result = await pr_ci_ops._salvage_ci_repair_dirty_output(
        self,
        workspace_id="ws-123",
        operation_start_head="abc1234567890def",
        operation_id=None,
        operation_type="ci_repair",
        phase="ci_repair_commit_sink",
    )

    assert result == {
        "salvage_error": {
            "reason_code": REPAIR_SALVAGE_UNEXPECTED,
            "message": "Command '['git', 'diff']' timed out after 30.0 seconds",
        },
    }


@pytest.mark.unit
async def test_ci_fix_provider_recovery_skips_rollback_when_salvage_raises_unexpectedly(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for PRRT_kwDOSJAM6s6N8a5t / PRRT_kwDOSJAM6s6N8v2q.

    Salvage failures must stay terminal without rolling back dirty repair output
    that was never captured to a patch — mirroring the stranded commit-sink path.
    """
    import subprocess
    from unittest.mock import AsyncMock

    from awf.service.repair_salvage import REPAIR_SALVAGE_UNEXPECTED

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.codex,
            result=CommandResult(
                returncode=1,
                stdout="partial fix written\n",
                stderr="MODEL_CAPACITY_EXHAUSTED",
            ),
            reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
            details={"provider": "openai", "model": "gpt-5.3-codex-spark"},
        )
    )
    operation_start_head = "abc1234567890def"
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # pre-existing dirty guard
    cmd.queue_result(returncode=0, stdout=f"{operation_start_head}\n")  # op start HEAD
    cmd.queue_result(returncode=0, stdout=f"{operation_start_head}\n")  # post-raise HEAD
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=tmp_path / "artifacts",
    )

    async def _repair_agent_runtime_ownership(
        logger: object,
        workspace_id: str,
        worktree_path: Path,
        reason: str,
        event_name: str,
        reason_code: str,
    ) -> bool:
        del logger, workspace_id, worktree_path, event_name, reason_code
        return True

    monkeypatch.setattr(
        pr_remote_repair,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    def _raising_capture(**kwargs: object) -> object:
        del kwargs
        raise subprocess.TimeoutExpired(cmd=["git", "diff"], timeout=30.0)

    monkeypatch.setattr(
        "awf.service.repair_salvage.capture_ci_repair_salvage",
        _raising_capture,
    )

    raised_exc = ProviderRecoveryRetryError(
        "provider recovery raised inside the CI fix commit sink"
    )
    monkeypatch.setattr(runner, "_commit_dirty_worktree", AsyncMock(side_effect=raised_exc))

    warnings: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.ci_ops._log.warning",
        lambda event, **fields: warnings.append((event, fields)),
    )

    push_result = await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="test", conclusion="FAILURE", log_excerpt="pytest failed"),),
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
    )

    assert push_result.reason_code == "REPAIR_DIRTY_COMMIT_FAILED"
    assert push_result.details is not None
    assert push_result.details["salvage_error"]["reason_code"] == REPAIR_SALVAGE_UNEXPECTED
    assert any(
        event == "monitor.ci_fix_dirty_commit_failed"
        and fields.get("salvage_reason_code") == REPAIR_SALVAGE_UNEXPECTED
        for event, fields in warnings
    ), warnings
    assert any(
        event == "monitor.ci_repair_salvage_failed"
        and fields.get("reason_code") == REPAIR_SALVAGE_UNEXPECTED
        for event, fields in warnings
    ), warnings
    assert any(
        event == "monitor.ci_repair_salvage_before_provider_recovery_failed"
        and fields.get("reason_code") == REPAIR_SALVAGE_UNEXPECTED
        for event, fields in warnings
    ), warnings
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert not any("reset" in call and "--hard" in call for call in joined_calls), joined_calls
    assert "rollback_error" not in (push_result.details or {})
