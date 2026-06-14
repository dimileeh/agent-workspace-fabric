"""Shared fixtures and helpers for workspace retry/requeue service tests."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.provider_failures import AGENT_IDLE_TIMEOUT
from awf.api.schemas import WorkspaceCreateRequest
from awf.common.config import Settings
from awf.db.enums import AgentRuntime, FailureReason, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    PLAN_CONFORMANCE_UNSATISFIED,
)
from awf.service.workspaces import WorkspaceService
from tests.postgres import postgres_test_engine


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a session factory backed by a disposable test database."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _request(
    *,
    task_kind: str = "feature_branch_pr",
    provider_readiness_override: bool = True,
) -> WorkspaceCreateRequest:
    payload: dict[str, object] = {
        "repo": {"url": "git@github.com:example/retryable.git", "base_branch": "development"},
        "task": {
            "title": "Retry flaky validation",
            "prompt": "Fix the intermittent validation failure.",
            "agent": "codex",
            "kind": task_kind,
            "external_id": "TICKET-RETRY",
            "task_class": "test_task",
            "owned_paths": ["src/awf/retry/**"],
            "auto_merge": False,
            "initial_review_grace_period_seconds": 30,
        },
        "workspace": {"profile_ref": "python", "profile": None},
        "validation": {"commands": ["uv run pytest tests/unit -q"], "requested_tier": 2},
        "resources": {},
    }
    if provider_readiness_override:
        payload["preflight"] = {
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "retry service test fixture",
        }
    return WorkspaceCreateRequest.model_validate(payload)


def _request_with_preflight_override(
    *,
    reason: str = "operator verified provider readiness manually",
) -> WorkspaceCreateRequest:
    payload = _request().model_dump(mode="json")
    payload["preflight"] = {
        "provider_readiness_override": True,
        "provider_readiness_override_reason": reason,
    }
    return WorkspaceCreateRequest.model_validate(payload)


def _opencode_request() -> WorkspaceCreateRequest:
    payload = _request(provider_readiness_override=False).model_dump(mode="python")
    payload["task"]["agent"] = "opencode"
    payload["task"]["model"] = "ollama/kimi-k2.6:cloud"
    return WorkspaceCreateRequest.model_validate(payload)


def _settings_with_host_home(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        host_home=str(tmp_path / "home"),
        docker_host="",
    )


def _ollama_provider_environ() -> dict[str, str]:
    return {
        "OLLAMA_API_KEY": "ollama_secret",
        "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local:11434/v1",
    }


def _docker_ok(args: list[str], **kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout="/usr/bin/cli\n", stderr="")


def _ollama_ok(url: str, *, timeout: float) -> SimpleNamespace:
    text = '{"models":[{"name":"kimi-k2.6:cloud"}]}' if url.endswith("/api/tags") else "{}"
    return SimpleNamespace(status_code=200, text=text)


def _ollama_ok_requiring_worker_thread(
    url: str,
    *,
    timeout: float,
) -> SimpleNamespace:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise AssertionError("provider preflight probe ran on the event-loop thread")
    return _ollama_ok(url, timeout=timeout)


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _settings_with_work_dir(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        work_dir=str(tmp_path / "awf-state"),
        host_home=str(tmp_path / "home"),
        docker_host="",
    )


def _create_conformance_source_worktree(
    settings: Settings,
    workspace_id: str,
    *,
    implementation_diff: bool = True,
) -> str:
    worktree = Path(settings.work_dir) / "git" / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    _git(["init", "-q"], worktree)
    _git(["config", "user.name", "AWF Test"], worktree)
    _git(["config", "user.email", "awf@test.local"], worktree)
    (worktree / "src/awf").mkdir(parents=True)
    (worktree / "src/awf/retry.py").write_text("def retry():\n    return 'old'\n")
    _git(["add", "."], worktree)
    _git(["commit", "-q", "-m", "base"], worktree)
    base_commit = _git(["rev-parse", "HEAD"], worktree)

    (worktree / "docs/awf-plans").mkdir(parents=True)
    (worktree / "docs/awf-plans/ws_old.md").write_text("# Plan\n")
    (worktree / "docs/awf-plans/ws_old.conformance.json").write_text(
        '{"status":"needs_iteration"}\n'
    )
    if implementation_diff:
        (worktree / "src/awf/retry.py").write_text("def retry():\n    return 'new'\n")
        (worktree / "tests/unit").mkdir(parents=True)
        (worktree / "tests/unit/test_retry.py").write_text("def test_retry():\n    assert True\n")
    return base_commit


async def _retry_with_preflight_override(
    service: WorkspaceService,
    workspace_id: str,
) -> object:
    return await service.retry_workspace(
        workspace_id,
        provider_readiness_override=True,
        provider_readiness_override_reason="retry service test fixture",
    )


async def _mark_failed(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    branch_name: str = "codex/old-attempt",
    remote_push_branch: str | None = None,
    release_runtime: bool = True,
) -> dict[str, object]:
    """Mark a workspace as failed with shared transition/evidence payload."""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="TEST")
        workspace.failure_reason = "validation_failure"
        workspace.failure_message = "pytest failed"
        workspace.branch_name = branch_name
        workspace.remote_push_branch = remote_push_branch
        workspace.pr_url = "https://github.com/example/retryable/pull/10"
        workspace.compose_project_name = "awf_old_attempt"
        assert workspace.resolved_profile is not None
        frozen_profile = {
            **workspace.resolved_profile,
            "source": "frozen:test-profile",
        }
        workspace.resolved_profile = frozen_profile
        await repo.transition(workspace, to=WorkspaceStatus.failed, reason_code="TEST_FAIL")
        if release_runtime:
            await repo.add_event(
                workspace,
                event_type="workspace.terminal_runtime_released",
                reason_code="TERMINAL_RUNTIME_RELEASED",
            )
        await session.commit()
        return frozen_profile


async def _mark_conformance_failed(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    base_commit: str | None = None,
) -> None:
    """Mark a workspace as failed with conformance-unsatisfied evidence."""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="TEST")
        workspace.failure_reason = FailureReason.agent_failure.value
        workspace.failure_message = (
            "plan conformance was not satisfied after 0 iteration(s): add tests"
        )
        workspace.branch_name = "awf/ws_old"
        workspace.remote_push_branch = "awf/ws_old"
        workspace.base_commit = base_commit
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code=PLAN_CONFORMANCE_UNSATISFIED,
            payload={
                "reason_code": PLAN_CONFORMANCE_UNSATISFIED,
                "details": {
                    "conformance": {
                        "summary": "Implementation is incomplete.",
                        "gaps": ["Add regression test", "Wire retry endpoint"],
                        "reason_code": PLAN_CONFORMANCE_UNSATISFIED,
                        "iterations_used": 0,
                        "max_iterations": 0,
                        "plan_path": "docs/awf-plans/ws_old.md",
                        "report_path": "docs/awf-plans/ws_old.conformance.json",
                    }
                },
                "salvage": {
                    "hint": "Workspace worktree and branch were preserved for salvage.",
                    "worktree_path": "/worktrees/ws_old",
                    "branch_name": "awf/ws_old",
                    "remote_push_branch": "awf/ws_old",
                },
            },
        )
        await repo.add_event(
            workspace,
            event_type="workspace.terminal_runtime_released",
            reason_code="TERMINAL_RUNTIME_RELEASED",
        )
        await session.commit()


async def _mark_conformance_failed_without_evidence(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    base_commit: str | None = None,
) -> None:
    """Mark a conformance failure workspace without conformance evidence payload."""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="TEST")
        workspace.failure_reason = FailureReason.agent_failure.value
        workspace.failure_message = "plan conformance was not satisfied"
        workspace.branch_name = "awf/ws_old"
        workspace.remote_push_branch = "awf/ws_old"
        workspace.base_commit = base_commit
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code=PLAN_CONFORMANCE_UNSATISFIED,
            payload={
                "reason_code": PLAN_CONFORMANCE_UNSATISFIED,
                "details": {"conformance": "legacy-invalid"},
            },
        )
        await repo.add_event(
            workspace,
            event_type="workspace.terminal_runtime_released",
            reason_code="TERMINAL_RUNTIME_RELEASED",
        )
        await session.commit()


async def _mark_agent_timeout_failed(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    base_commit: str | None = None,
    reason_code: str = AGENT_IDLE_TIMEOUT,
) -> None:
    """Mark a workspace failure caused by agent timeout in agent phase."""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="TEST")
        workspace.failure_reason = FailureReason.agent_failure.value
        workspace.failure_message = "agent emitted no output for 3600.0 seconds"
        workspace.branch_name = "awf/ws_timeout"
        workspace.remote_push_branch = "awf/ws_timeout"
        workspace.base_commit = base_commit
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code=reason_code,
            payload={
                "reason_code": reason_code,
                "message": "agent emitted no output for 3600.0 seconds",
                "details": {
                    "provider": "claude_code",
                    "model": "claude-opus-4-7",
                    "retryable": True,
                },
            },
        )
        await repo.add_event(
            workspace,
            event_type="workspace.terminal_runtime_released",
            reason_code="TERMINAL_RUNTIME_RELEASED",
        )
        await session.commit()


async def _mark_planning_scope_failed(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    approved_fallback_model: str | None = None,
    branch_name: str = "awf/ws_scope_old",
    remote_push_branch: str | None = "awf/ws_scope_old",
) -> None:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="TEST")
        workspace.failure_reason = FailureReason.agent_failure.value
        workspace.failure_message = (
            "planning phase changed files outside `docs/awf-plans/ws_scope_old.md`"
        )
        workspace.branch_name = branch_name
        workspace.remote_push_branch = remote_push_branch
        workspace.task_policy = {
            **workspace.task_policy,
            **(
                {
                    "planning_scope_recovery": {
                        "approved_fallback_model": approved_fallback_model,
                    }
                }
                if approved_fallback_model is not None
                else {}
            ),
        }
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code=AGENT_PLAN_PHASE_SCOPE_VIOLATION,
            payload={
                "reason_code": AGENT_PLAN_PHASE_SCOPE_VIOLATION,
                "message": workspace.failure_message,
                "details": {
                    "planning_scope": {
                        "scope_phase": "planning",
                        "required_paths": ["docs/awf-plans/ws_scope_old.md"],
                        "offending_paths": ["src/awf/runtime/planning.py"],
                        "offending_commands": [],
                        "recommended_action": (
                            "Retry planning from a clean workspace and salvage the "
                            "preserved branch only after explicit operator approval."
                        ),
                        "recovery_strategy": "discard_and_replan",
                        "salvage_policy": "explicit_salvage_required",
                    },
                    "recommended_action": (
                        "Retry planning from a clean workspace and salvage the preserved "
                        "branch only after explicit operator approval."
                    ),
                    "recovery_strategy": "discard_and_replan",
                    "salvage_policy": "explicit_salvage_required",
                },
                "salvage": {
                    "hint": "Workspace worktree and branch were preserved for salvage.",
                    "worktree_path": "/worktrees/ws_scope_old",
                    "branch_name": "awf/ws_scope_old",
                    "remote_push_branch": "awf/ws_scope_old",
                },
            },
        )
        await repo.add_event(
            workspace,
            event_type="workspace.terminal_runtime_released",
            reason_code="TERMINAL_RUNTIME_RELEASED",
        )
        await session.commit()


async def _seed_failed_source_workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    task_kind: str,
) -> str:
    """Persist a source workspace for a task kind that bypasses direct create.

    ``sync_feature_pr`` is created through the PR-adoption flow (via
    ``WorkspaceRepository.create``), not the public request path, so retry
    coverage seeds the row the same way instead of building a rejected request.
    """
    async with factory() as session:
        repo = WorkspaceRepository(session)
        source = await repo.create(
            repo_url="git@github.com:example/retryable.git",
            branch_base="development",
            task_title="Retry flaky validation",
            task_prompt="Fix the intermittent validation failure.",
            task_external_id="TICKET-RETRY",
            task_class="test_task",
            owned_paths=["src/awf/retry/**"],
            auto_merge=False,
            initial_review_grace_period_seconds=30,
            agent=AgentRuntime.codex.value,
            profile_ref="python",
            requested_profile={"source": "retry-test-profile"},
            resolved_profile={"source": "retry-test-profile"},
            test_commands=["uv run pytest tests/unit -q"],
            task_kind=task_kind,
        )
        await session.commit()
        return source.id
