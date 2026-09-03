"""Shared builders and fixtures for PR monitor pre-push validation tests.

Extracted from ``test_pr_monitor_pre_push_validation.py`` so the test module
stays under the first-party file line limit enforced by
``tests/unit/test_core_decomposition_maintainability.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.enums import WorkspaceStatus
from awf.db.repositories import ValidationRunRepository, WorkspaceRepository
from awf.profiles.models import WorkspaceProfile
from awf.runtime.validation_types import (
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationResult,
)


class _FakeValidation:
    """Minimal validation runner used to script pass/fail outcomes."""

    def __init__(
        self,
        *results: ValidationResult | Exception,
        coverage_result: ValidationCoverageResult | None = None,
    ) -> None:
        """Store queued validation results for later retrieval."""
        self.results = list(results)
        self.coverage_result = coverage_result
        self.calls: list[dict[str, object]] = []
        self.coverage_calls: list[dict[str, object]] = []

    async def run_profile_phases(self, **kwargs: object) -> ValidationResult:
        """Return the next queued validation outcome."""
        self.calls.append(dict(kwargs))
        if not self.results:
            raise AssertionError("validation called more times than expected")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def run_profile_coverage(self, **kwargs: object) -> ValidationCoverageResult | None:
        """Stub profile coverage step; included for interface compatibility."""
        self.coverage_calls.append(dict(kwargs))
        return self.coverage_result


def _command_result(
    tmp_path: Path,
    *,
    ok: bool,
    reason_code: str | None = None,
    command: str = "pytest -q",
    returncode: int | None = None,
    artifact_name: str | None = None,
    phase: str = "validate",
) -> ValidationCommandResult:
    """Build a deterministic validation command result with local artifact paths."""
    if reason_code is None:
        reason_code = "VALIDATION_OK" if ok else "PYTEST_TEST_FAILURE"
    label = artifact_name or ("ok" if ok else command.replace("/", "_").replace(" ", "_"))
    stdout_path = tmp_path / f"{label}.stdout"
    stderr_path = tmp_path / f"{label}.stderr"
    stdout_path.write_text("passed\n" if ok else "failed\n", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    return ValidationCommandResult(
        command=command,
        returncode=0 if ok else (returncode if returncode is not None else 1),
        duration_seconds=0.1,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        phase=phase,
        reason_code=reason_code,
    )


def _validation_result(
    tmp_path: Path,
    *,
    ok: bool,
    reason_code: str | None = None,
    command: str = "pytest -q",
    returncode: int | None = None,
    artifact_name: str | None = None,
    phase: str = "validate",
    coverage: ValidationCoverageResult | None = None,
    commands: list[ValidationCommandResult] | None = None,
) -> ValidationResult:
    """Wrap command result(s) into a validation result, optionally with coverage."""
    if commands is None:
        commands = [
            _command_result(
                tmp_path,
                ok=ok,
                reason_code=reason_code,
                command=command,
                returncode=returncode,
                artifact_name=artifact_name,
                phase=phase,
            )
        ]
    return ValidationResult(commands=commands, coverage=coverage)


class _CommandlessFailureValidationResult(ValidationResult):
    """Validation result that exposes a first failure outside command records."""

    _first_failure: ValidationCommandResult

    def __init__(self, first_failure: ValidationCommandResult) -> None:
        super().__init__(commands=[])
        object.__setattr__(self, "_first_failure", first_failure)

    @property
    def all_passed(self) -> bool:
        return False

    @property
    def first_failure(self) -> ValidationCommandResult | None:
        return self._first_failure


class _OverriddenFirstFailureValidationResult(ValidationResult):
    """Validation result whose provider-level first failure differs from commands."""

    _first_failure: ValidationCommandResult

    def __init__(
        self,
        *,
        commands: list[ValidationCommandResult],
        first_failure: ValidationCommandResult,
    ) -> None:
        super().__init__(commands=commands)
        object.__setattr__(self, "_first_failure", first_failure)

    @property
    def all_passed(self) -> bool:
        return False

    @property
    def first_failure(self) -> ValidationCommandResult | None:
        return self._first_failure


def _coverage_result(tmp_path: Path) -> ValidationCoverageResult:
    """Build a successful explicit coverage result for pre-push coverage tests."""
    return ValidationCoverageResult(
        provider="python",
        percent=99.5,
        minimum_percent=99.0,
        enforce=True,
        status="passed",
        reason_code="COVERAGE_OK",
        command_result=_command_result(tmp_path, ok=True, reason_code="COVERAGE_OK"),
        gaps=[{"path": "src/awf/runtime/pr_monitor_runner/pre_push_validation.py"}],
    )


def _failing_coverage_result(tmp_path: Path) -> ValidationCoverageResult:
    """Build a failed coverage result whose command exited successfully."""
    return ValidationCoverageResult(
        provider="python",
        percent=98.5,
        minimum_percent=99.0,
        enforce=True,
        status="failed",
        reason_code="COVERAGE_BELOW_THRESHOLD",
        command_result=_command_result(
            tmp_path,
            ok=True,
            reason_code="VALIDATION_OK",
            command="coverage run -m pytest && coverage report",
            artifact_name="coverage_below_threshold",
        ),
        gaps=[{"path": "src/awf/runtime/pr_monitor_runner/pre_push_validation.py"}],
    )


def _provider_coverage_failure_without_command() -> ValidationCoverageResult:
    """Build a failed provider result without an associated command record."""
    return ValidationCoverageResult(
        provider="python",
        percent=None,
        minimum_percent=99.0,
        enforce=True,
        status="failed",
        reason_code="COVERAGE_PROVIDER_FAILED",
        provider_failure_evidence=["coverage provider did not produce totals"],
    )


async def _set_resolved_profile(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    include_coverage: bool = False,
    coverage_final_gate: str = "coverage",
    setup_commands: list[str] | None = None,
    post_agent_commands: list[str] | None = None,
    validate_commands: list[str] | None = None,
) -> None:
    """Attach a simple resolved validation profile to the workspace."""
    phases: dict[str, object] = {
        "validate": ["pytest -q"] if validate_commands is None else validate_commands,
    }
    if setup_commands is not None:
        phases["setup"] = setup_commands
    if post_agent_commands is not None:
        phases["post_agent"] = post_agent_commands
    profile_payload: dict[str, object] = {
        "name": "test-profile",
        "phases": phases,
    }
    if include_coverage:
        profile_payload["validation"] = {
            "coverage": {
                "minimum_percent": 99.0,
                "command": "coverage run -m pytest && coverage report",
            },
            "strategy": {"final_gate": coverage_final_gate},
        }
    profile = WorkspaceProfile.model_validate(profile_payload)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        ws.resolved_profile = profile.model_dump(mode="json", by_alias=True)
        await session.commit()


async def _set_symlink_form_baseline(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    index_symlinks_are_symlinks: bool | None,
) -> None:
    """Persist the checkout symlink-form baseline used by validation cleanup."""
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        ws.block_index_symlinks_are_symlinks = index_symlinks_are_symlinks
        await session.commit()


def _name_status_z(*records: str) -> str:
    """Render ``git diff --name-status -z``-shaped stdout from raw NUL records.

    Each record is expected to already include its own NUL terminators (e.g.
    ``"M\\0src/fix.py\\0"`` or a rename ``"R100\\0old.txt\\0new.txt\\0"``), so
    callers can build arbitrarily shaped ``--name-status -z`` output without a
    bespoke builder per status letter.
    """
    return "".join(records)


def _mark_git_worktree(worktree: Path) -> None:
    """Make a temp directory look like a git worktree with a real gitdir.

    The PR monitor pre-push guard now passes ``remove_empty_untracked_dirs=True``,
    which calls ``_gitlink_paths`` and runs ``git -C <worktree> ls-tree -z -r -d HEAD``.
    A fake gitdir pointer causes git to exit 128, which the guard correctly
    interprets as ``VALIDATION_WORKTREE_STATUS_FAILED``. Real tests therefore need
    a real temp repo with at least one commit so HEAD resolves.

    Fixture git init/commit must ignore host or test-poisoned object-lookup env
    (``GIT_OBJECT_DIRECTORY``, alternates, replace refs, grafts). Callers that
    assert merge-safety helpers strip those variables often set them before the
    function under test; inheriting them here can abort ``git commit`` on CI.
    """
    import os
    import subprocess

    from awf.node.git_manager import git_env_without_object_lookup_overrides

    worktree.mkdir(parents=True, exist_ok=True)
    repo_dir = worktree.with_name(f"{worktree.name}-repo")
    repo_dir.mkdir(parents=True, exist_ok=True)
    git_env = git_env_without_object_lookup_overrides()
    git_env.pop("GIT_REPLACE_REF_BASE", None)
    git_env["GIT_GRAFT_FILE"] = os.devnull
    git_env["GIT_NO_REPLACE_OBJECTS"] = "1"
    subprocess.run(
        ["git", "init", str(repo_dir)],
        check=True,
        capture_output=True,
        text=True,
        env=git_env,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.email", "agent@example.com"],
        check=True,
        capture_output=True,
        text=True,
        env=git_env,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.name", "AWF Agent"],
        check=True,
        capture_output=True,
        text=True,
        env=git_env,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "--allow-empty", "-m", "init"],
        check=True,
        capture_output=True,
        text=True,
        env=git_env,
    )
    (worktree / ".git").write_text(f"gitdir: {repo_dir / '.git'}\n", encoding="utf-8")


async def _seed_monitoring_workspace_without_attempt(
    factory: async_sessionmaker[AsyncSession],
) -> str:
    """Create a monitoring workspace row without a task-attempt record."""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:dimileeh/aira-web.git",
            branch_base="development",
            task_title="monitor test without attempt",
            task_prompt="x",
            agent="claude_code",
            test_commands=["pytest -q"],
            requires_database=False,
            auto_merge=True,
        )
        for target in (
            WorkspaceStatus.provisioning,
            WorkspaceStatus.ready,
            WorkspaceStatus.running,
            WorkspaceStatus.validating,
            WorkspaceStatus.pushing,
            WorkspaceStatus.monitoring_pr,
        ):
            await repo.transition(ws, to=target, reason_code="X")
        ws.branch_name = f"awf/{ws.id}"
        ws.remote_push_branch = ws.branch_name
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.compose_file_path = f"/tmp/awf/{ws.id}/compose.yml"
        ws.pr_url = "https://github.com/dimileeh/aira-web/pull/42"
        ws.pr_number = 42
        await session.commit()
        return ws.id


async def _validation_runs(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> list[Any]:
    """Return all persisted validation runs for a workspace."""
    async with factory() as session:
        return await ValidationRunRepository(session).list_for_workspace(workspace_id)
