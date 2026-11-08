"""PR monitor pre-push validation must run in the agent's git work tree (#1182).

The control plane runs as root and several monitor steps rewrite worktree git
metadata (``.git`` gitfile, ``commondir``) from Python. The agent container runs
profile validation commands as uid 1000 with no ``safe.directory`` override, so
git's ownership check treats a root-owned worktree as "not a repository" and
``git diff --check`` silently falls into ``--no-index`` mode (exit 129). Initial
validation repairs agent ownership after every root git step; pre-push
validation must do the same before handing the tree to the validation runner.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import AsyncioSubprocessRunner, FakeCommandRunner
from awf.db.session import make_session_factory
from awf.node.git_manager import git_env_without_object_lookup_overrides
from awf.runtime.ownership import AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
from awf.runtime.pr_monitor_runner import pre_push_validation as pre_push_validation_module
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.validation import profile_phase_command_plan
from awf.runtime.validation_types import ValidationCommandResult, ValidationResult
from awf.runtime.validation_worktree_types import ValidationWorktreeCheck
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime._pre_push_validation_helpers import (
    _FakeValidation,
    _set_resolved_profile,
    _validation_result,
    _validation_runs,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a scoped async SQLAlchemy session factory for tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _git_env() -> dict[str, str]:
    """Return a git env that ignores host config (including ``safe.directory``)."""
    env = git_env_without_object_lookup_overrides()
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def _git(*args: str) -> None:
    """Run a fixture git command with host config and object overrides stripped."""
    subprocess.run(["git", *args], check=True, capture_output=True, env=_git_env())


def _linked_worktree(tmp_path: Path, workspace_id: str) -> Path:
    """Create a real mirror-like repo plus a linked worktree with one clean commit."""
    repo = tmp_path / "mirror"
    _git("init", "-q", str(repo))
    _git("-C", str(repo), "config", "user.email", "agent@example.com")
    _git("-C", str(repo), "config", "user.name", "AWF Agent")
    (repo / "README.md").write_text("clean\n", encoding="utf-8")
    _git("-C", str(repo), "add", "README.md")
    _git("-C", str(repo), "commit", "-q", "-m", "init")
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.parent.mkdir(parents=True)
    _git("-C", str(repo), "worktree", "add", "-q", str(worktree))
    return worktree


class _AgentContainerValidation(_FakeValidation):
    """Execute profile commands like the uid-1000 agent container would.

    The real runner execs ``sh -lc <command>`` in the agent container's
    ``/workspace`` without a ``safe.directory`` override. Git's own
    ``GIT_TEST_ASSUME_DIFFERENT_OWNER`` knob models the ownership check failing
    until the control plane has repaired agent ownership for the worktree.
    """

    def __init__(self, repaired: set[Path], order: list[str]) -> None:
        """Track which worktrees have had agent ownership repaired."""
        super().__init__()
        self.repaired = repaired
        self.order = order

    async def run_profile_phases(self, **kwargs: Any) -> ValidationResult:
        """Run each profile command in the worktree and record its real exit code."""
        self.calls.append(dict(kwargs))
        self.order.append("validation")
        worktree = Path(kwargs["worktree_path"])
        env = _git_env()
        if worktree not in self.repaired:
            env["GIT_TEST_ASSUME_DIFFERENT_OWNER"] = "1"
        commands: list[ValidationCommandResult] = []
        plan = profile_phase_command_plan(kwargs["profile"], kwargs["phase_names"])
        for index, step in enumerate(plan):
            command = step.command.command
            completed = subprocess.run(
                ["sh", "-c", command],
                cwd=worktree,
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            stdout_path = worktree.parent / f"validation-{index}.stdout"
            stderr_path = worktree.parent / f"validation-{index}.stderr"
            stdout_path.write_text(completed.stdout, encoding="utf-8")
            stderr_path.write_text(completed.stderr, encoding="utf-8")
            commands.append(
                ValidationCommandResult(
                    command=command,
                    returncode=completed.returncode,
                    duration_seconds=0.1,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    phase="validate",
                    reason_code="VALIDATION_OK" if completed.returncode == 0 else "COMMAND_FAILED",
                )
            )
        return ValidationResult(commands=commands)


def _record_ownership_repair(
    monkeypatch: pytest.MonkeyPatch,
    *,
    repaired: set[Path],
    order: list[str],
    calls: list[dict[str, object]],
    ok: bool = True,
) -> None:
    """Stand in for the root chown, which a non-root test cannot perform."""

    async def _repair(**kwargs: Any) -> bool:
        calls.append(dict(kwargs))
        order.append("ownership_repair")
        if ok:
            repaired.add(Path(kwargs["worktree_path"]))
        return ok

    monkeypatch.setattr(pre_push_validation_module, "repair_agent_runtime_ownership", _repair)


def _runner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    real_git: bool = True,
) -> tuple[Any, list[str]]:
    """Build a runner whose control-plane git runs for real and whose push is recorded."""
    cmd: Any = AsyncioSubprocessRunner()
    if not real_git:
        cmd = FakeCommandRunner()
        cmd.queue_result(returncode=0, stdout=f"{'f' * 40}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=0,
    )
    pushes: list[str] = []

    async def _git_push_result(**kwargs: Any) -> _GitPushResult:
        """Record the push instead of contacting a remote."""
        pushes.append(str(kwargs["remote_branch"]))
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    runner._git_push_result = _git_push_result
    return runner, pushes


@pytest.mark.unit
async def test_pre_push_validation_git_dependent_command_runs_in_repaired_git_context(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``git diff --check`` must see the work tree, not fall into no-index mode (rc 129)."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id, validate_commands=["git diff --check"])
    worktree = _linked_worktree(tmp_path, workspace_id)
    repaired: set[Path] = set()
    order: list[str] = []
    repair_calls: list[dict[str, object]] = []
    _record_ownership_repair(monkeypatch, repaired=repaired, order=order, calls=repair_calls)
    runner, pushes = _runner(factory, tmp_path)
    runner._deps.validation = _AgentContainerValidation(repaired, order)

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False, result.details
    assert result.pushed is True
    assert pushes == [f"awf/{workspace_id}"]
    assert order == ["ownership_repair", "validation"]
    assert repair_calls[0]["reason"] == "pre_push_validation"
    runs = await _validation_runs(factory, workspace_id)
    assert runs[-1].status == "succeeded"


@pytest.mark.unit
async def test_pre_push_validation_repairs_ownership_after_root_git_steps(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Root-side worktree check/finalize/residue cleanup all precede the ownership repair."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    order: list[str] = []
    dirty = ValidationWorktreeCheck(clean=False, paths=("residue.txt",))
    clean = ValidationWorktreeCheck(clean=True)

    async def _worktree_check(_self: Any, **_kwargs: object) -> ValidationWorktreeCheck:
        """Report residue so finalize and residue cleanup both run."""
        order.append("worktree_check")
        return dirty

    async def _finalize(_self: Any, **_kwargs: object) -> None:
        """Decline to finalize so residue cleanup runs next."""
        order.append("dirty_finalize")

    async def _residue(_self: Any, **_kwargs: object) -> tuple[ValidationWorktreeCheck, str]:
        """Clean the residue as the root-side restore/clean would."""
        order.append("residue_cleanup")
        return clean, "f" * 40

    monkeypatch.setattr(
        pre_push_validation_module, "_pre_push_validation_worktree_check", _worktree_check
    )
    monkeypatch.setattr(
        pre_push_validation_module, "_try_finalize_pre_push_dirty_repair_state", _finalize
    )
    monkeypatch.setattr(
        pre_push_validation_module, "_try_cleanup_pre_push_post_operation_residue", _residue
    )
    _record_ownership_repair(monkeypatch, repaired=set(), order=order, calls=[])

    class _OrderedValidation(_FakeValidation):
        """Record when the validation runner is invoked."""

        async def run_profile_phases(self, **kwargs: object) -> ValidationResult:
            """Record the call, then return the queued result."""
            order.append("validation")
            return await super().run_profile_phases(**kwargs)

    runner, _pushes = _runner(factory, tmp_path, real_git=False)
    runner._deps.validation = _OrderedValidation(_validation_result(tmp_path, ok=False))

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
    )

    assert result.passed is False
    assert order[:5] == [
        "worktree_check",
        "dirty_finalize",
        "residue_cleanup",
        "ownership_repair",
        "validation",
    ]


@pytest.mark.unit
async def test_pre_push_validation_ownership_repair_failure_skips_validation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed ownership repair fails closed instead of validating in a wrong git context."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id, validate_commands=["git diff --check"])
    worktree = _linked_worktree(tmp_path, workspace_id)
    order: list[str] = []
    _record_ownership_repair(monkeypatch, repaired=set(), order=order, calls=[], ok=False)
    runner, pushes = _runner(factory, tmp_path)
    validation = _AgentContainerValidation(set(), order)
    runner._deps.validation = validation
    runs_before = await _validation_runs(factory, workspace_id)

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.pushed is False
    assert result.reason_code == AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
    assert result.terminal_monitor_failure is True
    assert validation.calls == []
    assert order == ["ownership_repair"]
    assert len(await _validation_runs(factory, workspace_id)) == len(runs_before)
    assert pushes == []
