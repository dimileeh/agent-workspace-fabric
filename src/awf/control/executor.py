"""Workspace executor — drives ``ready`` → ``completed`` (or ``failed``).

Pipeline:

    ready
      └─▶ running            (agent CLI invoked inside the container)
            └─▶ validating   (test commands + Alembic if required)
                  └─▶ pushing (git push + gh pr create)
                        └─▶ completed

Failure at any step transitions to ``failed`` with a typed ``FailureReason``
and keeps the compose stack running so operators can docker-exec in for
triage. Explicit ``cleanup(workspace_id)`` is a separate operation.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import shlex
import time
import traceback
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import (
    DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS,
    DEFAULT_AGENT_WALL_TIMEOUT_SECONDS,
    AgentAdapter,
    AgentDefaults,
    AgentRunError,
    get_adapter,
)
from awf.adapters.defaults import DEFAULT_AGENT_DEFAULTS, defaults_with_model_overrides
from awf.common.audit import redact_audit_text, redact_audit_value
from awf.common.command_evidence import append_command_evidence
from awf.common.commands import AsyncCommandRunner, CommandResult
from awf.common.compose_exec import (
    EXEC_PROCESS_CLEANUP_FAILED,
    ComposeExecCleanupError,
    build_tracked_compose_exec,
    cleanup_failure_message,
)
from awf.common.git_identity import git_identity_config_args, git_safe_directory_config_args
from awf.common.github_client import RepoRef
from awf.common.logging import get_logger
from awf.control.quality_gates import (
    PLAN_ONLY_OUTPUT_REASON_CODE,
    ProtectedFileDiff,
    changed_paths_are_only_internal_plan_artifacts,
    diff_classified_protected_paths,
    find_protected_quality_gate_changes,
    plan_only_output_message,
    quality_gate_violation_message,
)
from awf.control.state_machine import WorkspaceStateMachine
from awf.control.validation_fix_cycle import (
    ValidationFixContext,
    build_fix_prompt,
    read_output_tail,
)
from awf.db.enums import (
    AgentRuntime,
    FailureReason,
    OperationStatus,
    OperationType,
    TaskClass,
    WorkspaceStatus,
)
from awf.db.models import Operation, Workspace
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    ResourceReservationRepository,
    StaleReasonRepository,
    TaskAttemptRepository,
    ValidationRunRepository,
    WorkspaceRepository,
    sync_candidate_readiness,
)
from awf.db.validation_runs import validation_run_coverage_payload
from awf.node.compose_manager import ComposeManager, ComposeOperationError
from awf.node.git_manager import mirror_path_for_worktree, repair_agent_writable_worktree
from awf.profiles.models import WorkspaceProfile
from awf.profiles.resolver import resolve_workspace_profile
from awf.runtime.alembic_validation import (
    ALEMBIC_MIGRATION_POLICY_COMMAND,
    ALEMBIC_MIGRATION_POLICY_PHASE,
)
from awf.runtime.logs import LogStore
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    AGENT_STALLED_IN_CONFORMANCE,
    CONFORMANCE_REQUIRES_AWF_VALIDATION,
    PLAN_CONFORMANCE_UNSATISFIED,
    ConformanceIterationRecord,
    ConformanceStallEvidence,
    ConformanceStallKind,
    ConformanceStallPolicy,
    PlanConformanceReport,
    PlanConformanceStatus,
    build_agent_task_prompt,
    build_conformance_failure_evidence,
    build_conformance_prompt,
    build_conformance_stall_failure_evidence,
    build_execution_prompt,
    build_planning_prompt,
    changed_paths_from_porcelain,
    classify_conformance_stall,
    conformance_requires_awf_validation,
    parse_conformance_report,
    render_workspace_path,
)
from awf.runtime.pr_creator import PullRequestCreator, PullRequestError
from awf.runtime.pr_monitor_operations import (
    MonitorOperationHandle,
    build_monitor_operation_payload,
    create_or_start_monitor_operation,
    finish_monitor_operation,
    monitor_operation_idempotency_key,
)
from awf.runtime.pr_push_remote import remote_push_url_for_workspace
from awf.runtime.validation import (
    DATABASE_GENERATED_SETUP_TIMEOUT,
    DATABASE_REFRESH_TIMEOUT,
    DB_GENERATED_SETUP_PHASE,
    PROFILE_VALIDATION_TOOL_UNAVAILABLE,
    PYTEST_TEST_FAILURE,
    SETUP_DEPENDENCY_NETWORK_FAILURE,
    SETUP_DEPENDENCY_NETWORK_METADATA_KEY,
    SETUP_DEPENDENCY_NETWORK_RETRY,
    SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED,
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationResult,
    ValidationRunner,
    profile_phase_command_plan,
)
from awf.runtime.validation_identity import (
    environment_identity_digest,
    environment_identity_inputs,
    resolved_profile_digest,
)
from awf.runtime.workspace_prompt_context import render_workspace_runtime_context
from awf.service.conformance_salvage import (
    CONFORMANCE_SALVAGE_APPLIED_EVENT_TYPE,
    CONFORMANCE_SALVAGE_APPLIED_REASON,
    CONFORMANCE_SALVAGE_CONFLICT_EVENT_TYPE,
    CONFORMANCE_SALVAGE_CONFLICT_REASON,
    SALVAGE_PATCH_APPLY_FAILED,
    SALVAGE_PATCH_DIGEST_MISMATCH,
    SALVAGE_PATCH_UNAVAILABLE,
    build_conformance_salvage_conflict_prompt,
    conformance_salvage_from_task_policy,
)
from awf.service.coordination import coordination_warnings_from_task_policy
from awf.service.provider_recovery import create_provider_recovery_attempt_row
from awf.service.supply_chain_policy import (
    SupplyChainFinding,
    SupplyChainPolicyRefreshResult,
    SupplyChainPolicyRefreshService,
)
from awf.service.workspaces import WorkspaceRetryError, retry_workspace_row


class _MonitorRunnerProto(Protocol):
    """Minimum surface the executor needs from a PR monitor runner.

    Declared as a Protocol so the executor doesn't structurally depend
    on ``PullRequestMonitorRunner`` — tests can pass a tiny stub, and
    the monitor stage is a clean extension seam for Phase 2 variants
    (merge queue, release-PR monitor, etc.)."""

    async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None: ...


_log = get_logger(__name__)


def _monotonic() -> float:
    return time.monotonic()


WORKTREE_MISSING_REASON_CODE = "WORKTREE_MISSING"
PR_REEXECUTION_GUARD_REASON_CODE = "PR_REEXECUTION_GUARD"
_EXECUTOR_AUDIT_ACTOR = "executor"
_AUDIT_GIT_PUSH_EVENT = "workspace.audit.git_push"
_AUDIT_PR_CREATED_EVENT = "workspace.audit.pr_created"
_GIT_PUSH_FAILED_REASON_CODE = "GIT_PUSH_FAILED"
_PR_CREATE_FAILED_REASON_CODE = "PR_CREATE_FAILED"
GIT_AGENT_WRITABILITY_FAILED_REASON_CODE = "GIT_AGENT_WRITABILITY_FAILED"
GIT_OBJECT_MISSING_REASON_CODE = "GIT_OBJECT_MISSING"
GIT_OBJECT_MISSING_RECOVERED_REASON_CODE = "GIT_OBJECT_MISSING_RECOVERED"
POST_VALIDATION_CONFORMANCE_REPORT_GIT_FAILED_REASON_CODE = (
    "POST_VALIDATION_CONFORMANCE_REPORT_GIT_FAILED"
)
POST_VALIDATION_CONFORMANCE_REPORT_WRITE_FAILED_REASON_CODE = (
    "POST_VALIDATION_CONFORMANCE_REPORT_WRITE_FAILED"
)
POST_VALIDATION_CONFORMANCE_FAILED_REASON_CODE = "POST_VALIDATION_CONFORMANCE_FAILED"
POST_AGENT_GIT_ADD_FAILED_REASON_CODE = "POST_AGENT_GIT_ADD_FAILED"
POST_AGENT_COMMIT_FAILED_REASON_CODE = "POST_AGENT_COMMIT_FAILED"
POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE = "POST_AGENT_COMMIT_PRECOMMIT_FAILED"
POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE = "POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED"
POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE = "POST_AGENT_FORMAT_REPAIR_FAILED"
POST_AGENT_COMMIT_REPAIR_EVENT_TYPE = "workspace.post_agent_commit_repair"
POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE = POST_AGENT_COMMIT_REPAIR_EVENT_TYPE
SETUP_DEPENDENCY_NETWORK_RETRY_EVENT_TYPE = "workspace.setup_dependency_network_retry"
SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED_EVENT_TYPE = (
    "workspace.setup_dependency_network_retry_exhausted"
)
_PR_MONITOR_ADOPTED_EVENT = "workspace.pr_monitor_adopted"
_PR_MONITOR_ADOPTED_REASON_CODE = "PR_MONITOR_ADOPTED"
_PR_ADOPTION_SKIP_AGENT_REASON_CODE = "PR_ADOPTION_SKIP_AGENT"
_PR_ADOPTION_METADATA_MISSING_REASON_CODE = "PR_ADOPTION_METADATA_MISSING"
_PR_ADOPTION_MONITOR_UNAVAILABLE_REASON_CODE = "PR_ADOPTION_MONITOR_UNAVAILABLE"
_EXCEPTION_TRACEBACK_LIMIT = 4000
_VALIDATION_EVIDENCE_JSON_LIMIT = 20000
_VALIDATION_EVIDENCE_COVERAGE_PRIORITY_KEYS = (
    "status",
    "reason_code",
    "percent",
    "minimum_percent",
    "enforce",
    "provider",
)
_VALIDATION_EVIDENCE_RETAINED_KEY_COUNT = 5
_VALIDATION_EVIDENCE_CORE_KEYS = (
    "validation_run_id",
    "status",
    "reason_code",
    "coverage",
    "workspace_head_sha",
    "target_branch",
    "target_head_sha",
    "base_commit",
    "base_sha",
    "tier",
    "retry_count",
    "command_set_hash",
)
_FILE_DIGEST_CHUNK_SIZE = 64 * 1024

_RECOVERY_ACTIVE_OPERATION_STATUSES = {
    OperationStatus.pending.value,
    OperationStatus.running.value,
}
_VALIDATE_ONLY_RECOVERY_SOURCES = {"pr_monitor", "operator_api"}
_VALIDATE_ONLY_RECOVERY_MODES = {"validate_only", "rebase_only"}
_REBASE_RECOVERY_OPERATION_IDENTITY_KEYS = (
    "source",
    "recovery_mode",
    "reason_code",
    "pr_number",
    "source_head_sha",
    "source_base_sha",
)


@dataclass(frozen=True)
class _RebaseRecoveryResult:
    base_sha: str
    head_sha: str


class _PostValidationConformanceReportGitError(RuntimeError):
    def __init__(
        self,
        *,
        operation: str,
        result: CommandResult,
        cleanup_operation: str | None = None,
        cleanup_result: CommandResult | None = None,
    ) -> None:
        output = (result.stderr or result.stdout or "").strip()
        message = (
            f"post-validation conformance report git {operation} failed "
            f"(exit={result.returncode}): {output}"
        )
        if cleanup_operation is not None and cleanup_result is not None:
            cleanup_output = (cleanup_result.stderr or cleanup_result.stdout or "").strip()
            message = (
                f"{message}; cleanup git {cleanup_operation} failed "
                f"(exit={cleanup_result.returncode}): {cleanup_output}"
            )
        super().__init__(message)
        self.operation = operation
        self.returncode = result.returncode
        self.command_reason_code = result.reason_code
        self.cleanup_operation = cleanup_operation
        self.cleanup_returncode = cleanup_result.returncode if cleanup_result is not None else None
        self.cleanup_command_reason_code = (
            cleanup_result.reason_code if cleanup_result is not None else None
        )


class _PostValidationConformanceReportWriteError(RuntimeError):
    def __init__(self, *, report_path: Path, error: OSError) -> None:
        message = (
            f"post-validation conformance report write failed for {report_path.as_posix()}: {error}"
        )
        super().__init__(message)
        self.report_path = report_path
        self.error_type = type(error).__name__
        self.errno = error.errno


_AWF_RUFF_FORMAT_CHECK_HOOK_ID = "awf-ruff-format-check"
_AWF_RUFF_CHECK_HOOK_ID = "awf-ruff-check"
_PRE_COMMIT_DETERMINISTIC_REPAIR_HOOK_IDS = frozenset(
    {
        "trailing-whitespace",
        "end-of-file-fixer",
        _AWF_RUFF_FORMAT_CHECK_HOOK_ID,
    }
)
_PRE_COMMIT_HOOK_ID_PATTERN = re.compile(r"^-\s*hook id:\s*(?P<hook_id>\S+)", re.MULTILINE)
_PRE_COMMIT_WOULD_REFORMAT_PATTERN = re.compile(r"^Would reformat:\s*(?P<path>\S.*)$", re.MULTILINE)
_PRE_COMMIT_FIXING_PATH_PATTERN = re.compile(r"^Fixing\s+(?P<path>\S.*)$", re.MULTILINE)
_RUFF_DIAGNOSTIC_PATTERN = re.compile(r"^\s*[A-Z]+[0-9]+\s*(?P<fixable>\[\*\])?")
_RUFF_DIAGNOSTIC_PATH_PATTERN = re.compile(r"^\s*-->\s+(?P<path>.+?):\d+:\d+")


def _ruff_check_autofix_repair_files(output: str) -> tuple[str, ...]:
    """Return Ruff paths only when every observed diagnostic is auto-fixable."""

    paths: list[str] = []
    pending_fixable_path = False
    saw_diagnostic = False
    saw_unfixable = False
    for line in output.splitlines():
        diagnostic = _RUFF_DIAGNOSTIC_PATTERN.match(line)
        if diagnostic is not None:
            saw_diagnostic = True
            pending_fixable_path = diagnostic.group("fixable") is not None
            if not pending_fixable_path:
                saw_unfixable = True
            continue
        if pending_fixable_path:
            path = _RUFF_DIAGNOSTIC_PATH_PATTERN.match(line)
            if path is not None:
                paths.append(path.group("path").strip())
                pending_fixable_path = False
    if not saw_diagnostic or saw_unfixable:
        return ()
    return tuple(dict.fromkeys(paths))


@dataclass(frozen=True)
class _PostAgentCommitClassification:
    """Structured result of parsing a non-zero ``git commit`` output.

    ``reason_code`` is one of the ``POST_AGENT_COMMIT_*`` constants.
    ``failed_hooks`` lists the pre-commit hook ids parsed from the output;
    empty when the failure is not pre-commit related. ``deterministic_hooks``
    are hooks AWF can repair without guessing (normalizer hooks and scoped
    formatters). ``semantic_hooks`` require agent repair or human/operator
    attention. ``format_repair_files`` holds ``Would reformat:`` paths
    verbatim; the executor intersects them with the agent's staged diff before
    invoking the formatter. ``normalizer_repair_files`` holds ``Fixing ...``
    paths from pre-commit normalizer hooks for repair prompts and event
    provenance. ``summary`` is a truncated, human blurb safe for the workspace
    ``failure_message``.
    """

    reason_code: str
    failed_hooks: tuple[str, ...]
    format_repair_files: tuple[str, ...]
    summary: str
    deterministic_hooks: tuple[str, ...] = ()
    semantic_hooks: tuple[str, ...] = ()
    normalizer_repair_files: tuple[str, ...] = ()
    autofix_repair_files: tuple[str, ...] = ()
    repair_strategy: str = "none"


class _PostAgentCommitStepError(RuntimeError):
    """Raised when post-agent ``git add`` / ``git commit`` exits non-zero.

    Carries the structured classification so the outer exception handler
    can emit a specific reason code instead of falling back to
    ``INFRASTRUCTURE_FAILURE``. The ``stage`` field distinguishes
    ``git add`` failures from ``git commit`` failures; the
    ``classification`` field is ``None`` for ``git add`` failures (we
    only classify commit output). ``reason_code_override`` lets the
    format-repair path surface a distinct code (e.g. when
    ``ruff format`` itself crashed) without mutating the parsed
    classification. ``failure_reason_override`` lets policy gates retain
    their terminal failure class when they run inside the repair helper.
    """

    def __init__(
        self,
        *,
        stage: str,
        result: CommandResult,
        classification: _PostAgentCommitClassification | None,
        format_repair_attempted: bool = False,
        precommit_repair_attempted: bool = False,
        repair_strategy: str | None = None,
        reason_code_override: str | None = None,
        failure_reason_override: FailureReason | None = None,
    ) -> None:
        self.stage = stage
        self.result = result
        self.classification = classification
        self.format_repair_attempted = format_repair_attempted
        self.precommit_repair_attempted = precommit_repair_attempted
        self.repair_strategy = repair_strategy
        self.reason_code_override = reason_code_override
        self.failure_reason_override = failure_reason_override
        output = (result.stderr or result.stdout or "").strip()
        super().__init__(f"post-agent {stage} failed (exit={result.returncode}): {output}")


def _classify_post_agent_commit_failure(
    result: CommandResult,
) -> _PostAgentCommitClassification:
    """Classify a failed ``git commit`` CommandResult.

    The classifier reads only ``result.stdout`` and ``result.stderr``; it
    does not touch the worktree. When the captured output looks like
    pre-commit hook framing (``- hook id: <id>``), we treat the failure
    as pre-commit related. Deterministic hook failures are safe for AWF to
    retry without interpretation; semantic hook failures require a targeted
    repair turn. The narrow historical subcase — sole failing hook is
    ``awf-ruff-format-check`` AND we can parse ``Would reformat:`` lines —
    keeps the existing ``POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED`` reason code
    for compatibility.
    """

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    combined = f"{stdout}\n{stderr}"

    failed_hooks = tuple(
        dict.fromkeys(
            match.group("hook_id") for match in _PRE_COMMIT_HOOK_ID_PATTERN.finditer(combined)
        )
    )
    format_repair_files = tuple(
        match.group("path").strip()
        for match in _PRE_COMMIT_WOULD_REFORMAT_PATTERN.finditer(combined)
    )
    normalizer_repair_files = tuple(
        dict.fromkeys(
            match.group("path").strip()
            for match in _PRE_COMMIT_FIXING_PATH_PATTERN.finditer(combined)
        )
    )
    autofix_repair_files = (
        _ruff_check_autofix_repair_files(combined)
        if _AWF_RUFF_CHECK_HOOK_ID in failed_hooks
        else ()
    )
    deterministic_hooks = tuple(
        hook for hook in failed_hooks if hook in _PRE_COMMIT_DETERMINISTIC_REPAIR_HOOK_IDS
    )
    semantic_hooks = tuple(
        hook for hook in failed_hooks if hook not in _PRE_COMMIT_DETERMINISTIC_REPAIR_HOOK_IDS
    )
    repair_strategy = (
        "agent" if semantic_hooks else "deterministic" if deterministic_hooks else "none"
    )

    raw_summary = stderr.strip() or stdout.strip()
    summary = raw_summary[:2000]

    if failed_hooks:
        if set(failed_hooks) == {_AWF_RUFF_FORMAT_CHECK_HOOK_ID} and format_repair_files:
            return _PostAgentCommitClassification(
                reason_code=POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE,
                failed_hooks=failed_hooks,
                format_repair_files=format_repair_files,
                summary=summary or "ruff format --check reported files would be reformatted",
                deterministic_hooks=deterministic_hooks,
                semantic_hooks=semantic_hooks,
                normalizer_repair_files=normalizer_repair_files,
                autofix_repair_files=autofix_repair_files,
                repair_strategy=repair_strategy,
            )
        return _PostAgentCommitClassification(
            reason_code=POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE,
            failed_hooks=failed_hooks,
            format_repair_files=format_repair_files,
            summary=summary or "pre-commit hooks rejected the post-agent commit",
            deterministic_hooks=deterministic_hooks,
            semantic_hooks=semantic_hooks,
            normalizer_repair_files=normalizer_repair_files,
            autofix_repair_files=autofix_repair_files,
            repair_strategy=repair_strategy,
        )

    return _PostAgentCommitClassification(
        reason_code=POST_AGENT_COMMIT_FAILED_REASON_CODE,
        failed_hooks=(),
        format_repair_files=(),
        summary=summary or "git commit exited non-zero with no output",
    )


def _build_post_agent_precommit_repair_prompt(
    *,
    classification: _PostAgentCommitClassification,
    staged_paths: Sequence[str],
) -> str:
    failed_hooks = ", ".join(classification.failed_hooks) or "unknown"
    semantic_hooks = ", ".join(classification.semantic_hooks) or "unknown"
    staged_preview = "\n".join(f"- {path}" for path in staged_paths[:80])
    if len(staged_paths) > 80:
        staged_preview += f"\n- ... and {len(staged_paths) - 80} more"
    normalizer_preview = "\n".join(
        f"- {path}" for path in classification.normalizer_repair_files[:40]
    )
    if len(classification.normalizer_repair_files) > 40:
        normalizer_preview += f"\n- ... and {len(classification.normalizer_repair_files) - 40} more"
    formatter_preview = "\n".join(f"- {path}" for path in classification.format_repair_files[:40])
    if len(classification.format_repair_files) > 40:
        formatter_preview += f"\n- ... and {len(classification.format_repair_files) - 40} more"
    summary = redact_audit_text(classification.summary, limit=3000)
    return (
        "AWF post-agent pre-commit repair is required.\n\n"
        "The implementation work has returned, but `git commit` was rejected "
        "by semantic pre-commit hook failures. Fix only the hook failures below, "
        "then stop. Do not bypass pre-commit. Do not change coverage policy, "
        "dependency files, CI, or unrelated files. Remove temporary/debug files "
        "if they caused the hook failure.\n\n"
        f"Failed hooks: {failed_hooks}\n"
        f"Semantic hooks that require code/test repair: {semantic_hooks}\n\n"
        "Currently staged paths:\n"
        f"{staged_preview or '- <none>'}\n\n"
        "Normalizer-rewritten paths, if any:\n"
        f"{normalizer_preview or '- <none>'}\n\n"
        "Formatter-reported paths, if any:\n"
        f"{formatter_preview or '- <none>'}\n\n"
        "Pre-commit output tail:\n"
        f"{summary or '<no output>'}\n"
    )


@dataclass(frozen=True)
class _CoverageEvidenceResult:
    coverage: ValidationCoverageResult | None
    evidence_status: str | None = None
    reason_code: str | None = None
    source_run_id: str | None = None


@dataclass(frozen=True)
class _PrReexecutionGuardResult:
    blocked: bool
    recovery: dict[str, Any] | None = None


@dataclass(frozen=True)
class _PlanningRunFailure:
    message: str
    reason_code: str | None = None
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class _PlanningValidationHandoff:
    report: PlanConformanceReport
    plan_path: Path
    report_path: Path
    iteration: int
    max_iterations: int


@dataclass(frozen=True)
class _GitObjectRecoveryResult:
    broken_head_sha: str | None
    recovered_head_sha: str
    strategy: str = "filesystem_tree_commit"


def _git_error_indicates_missing_head_object(text: str) -> bool:
    lower = text.lower()
    return (
        "bad object head" in lower
        or "not a valid object name head" in lower
        or "could not parse object 'head'" in lower
    )


def _agent_git_writability_preflight_script(workspace_id: str) -> str:
    quoted_workspace_id = shlex.quote(workspace_id)
    return f"""
set -eu
workspace_id={quoted_workspace_id}
git status --porcelain >/dev/null
blob=$(printf 'awf git preflight %s\\n' "$workspace_id" | git hash-object -w --stdin)
git cat-file -e "$blob^{{blob}}"
ref="refs/awf/preflight/$workspace_id"
git update-ref "$ref" HEAD
git update-ref -d "$ref"
""".strip()


async def _recover_missing_head_from_filesystem(
    *,
    runner: AsyncCommandRunner,
    workspace_id: str,
    worktree_path: Path,
    base_commit: str,
    branch_name: str,
) -> _GitObjectRecoveryResult | None:
    """Rebuild a valid AWF branch commit from files when HEAD points to a missing object."""
    mirror_path = mirror_path_for_worktree(worktree_path)
    if mirror_path is None:
        return None
    branch_ref = branch_name if branch_name.startswith("refs/") else f"refs/heads/{branch_name}"
    broken_head_sha = _read_ref_sha(mirror_path, branch_ref)

    async def mirror_git(args: list[str]) -> CommandResult:
        return await runner.run(["git", "--git-dir", str(mirror_path), *args])

    async def worktree_git(args: list[str]) -> CommandResult:
        return await runner.run(
            [
                "git",
                *git_safe_directory_config_args(worktree_path),
                "-C",
                str(worktree_path),
                *args,
            ]
        )

    base_ok = await mirror_git(["cat-file", "-e", f"{base_commit}^{{commit}}"])
    if not base_ok.ok:
        return None
    reset_ref = await mirror_git(["update-ref", branch_ref, base_commit])
    if not reset_ref.ok:
        return None
    reset_index = await worktree_git(["reset", "--mixed", "HEAD"])
    if not reset_index.ok:
        return None
    add = await worktree_git(["add", "-A"])
    if not add.ok:
        return None
    diff = await worktree_git(["diff", "--cached", "--quiet"])
    if diff.returncode not in {0, 1}:
        return None
    if diff.returncode == 0:
        return None
    commit = await runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            *git_identity_config_args(),
            "commit",
            "-m",
            f"awf: recover {workspace_id} from missing git object"[:72],
            "-m",
            (
                f"AWF recovered workspace {workspace_id} after HEAD pointed at "
                "a commit object missing from the canonical mirror. The commit "
                f"squashes the workspace filesystem state onto base {base_commit[:10]}."
            ),
        ]
    )
    if not commit.ok:
        return None
    await asyncio.to_thread(repair_agent_writable_worktree, mirror_path, worktree_path)
    head = await worktree_git(["rev-parse", "HEAD"])
    recovered_head_sha = head.stdout.strip()
    if not head.ok or not recovered_head_sha:
        return None
    return _GitObjectRecoveryResult(
        broken_head_sha=broken_head_sha,
        recovered_head_sha=recovered_head_sha,
    )


def _read_ref_sha(mirror_path: Path, ref: str) -> str | None:
    ref_path = mirror_path / ref
    try:
        value = ref_path.read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    if value:
        return value
    packed_refs = mirror_path / "packed-refs"
    try:
        for line in packed_refs.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith(("#", "^")):
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    except OSError:
        return None
    return None


def _build_planning_scope_failure(
    *,
    scope_phase: str,
    required_paths: Sequence[Path],
    offending_paths: Sequence[Path],
    summary: str,
    offending_commands: Sequence[str] = (),
) -> _PlanningRunFailure:
    required = [path.as_posix() for path in required_paths]
    offending = [path.as_posix() for path in sorted(offending_paths)]
    commands = [command for command in offending_commands if command]
    recommended_action = (
        "Retry planning from a clean workspace. Discard the premature implementation "
        "by default, and salvage the preserved branch only after explicit operator approval."
    )
    artifact = required[0] if required else "the configured plan artifact"
    if offending:
        message = f"{summary}: {', '.join(offending[:10])}. {recommended_action}"
    else:
        message = f"{summary}. {recommended_action}"
    planning_scope = {
        "scope_phase": scope_phase,
        "required_paths": required,
        "offending_paths": offending,
        "offending_commands": commands,
        "recommended_action": recommended_action,
        "recovery_strategy": "discard_and_replan",
        "salvage_policy": "explicit_salvage_required",
        "plan_artifact": artifact,
    }
    return _PlanningRunFailure(
        message=message,
        reason_code=AGENT_PLAN_PHASE_SCOPE_VIOLATION,
        details={
            "planning_scope": planning_scope,
            "recommended_action": recommended_action,
            "recovery_strategy": "discard_and_replan",
            "salvage_policy": "explicit_salvage_required",
            # Temporary compatibility for older console code that read `scope`.
            "scope": {
                "scope_phase": scope_phase,
                "required_paths": required,
                "forbidden_paths": offending,
                "recommended_action": recommended_action,
            },
        },
    )


class _MonitorRebaseRecoveryError(RuntimeError):
    """Raised when monitor-driven rebase recovery cannot update the PR branch."""


def _get_active_recovery_payload(workspace: Any) -> dict[str, Any] | None:
    """Return the active monitor/operator recovery payload (or ``None``).

    Recovery operations use a pending/running operation with ``recovery_mode``
    set. Validate-only recovery is recorded as ``validate``; public operator
    rebase requests are recorded as ``rebase`` but run through the same
    rebase-only executor path.
    """
    operations = getattr(workspace, "operations", None) or []
    for operation in operations:
        if operation.status not in _RECOVERY_ACTIVE_OPERATION_STATUSES:
            continue
        operation_type = getattr(operation, "type", None)
        if operation_type not in {
            OperationType.validate.value,
            OperationType.rebase.value,
        }:
            continue
        payload = operation.payload
        if not _is_validate_only_recovery_payload(payload):
            continue
        recovery_payload = cast(dict[str, Any], payload)
        if (
            operation_type == OperationType.rebase.value
            and recovery_payload.get("recovery_mode") != "rebase_only"
        ):
            continue
        return recovery_payload
    return None


def _is_validate_only_recovery_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    return (
        payload.get("source") in _VALIDATE_ONLY_RECOVERY_SOURCES
        and payload.get("recovery_mode") in _VALIDATE_ONLY_RECOVERY_MODES
    )


def _planning_validation_handoff_from_recovery_payload(
    *,
    workspace_id: str,
    profile: WorkspaceProfile,
    recovery_payload: Mapping[str, Any],
) -> _PlanningValidationHandoff | None:
    conformance_payload = recovery_payload.get("conformance")
    conformance = conformance_payload if isinstance(conformance_payload, Mapping) else {}
    reason_code = _recovery_conformance_reason_code(recovery_payload, conformance)
    if reason_code != CONFORMANCE_REQUIRES_AWF_VALIDATION:
        return None
    try:
        plan_path = render_workspace_path(
            _recovery_conformance_path(
                conformance,
                key="plan_path",
                fallback=profile.planning.plan_path,
            ),
            workspace_id=workspace_id,
        )
        report_path = render_workspace_path(
            _recovery_conformance_path(
                conformance,
                key="report_path",
                fallback=profile.planning.conformance_report_path,
            ),
            workspace_id=workspace_id,
        )
    except ValueError:
        plan_path = render_workspace_path(
            profile.planning.plan_path,
            workspace_id=workspace_id,
        )
        report_path = render_workspace_path(
            profile.planning.conformance_report_path,
            workspace_id=workspace_id,
        )
    gaps = _recovery_conformance_gaps(conformance)
    summary_value = conformance.get("summary")
    summary = (
        summary_value.strip()
        if isinstance(summary_value, str) and summary_value.strip()
        else "Conformance requires AWF-owned validation evidence."
    )
    iteration = _int_or_none(conformance.get("iteration"))
    if iteration is None:
        iteration = _int_or_none(recovery_payload.get("iteration"))
    max_iterations = _int_or_none(conformance.get("max_iterations"))
    if max_iterations is None:
        max_iterations = _int_or_none(recovery_payload.get("max_iterations"))
    return _PlanningValidationHandoff(
        report=PlanConformanceReport(
            status=PlanConformanceStatus.needs_iteration,
            summary=summary,
            gaps=gaps or ("AWF-owned validation evidence is missing or stale.",),
            reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
        ),
        plan_path=plan_path,
        report_path=report_path,
        iteration=iteration if iteration is not None else 0,
        max_iterations=(
            max_iterations if max_iterations is not None else profile.planning.max_iterations
        ),
    )


def _recovery_conformance_reason_code(
    recovery_payload: Mapping[str, Any],
    conformance: Mapping[str, Any],
) -> str | None:
    for value in (
        conformance.get("report_reason_code"),
        conformance.get("reason_code"),
        recovery_payload.get("conformance_reason_code"),
        recovery_payload.get("conformance_handoff_reason_code"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip().upper().replace("-", "_")
    return None


def _recovery_conformance_path(
    conformance: Mapping[str, Any],
    *,
    key: str,
    fallback: str,
) -> str:
    value = conformance.get(key)
    return value if isinstance(value, str) and value.strip() else fallback


def _recovery_conformance_gaps(conformance: Mapping[str, Any]) -> tuple[str, ...]:
    value = conformance.get("gaps")
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, str) and value.strip():
        return (value.strip(),)
    return ()


def _recovery_needs_existing_pr_push(
    recovery_payload: Mapping[str, Any],
    *,
    validated_workspace_head_sha: str | None,
    rebase_recovery_result: _RebaseRecoveryResult | None,
) -> bool:
    """Return true when recovery produced local commits that are not on the PR yet."""
    if not validated_workspace_head_sha:
        return False
    validated_head = validated_workspace_head_sha.strip()
    if not validated_head:
        return False
    recovery_mode = recovery_payload.get("recovery_mode")
    if recovery_mode == "validate_only":
        if rebase_recovery_result is not None:
            return False
        source_head_sha = recovery_payload.get("source_head_sha")
        if not isinstance(source_head_sha, str) or not source_head_sha.strip():
            return False
        return validated_head != source_head_sha.strip()
    if recovery_mode == "rebase_only":
        if rebase_recovery_result is None:
            return False
        # Push only when AWF-owned work, such as validation fixes or a
        # conformance report commit, advanced HEAD past the pushed rebase commit.
        return validated_head != rebase_recovery_result.head_sha
    return False


def _is_callback_terminal_status(status: str) -> bool:
    try:
        workspace_status = WorkspaceStatus(status)
    except ValueError:  # pragma: no cover - defensive for legacy bad rows
        return False
    return WorkspaceStateMachine.is_callback_terminal(workspace_status)


def _rebase_recovery_operation_payload_identities(
    recovery_payload: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    recovery_payload_dict = dict(recovery_payload)
    identity: dict[str, Any] = {
        key: recovery_payload_dict[key]
        for key in _REBASE_RECOVERY_OPERATION_IDENTITY_KEYS
        if key in recovery_payload_dict
    }
    identity.setdefault("recovery_mode", "rebase_only")
    if "source" in identity:
        return (identity,)
    return tuple(
        {**identity, "source": source} for source in sorted(_VALIDATE_ONLY_RECOVERY_SOURCES)
    )


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _supply_chain_block_message(findings: Sequence[SupplyChainFinding]) -> str:
    blocking = [finding for finding in findings if finding.severity == "blocking"]
    if not blocking:
        return "Supply-chain policy blocked workspace output."
    lines = ["Supply-chain policy blocked workspace output:"]
    for finding in blocking[:5]:
        guidance = finding.details.get("recovery_guidance")
        subject = f" ({finding.subject_path})" if finding.subject_path else ""
        lines.append(f"- {finding.reason_code}{subject}: {finding.explanation}")
        if isinstance(guidance, str) and guidance:
            lines.append(f"  Recovery: {guidance}")
    if len(blocking) > 5:
        lines.append(f"- {len(blocking) - 5} additional blocking finding(s).")
    return "\n".join(lines)


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _metadata_str(metadata: Mapping[str, object], key: str) -> str | None:
    value = metadata.get(key)
    return value if isinstance(value, str) else None


def _metadata_int(metadata: Mapping[str, object], key: str) -> int | None:
    value = metadata.get(key)
    return value if isinstance(value, int) else None


def _setup_dependency_network_details(first_fail: object | None) -> dict[str, Any] | None:
    metadata = getattr(first_fail, "metadata", None)
    if not isinstance(metadata, Mapping):
        return None
    details = metadata.get(SETUP_DEPENDENCY_NETWORK_METADATA_KEY)
    if not isinstance(details, Mapping):
        return None
    return cast(dict[str, Any], redact_audit_value(dict(details)))


def _setup_dependency_network_failure_details(
    first_fail: object | None,
) -> dict[str, Any] | None:
    if getattr(first_fail, "reason_code", None) != SETUP_DEPENDENCY_NETWORK_FAILURE:
        return None
    return _setup_dependency_network_details(first_fail)


def _setup_dependency_network_event_payload(
    details: Mapping[str, Any],
    *,
    reason_code: str,
) -> dict[str, Any]:
    payload = dict(details)
    payload["reason_code"] = reason_code
    # This identifies the transient setup-dependency classifier that caused the
    # retry event. When retry_exhausted=false and recovered=false, the command
    # can still terminate on a later deterministic setup failure.
    payload["failure_reason_code"] = SETUP_DEPENDENCY_NETWORK_FAILURE
    return payload


def _metadata_number(metadata: Mapping[str, object], key: str) -> int | float | None:
    value = metadata.get(key)
    return value if isinstance(value, int | float) else None


@dataclass(frozen=True)
class ExecutorConfig:
    """Config for WorkspaceExecutor. All paths are host-absolute."""

    worktrees_root: Path
    """Parent dir containing one subdir per workspace (``<root>/<workspace_id>``)."""

    compose_projects_root: Path
    """Where per-workspace compose.yml was rendered by the Provisioner."""

    default_models: Mapping[AgentRuntime, str] | None = None
    """Legacy model-only overrides. Prefer ``agent_defaults`` for new code."""

    agent_defaults: Mapping[AgentRuntime, AgentDefaults] = DEFAULT_AGENT_DEFAULTS
    """Default model and effort policy for each agent runtime."""

    agent_wall_timeout_seconds: float = DEFAULT_AGENT_WALL_TIMEOUT_SECONDS
    """Maximum wall-clock seconds for one agent CLI run. Default: 7200 seconds."""

    agent_idle_timeout_seconds: float = DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS
    """Maximum seconds with no agent stdout/stderr. Default: 3600 seconds."""

    max_validation_fix_passes: int = 5
    """Maximum fix attempts on validation failure. After the initial agent
    run + validation, if validation fails, the executor re-invokes the
    coding CLI with a fix prompt (failing command + stdout/stderr tails)
    and re-validates. ``0`` disables the loop (single-shot legacy
    behaviour); the default mirrors the PR monitor's fix-cycle cap."""

    planning_max_iterations_default: int = 3
    """Default plan-conformance remediation iterations when a profile omits
    planning.max_iterations. Explicit profile values win."""


@dataclass(frozen=True)
class _ConformanceSalvageExecutionResult:
    status: str
    prompt_override: str | None = None


class WorkspaceExecutor:
    """Drives a single workspace through run → validate → push → completed."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        runner: AsyncCommandRunner,
        compose: ComposeManager,
        validation: ValidationRunner,
        pr_creator: PullRequestCreator,
        config: ExecutorConfig,
        pr_monitor: _MonitorRunnerProto | None = None,
        pr_monitor_factory: Callable[..., _MonitorRunnerProto] | None = None,
        log_store: LogStore | None = None,
    ) -> None:
        """``pr_monitor`` and ``pr_monitor_factory`` are mutually exclusive
        optional hooks that wire the ``monitoring_pr`` stage:

        * ``pr_monitor`` — a pre-constructed monitor. Used by tests that
          hand in a stub (the production monitor needs the per-task agent
          adapter, which the executor only has mid-``execute``).
        * ``pr_monitor_factory`` — a callable the executor invokes AFTER
          the adapter is resolved. The service worker passes a factory
          that builds a ``PullRequestMonitorRunner`` from the adapter,
          GitHub client, worktree paths, and resolved workspace profile.
          Adapter-only factories are still accepted for older tests.

        If both are None the monitor stage is skipped and the executor
        preserves the original ``pushing → completed`` contract (the
        executor_tests no-monitor scenarios still pass)."""
        if pr_monitor is not None and pr_monitor_factory is not None:
            raise ValueError("pr_monitor and pr_monitor_factory are mutually exclusive")
        self._session_factory = session_factory
        self._runner = runner
        self._compose = compose
        self._validation = validation
        self._pr_creator = pr_creator
        self._config = config
        self._pr_monitor = pr_monitor
        self._pr_monitor_factory = pr_monitor_factory
        self._log_store = log_store

    async def _record_executor_pr_audit_event(
        self,
        workspace_id: str,
        *,
        event_type: str,
        action: str,
        outcome: str,
        reason_code: str,
        branch_name: str | None = None,
        remote_branch: str | None = None,
        pr_number: int | None = None,
        pr_url: str | None = None,
        source_head_sha: str | None = None,
        source_base_sha: str | None = None,
        operation_id: str | None = None,
        operation_type: str | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            workspace = await repo.get(workspace_id)
            if workspace is None:  # pragma: no cover - destroyed mid-flight
                return
            await self._add_executor_pr_audit_event(
                repo,
                workspace,
                event_type=event_type,
                action=action,
                outcome=outcome,
                reason_code=reason_code,
                branch_name=branch_name,
                remote_branch=remote_branch,
                pr_number=pr_number,
                pr_url=pr_url,
                source_head_sha=source_head_sha,
                source_base_sha=source_base_sha,
                operation_id=operation_id,
                operation_type=operation_type,
                evidence=evidence,
            )
            await session.commit()

    async def _add_executor_pr_audit_event(
        self,
        repo: WorkspaceRepository,
        workspace: Workspace,
        *,
        event_type: str,
        action: str,
        outcome: str,
        reason_code: str,
        branch_name: str | None = None,
        remote_branch: str | None = None,
        pr_number: int | None = None,
        pr_url: str | None = None,
        source_head_sha: str | None = None,
        source_base_sha: str | None = None,
        operation_id: str | None = None,
        operation_type: str | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        resolved_branch_name = branch_name or workspace.branch_name
        resolved_remote_branch = (
            remote_branch or workspace.remote_push_branch or workspace.branch_name
        )
        await repo.add_audit_event(
            workspace,
            event_type=event_type,
            actor=_EXECUTOR_AUDIT_ACTOR,
            action=action,
            outcome=outcome,
            reason_code=reason_code,
            operation_id=operation_id,
            operation_type=operation_type,
            pr_number=pr_number if pr_number is not None else workspace.pr_number,
            pr_url=pr_url or workspace.pr_url,
            source_head_sha=source_head_sha,
            source_base_sha=source_base_sha or workspace.base_commit,
            target_branch=workspace.branch_base,
            remote_branch=resolved_remote_branch,
            branch_name=resolved_branch_name,
            evidence=evidence,
        )

    async def _prepare_conformance_salvage_for_execution(
        self,
        *,
        workspace_id: str,
        workspace: Workspace,
        worktree_path: Path,
    ) -> _ConformanceSalvageExecutionResult | None:
        salvage = conformance_salvage_from_task_policy(workspace.task_policy)
        if salvage is None:
            return None

        patch_path_value = salvage.get("patch_path")
        expected_sha = salvage.get("patch_sha256")
        if not isinstance(patch_path_value, str) or not patch_path_value.strip():
            return await self._fail_conformance_salvage_execution(
                workspace_id=workspace_id,
                reason_code=SALVAGE_PATCH_UNAVAILABLE,
                message="conformance salvage patch path is missing",
                salvage=salvage,
            )
        if not isinstance(expected_sha, str) or not expected_sha.strip():
            return await self._fail_conformance_salvage_execution(
                workspace_id=workspace_id,
                reason_code=SALVAGE_PATCH_DIGEST_MISMATCH,
                message="conformance salvage patch digest is missing",
                salvage=salvage,
            )

        patch_path = Path(patch_path_value)
        if not patch_path.is_file():
            return await self._fail_conformance_salvage_execution(
                workspace_id=workspace_id,
                reason_code=SALVAGE_PATCH_UNAVAILABLE,
                message=f"conformance salvage patch is unavailable: {patch_path}",
                salvage=salvage,
            )

        patch_bytes = patch_path.read_bytes()
        actual_sha = hashlib.sha256(patch_bytes).hexdigest()
        if actual_sha != expected_sha:
            return await self._fail_conformance_salvage_execution(
                workspace_id=workspace_id,
                reason_code=SALVAGE_PATCH_DIGEST_MISMATCH,
                message=(
                    "conformance salvage patch digest mismatch "
                    f"(expected={expected_sha}, actual={actual_sha})"
                ),
                salvage=salvage,
            )

        async def git(args: list[str]) -> CommandResult:
            return await self._runner.run(
                [
                    "git",
                    *git_safe_directory_config_args(worktree_path),
                    "-C",
                    str(worktree_path),
                    *args,
                ]
            )

        check = await git(["apply", "--check", str(patch_path)])
        if check.ok:
            applied = await git(["apply", str(patch_path)])
            if not applied.ok:
                return await self._fail_conformance_salvage_execution(
                    workspace_id=workspace_id,
                    reason_code=SALVAGE_PATCH_APPLY_FAILED,
                    message=(
                        "conformance salvage patch passed preflight but failed to apply: "
                        f"{applied.stderr or applied.stdout}"
                    )[:2000],
                    salvage=salvage,
                )
            await self._record_conformance_salvage_event(
                workspace_id=workspace_id,
                event_type=CONFORMANCE_SALVAGE_APPLIED_EVENT_TYPE,
                reason_code=CONFORMANCE_SALVAGE_APPLIED_REASON,
                payload={
                    "conformance_salvage": salvage,
                    "patch_sha256": actual_sha,
                    "implementation_paths": salvage.get("implementation_paths", []),
                },
            )
            return _ConformanceSalvageExecutionResult(status="applied")

        agent_patch_path = self._materialize_salvage_patch_for_agent(
            worktree_path=worktree_path,
            patch_path=patch_path,
            patch_bytes=patch_bytes,
        )
        apply_error = (check.stderr or check.stdout or "git apply --check failed").strip()
        await self._record_conformance_salvage_event(
            workspace_id=workspace_id,
            event_type=CONFORMANCE_SALVAGE_CONFLICT_EVENT_TYPE,
            reason_code=CONFORMANCE_SALVAGE_CONFLICT_REASON,
            payload={
                "conformance_salvage": salvage,
                "agent_patch_path": agent_patch_path,
                "apply_error": apply_error[:2000],
            },
        )
        return _ConformanceSalvageExecutionResult(
            status="conflict",
            prompt_override=build_conformance_salvage_conflict_prompt(
                task_prompt=workspace.task_prompt,
                salvage=salvage,
                agent_patch_path=agent_patch_path,
                apply_error=apply_error,
            ),
        )

    async def _fail_conformance_salvage_execution(
        self,
        *,
        workspace_id: str,
        reason_code: str,
        message: str,
        salvage: Mapping[str, Any],
    ) -> _ConformanceSalvageExecutionResult:
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"{reason_code}: {message}"[:2000],
            reason_code=reason_code,
            details={
                "reason_code": reason_code,
                "conformance_salvage": dict(salvage),
            },
        )
        return _ConformanceSalvageExecutionResult(status="failed")

    async def _record_conformance_salvage_event(
        self,
        *,
        workspace_id: str,
        event_type: str,
        reason_code: str,
        payload: dict[str, Any],
    ) -> None:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            workspace = await repo.get(workspace_id)
            if workspace is None:  # pragma: no cover - destroyed mid-flight
                return
            await repo.add_event(
                workspace,
                event_type=event_type,
                reason_code=reason_code,
                payload=payload,
            )
            await session.commit()

    async def _record_setup_dependency_network_events(
        self,
        *,
        workspace_id: str,
        result: ValidationResult,
    ) -> None:
        event_specs: list[tuple[str, str, dict[str, Any]]] = []
        commands = getattr(result, "commands", None)
        if not commands:
            return
        for command in commands:
            details = _setup_dependency_network_details(command)
            if details is None:
                continue
            retry_count = _metadata_int(details, "retry_count") or 0
            if retry_count > 0:
                # Exhausted attempts intentionally emit both the retry event and
                # the exhausted event from the same redacted retry metadata.
                event_specs.append(
                    (
                        SETUP_DEPENDENCY_NETWORK_RETRY_EVENT_TYPE,
                        SETUP_DEPENDENCY_NETWORK_RETRY,
                        _setup_dependency_network_event_payload(
                            details,
                            reason_code=SETUP_DEPENDENCY_NETWORK_RETRY,
                        ),
                    )
                )
            if details.get("retry_exhausted") is True:
                event_specs.append(
                    (
                        SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED_EVENT_TYPE,
                        SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED,
                        _setup_dependency_network_event_payload(
                            details,
                            reason_code=SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED,
                        ),
                    )
                )
        if not event_specs:
            return

        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            workspace = await repo.get(workspace_id)
            if workspace is None:  # pragma: no cover - destroyed mid-flight
                return
            for event_type, reason_code, payload in event_specs:
                await repo.add_event(
                    workspace,
                    event_type=event_type,
                    reason_code=reason_code,
                    payload=payload,
                )
            await session.commit()

    def _materialize_salvage_patch_for_agent(
        self,
        *,
        worktree_path: Path,
        patch_path: Path,
        patch_bytes: bytes,
    ) -> str:
        relative_path = Path(".awf") / "salvage" / patch_path.name
        agent_patch_path = worktree_path / relative_path
        agent_patch_path.parent.mkdir(parents=True, exist_ok=True)
        agent_patch_path.write_bytes(patch_bytes)
        self._exclude_agent_salvage_artifacts(worktree_path)
        return relative_path.as_posix()

    def _exclude_agent_salvage_artifacts(self, worktree_path: Path) -> None:
        git_dir_file = worktree_path / ".git"
        exclude_path = worktree_path / ".git" / "info" / "exclude"
        if git_dir_file.is_file():
            content = git_dir_file.read_text(encoding="utf-8", errors="replace").strip()
            prefix = "gitdir:"
            if content.startswith(prefix):
                git_dir = Path(content[len(prefix) :].strip())
                if not git_dir.is_absolute():
                    git_dir = (worktree_path / git_dir).resolve()
                exclude_path = git_dir / "info" / "exclude"
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        pattern = "/.awf/salvage/"
        if pattern not in existing.splitlines():
            suffix = "" if existing.endswith("\n") or not existing else "\n"
            exclude_path.write_text(f"{existing}{suffix}{pattern}\n", encoding="utf-8")

    async def _handoff_sync_feature_pr_monitor(
        self,
        *,
        workspace_id: str,
        workspace: Workspace,
        compose_project: str,
        compose_file: Path,
        worktree_path: Path,
    ) -> None:
        metadata = _sync_feature_pr_adoption_metadata(workspace)
        missing = _missing_sync_feature_pr_adoption_metadata(workspace, metadata)
        if missing:
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.running,
                failure_reason=FailureReason.infrastructure_failure,
                message=_sync_feature_pr_missing_metadata_message(missing),
                reason_code=_PR_ADOPTION_METADATA_MISSING_REASON_CODE,
                details={"missing": missing},
            )
            return

        if not await self._ensure_worktree_available(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            expected=WorkspaceStatus.running,
            action="sync_feature_pr_handoff",
        ):
            return

        monitor: _MonitorRunnerProto | None = self._pr_monitor
        try:
            if monitor is None and self._pr_monitor_factory is not None:
                agent = AgentRuntime(workspace.agent)
                defaults = self._defaults_for(agent)
                adapter_defaults = _agent_defaults_for_workspace(workspace, defaults)
                adapter = get_adapter(
                    agent,
                    runner=self._runner,
                    defaults=adapter_defaults,
                    log_store=self._log_store,
                    agent_wall_timeout_seconds=self._config.agent_wall_timeout_seconds,
                    agent_idle_timeout_seconds=self._config.agent_idle_timeout_seconds,
                )
                profile = _profile_for_workspace(
                    workspace,
                    worktree_path=worktree_path,
                    planning_max_iterations_default=(self._config.planning_max_iterations_default),
                )
                monitor = _call_pr_monitor_factory(
                    self._pr_monitor_factory,
                    adapter=adapter,
                    profile=profile,
                    workspace=workspace,
                    provider_recovery_default_model=(
                        defaults.model if defaults is not None else None
                    ),
                )
        except Exception as exc:
            _log.error(
                "executor.sync_feature_pr_monitor_build_failed",
                workspace_id=workspace_id,
                redacted_traceback=_redacted_exception_traceback(exc),
            )
            safe_exception = redact_audit_text(repr(exc), limit=1900)
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.running,
                failure_reason=FailureReason.infrastructure_failure,
                message=f"adopted PR monitor handoff failed: {safe_exception}"[:2000],
                reason_code=_PR_ADOPTION_MONITOR_UNAVAILABLE_REASON_CODE,
            )
            return

        if monitor is None:
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.running,
                failure_reason=FailureReason.infrastructure_failure,
                message="adopted PR monitor handoff failed: no PR monitor configured",
                reason_code=_PR_ADOPTION_MONITOR_UNAVAILABLE_REASON_CODE,
            )
            return

        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            persisted = await repo.get(workspace_id)
            if persisted is None:  # pragma: no cover - destroyed mid-flight
                return
            if persisted.status != WorkspaceStatus.running.value:
                await self._record_stale_action_skip(
                    repo,
                    persisted,
                    action="sync_feature_pr_handoff",
                    expected=WorkspaceStatus.running,
                    reason_code="EXECUTOR_STALE_STATUS",
                )
                await session.commit()
                return

            persisted_metadata = _sync_feature_pr_adoption_metadata(persisted)
            missing = _missing_sync_feature_pr_adoption_metadata(
                persisted,
                persisted_metadata,
            )
            if missing:
                safe_message = redact_audit_text(
                    _sync_feature_pr_missing_metadata_message(missing),
                    limit=2000,
                )
                persisted.failure_reason = FailureReason.infrastructure_failure.value
                persisted.failure_message = safe_message
                await repo.transition(
                    persisted,
                    to=WorkspaceStatus.failed,
                    reason_code=_PR_ADOPTION_METADATA_MISSING_REASON_CODE,
                    payload={
                        "failure_reason": FailureReason.infrastructure_failure.value,
                        "reason_code": _PR_ADOPTION_METADATA_MISSING_REASON_CODE,
                        "message": safe_message,
                        "details": {"missing": missing},
                    },
                )
                await session.commit()
                return

            head_sha = _required_metadata_str(persisted_metadata, "head_sha")
            base_sha = _required_metadata_str(persisted_metadata, "base_sha")
            head_ref = _required_metadata_str(persisted_metadata, "head_ref")
            base_ref = _required_metadata_str(persisted_metadata, "base_ref")
            pr_url = persisted.pr_url or _required_metadata_str(
                persisted_metadata,
                "pr_url",
            )
            pr_number = persisted.pr_number or _metadata_int(
                persisted_metadata,
                "pr_number",
            )
            remote_branch = persisted.remote_push_branch or head_ref

            persisted.pr_url = pr_url
            persisted.pr_number = pr_number
            persisted.remote_push_branch = remote_branch
            persisted.monitor_last_commit_sha = head_sha
            persisted.base_commit = base_sha
            await repo.add_event(
                persisted,
                event_type=_PR_MONITOR_ADOPTED_EVENT,
                reason_code=_PR_MONITOR_ADOPTED_REASON_CODE,
                payload={
                    "pr_number": pr_number,
                    "pr_url": pr_url,
                    "head_ref": head_ref,
                    "base_ref": base_ref,
                    "head_sha": head_sha,
                    "base_sha": base_sha,
                    "remote_branch": remote_branch,
                    "source": "existing_github_pr",
                },
            )
            await repo.transition(
                persisted,
                to=WorkspaceStatus.validating,
                reason_code=_PR_ADOPTION_SKIP_AGENT_REASON_CODE,
                payload={"source": "existing_github_pr"},
            )
            await repo.transition(
                persisted,
                to=WorkspaceStatus.monitoring_pr,
                reason_code=_PR_MONITOR_ADOPTED_REASON_CODE,
                payload={
                    "pr_number": pr_number,
                    "pr_url": pr_url,
                    "head_sha": head_sha,
                    "base_sha": base_sha,
                    "source": "existing_github_pr",
                },
            )
            await session.commit()

        _log.info(
            "executor.sync_feature_pr_handoff_to_monitor",
            workspace_id=workspace_id,
            pr_url=workspace.pr_url,
        )
        if not await self._recheck_status(
            workspace_id,
            expected=WorkspaceStatus.monitoring_pr,
            action="run_pr_monitor",
        ):
            return
        await monitor.run(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
        )

    async def execute(
        self,
        workspace_id: str,
        *,
        execution_owner_id: str | None = None,
        execution_lease_expires_at: datetime | None = None,
    ) -> None:
        """Drive a ``ready`` workspace to ``completed`` (or ``failed``).

        The function is idempotent in the sense that it refuses to run on a
        workspace that is not currently in ``ready`` — useful when a poll
        loop races with a manual invocation.
        """
        ws = await self._claim_ready(
            workspace_id,
            execution_owner_id=execution_owner_id,
            execution_lease_expires_at=execution_lease_expires_at,
        )
        if ws is None:
            return
        if not await self._recheck_status(
            workspace_id,
            expected=WorkspaceStatus.running,
            action="execute",
        ):
            return

        compose_file = (
            Path(ws.compose_file_path)
            if ws.compose_file_path
            else self._config.compose_projects_root / workspace_id / "compose.yml"
        )
        compose_project = ws.compose_project_name or f"awf_{workspace_id}"
        worktree_path = self._config.worktrees_root / workspace_id

        # When the PR monitor's RECOVERY_DISPATCH path delivered this
        # workspace, the executor must NOT re-run planning, the agent
        # CLI, or any post-agent commit hooks — those would rewrite the
        # plan artifact and re-implement the feature mid-merge. Recovery
        # only re-runs validation against the already-pushed work.
        recovery = _get_active_recovery_payload(ws)
        if recovery is None:
            guard_result = await self._block_open_pr_reexecution_without_recovery(
                workspace_id=workspace_id,
            )
            if guard_result.blocked:
                return
            recovery = guard_result.recovery

        if ws.task_kind == "sync_feature_pr" and recovery is None:
            await self._handoff_sync_feature_pr_monitor(
                workspace_id=workspace_id,
                workspace=ws,
                compose_project=compose_project,
                compose_file=compose_file,
                worktree_path=worktree_path,
            )
            return

        # ── Step 1: agent CLI runs the task inside the container ────────────
        if recovery is None:
            salvage_result = await self._prepare_conformance_salvage_for_execution(
                workspace_id=workspace_id,
                workspace=ws,
                worktree_path=worktree_path,
            )
            if salvage_result is not None:
                if salvage_result.status == "failed":
                    return
                if salvage_result.prompt_override is not None:
                    ws.task_prompt = salvage_result.prompt_override
        rebase_recovery_result: _RebaseRecoveryResult | None = None
        baseline_coverage: ValidationCoverageResult | None = None
        profile: WorkspaceProfile | None = None
        agent_exit_note: str | None = None
        agent_run_reason_code: str | None = None
        agent_run_details: Mapping[str, Any] | None = None
        # ``agent_run_failure_reason`` is only set when the upstream cause was
        # an actual agent/provider failure (``AgentRunError``). Recovered
        # infrastructure paths (e.g. missing-HEAD recovery) leave this None so
        # downstream commit failures route through the standard infra path
        # instead of being mis-classified as agent failures and queueing
        # provider recovery.
        agent_run_failure_reason: FailureReason | None = None
        planning_validation_handoff: _PlanningValidationHandoff | None = None
        expected_branch = ws.branch_name or f"awf/{workspace_id}"
        adapter: AgentAdapter | None = None
        defaults: AgentDefaults | None = None
        default_model: str | None = None
        agent_command_evidence: list[str] = []
        try:
            agent = AgentRuntime(ws.agent)
            defaults = self._defaults_for(agent)
            adapter_defaults = _agent_defaults_for_workspace(ws, defaults)
            default_model = adapter_defaults.model if adapter_defaults is not None else None
            adapter = get_adapter(
                agent,
                runner=self._runner,
                defaults=adapter_defaults,
                log_store=self._log_store,
                agent_wall_timeout_seconds=self._config.agent_wall_timeout_seconds,
                agent_idle_timeout_seconds=self._config.agent_idle_timeout_seconds,
            )
            profile = _profile_for_workspace(
                ws,
                worktree_path=worktree_path,
                planning_max_iterations_default=(self._config.planning_max_iterations_default),
            )
            setup_result = await self._validation.run_profile_phases(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                profile=profile,
                phase_names=("setup", "pre_agent"),
                worktree_path=worktree_path,
            )
            try:
                await self._record_setup_dependency_network_events(
                    workspace_id=workspace_id,
                    result=setup_result,
                )
            except Exception:
                _log.exception(
                    "executor.setup_dependency_network_event_record_failed",
                    workspace_id=workspace_id,
                    setup_all_passed=setup_result.all_passed,
                )
            if not setup_result.all_passed:
                first_fail = setup_result.first_failure
                setup_dependency_details = _setup_dependency_network_failure_details(first_fail)
                setup_failure_reason_code = (
                    SETUP_DEPENDENCY_NETWORK_FAILURE
                    if setup_dependency_details is not None
                    else None
                )
                if recovery is not None:
                    recovery_setup_failure_reason_code = (
                        setup_failure_reason_code or "MONITOR_RECOVERY_SETUP_FAILED"
                    )
                    await self._finish_active_recovery_operations(
                        workspace_id=workspace_id,
                        status=OperationStatus.failed,
                        reason_code=recovery_setup_failure_reason_code,
                        error_message=(
                            f"profile setup failed: {first_fail.command}"
                            if first_fail is not None
                            else "profile setup failed"
                        )[:2000],
                    )
                await self._mark_failed(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.running,
                    failure_reason=_failure_reason_for_phase(first_fail),
                    message=(
                        f"profile setup failed: {first_fail.command}"
                        if first_fail is not None
                        else "profile setup failed"
                    )[:2000],
                    reason_code=setup_failure_reason_code,
                    details=setup_dependency_details,
                )
                return
            profile_preflight = getattr(self._validation, "run_profile_tool_preflight", None)
            profile_preflight_result = (
                await profile_preflight(workspace_id=workspace_id, profile=profile)
                if callable(profile_preflight)
                else ValidationResult()
            )
            if not profile_preflight_result.all_passed:
                first_fail = profile_preflight_result.first_failure
                if recovery is not None:
                    await self._finish_active_recovery_operations(
                        workspace_id=workspace_id,
                        status=OperationStatus.failed,
                        reason_code="MONITOR_RECOVERY_PROFILE_PREFLIGHT_FAILED",
                        error_message=(
                            f"profile preflight failed: {first_fail.command}"
                            if first_fail is not None
                            else "profile preflight failed"
                        )[:2000],
                    )
                await self._mark_failed(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.running,
                    failure_reason=_failure_reason_for_phase(first_fail),
                    message=(
                        f"profile preflight failed: {first_fail.command}"
                        if first_fail is not None
                        else "profile preflight failed"
                    )[:2000],
                )
                return
            if recovery is None:
                if not await self._run_agent_git_writability_preflight(
                    workspace_id=workspace_id,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    worktree_path=worktree_path,
                ):
                    return
                baseline_coverage = await self._run_baseline_coverage_preflight(
                    workspace_id=workspace_id,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    profile=profile,
                )
                if not await self._recheck_status(
                    workspace_id,
                    expected=WorkspaceStatus.running,
                    action="agent_run",
                ):
                    return
                planning_failure = await self._run_agent_task_with_optional_planning(
                    adapter=adapter,
                    workspace=ws,
                    profile=profile,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    worktree_path=worktree_path,
                    model=default_model,
                    command_evidence=agent_command_evidence,
                )
                if isinstance(planning_failure, _PlanningValidationHandoff):
                    planning_validation_handoff = planning_failure
                    await self._record_planning_validation_handoff_event(
                        workspace_id=workspace_id,
                        handoff=planning_failure,
                    )
                elif planning_failure is not None:
                    failure_message = (
                        planning_failure
                        if isinstance(planning_failure, str)
                        else planning_failure.message
                    )
                    reason_code = (
                        None if isinstance(planning_failure, str) else planning_failure.reason_code
                    )
                    details = (
                        None if isinstance(planning_failure, str) else planning_failure.details
                    )
                    await self._mark_failed(
                        workspace_id=workspace_id,
                        from_status=WorkspaceStatus.running,
                        failure_reason=FailureReason.agent_failure,
                        message=failure_message[:2000],
                        reason_code=reason_code,
                        details=details,
                        salvage=_failure_salvage_payload(ws, worktree_path=worktree_path),
                    )
                    if (
                        isinstance(planning_failure, _PlanningRunFailure)
                        and planning_failure.reason_code == AGENT_PLAN_PHASE_SCOPE_VIOLATION
                    ):
                        await self._auto_retry_planning_scope_failure(
                            workspace_id=workspace_id,
                            failure=planning_failure,
                        )
                    return
            else:
                # Recovery dispatch created the validate Operation in ``pending``;
                # flush it to ``running`` before validation so observability
                # tooling sees a real ``started_at`` (otherwise the row jumps
                # straight from pending → succeeded/failed when the validate
                # finalizer fires, with started_at == finished_at).
                await self._start_pending_recovery_operations(
                    workspace_id=workspace_id,
                )
                _log.info(
                    "executor.validate_only_recovery_started",
                    workspace_id=workspace_id,
                    source=recovery.get("source"),
                    recovery_mode=recovery.get("recovery_mode"),
                    reason=recovery.get("reason"),
                )
                planning_validation_handoff = _planning_validation_handoff_from_recovery_payload(
                    workspace_id=workspace_id,
                    profile=profile,
                    recovery_payload=recovery,
                )
        except ComposeExecCleanupError as exc:
            _log.error(
                "executor.exec_process_cleanup_failed",
                workspace_id=workspace_id,
                source=exc.source,
                label=exc.label,
                invocation_id=exc.invocation_id,
                reason_code=exc.reason_code,
            )
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.running,
                failure_reason=FailureReason.infrastructure_failure,
                message=cleanup_failure_message(exc),
                reason_code=EXEC_PROCESS_CLEANUP_FAILED,
            )
            return
        except AgentRunError as exc:
            append_command_evidence(
                agent_command_evidence,
                stdout=exc.result.stdout,
                stderr=exc.result.stderr,
            )
            # Do NOT bail out yet. A CLI that exits non-zero — typically
            # ``claude_code`` hitting a 1-hour internal session cap and
            # returning 137 (SIGKILL), or a timeout against a flaky
            # dependency — may have left valuable uncommitted work in the
            # worktree. Coding CLIs in general don't commit on their own;
            # AWF's post-agent auto-commit is the only thing that captures
            # their edits. Log the exit code, remember it for the final
            # failure message, but let the commit + validate pipeline run.
            # If there's nothing to commit, the existing no-work check
            # fails the workspace with ``agent_failure`` below. If there
            # IS work, validation decides whether it's pushable.
            # Structured provider-failure metadata is preserved in
            # ``agent_run_details``. If salvage finds no commits, the
            # no-work failure path below persists that metadata before
            # preparing the authorized provider retry/fallback workspace.
            agent_exit_note = (
                f"agent CLI exited {exc.result.returncode} ({exc.reason_code}); "
                f"continuing to salvage any uncommitted work"
            )
            agent_run_reason_code = exc.reason_code
            agent_run_details = getattr(exc, "details", None)
            agent_run_failure_reason = FailureReason.agent_failure
            _log.warning(
                "executor.agent_nonzero_exit_salvaging",
                workspace_id=workspace_id,
                agent=ws.agent,
                returncode=exc.result.returncode,
                reason_code=exc.reason_code,
            )
            await self._repair_agent_git_ownership(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                reason="agent_nonzero_exit_salvage",
            )
        except Exception as exc:  # unexpected — surface with generic reason
            if _git_error_indicates_missing_head_object(str(exc)):
                if await self._recover_missing_git_head_or_mark_failed(
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    base_commit=ws.base_commit,
                    branch_name=expected_branch,
                    from_status=WorkspaceStatus.running,
                    stage="agent_run",
                    error=exc,
                ):
                    agent_exit_note = (
                        "AWF recovered a missing Git HEAD object during the agent run; "
                        "continuing to salvage filesystem work"
                    )
                    agent_run_reason_code = GIT_OBJECT_MISSING_RECOVERED_REASON_CODE
                    agent_run_details = {"recovered_stage": "agent_run"}
                else:
                    return
            else:
                _log.exception("executor.unexpected_in_agent", workspace_id=workspace_id)
                await self._mark_failed(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.running,
                    failure_reason=FailureReason.infrastructure_failure,
                    message=f"unexpected error during agent run: {exc!r}"[:2000],
                )
                return
        if adapter is None:
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.running,
                failure_reason=FailureReason.infrastructure_failure,
                message="executor could not initialize agent adapter before post-agent capture",
            )
            return

        # ── Step 1b: capture the agent's work as a commit on the feature branch ──
        if not await self._recheck_status(
            workspace_id,
            expected=WorkspaceStatus.running,
            action="post_agent_commit",
        ):
            return

        # Coding CLIs make file edits reliably but are inconsistent about git:
        # some commit, some leave changes unstaged, some commit partial subsets
        # and leave the rest dirty. AWF normalizes: after the agent exits, we
        # stage everything and commit if anything's cached. If HEAD still
        # matches the base branch afterwards, the agent produced zero change
        # and we fail with a specific reason rather than pushing nothing.
        if not await self._ensure_worktree_available(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            expected=WorkspaceStatus.running,
            action="post_agent_commit",
        ):
            return

        # ``base_commit`` is set by the provisioner before a workspace ever
        # reaches ``ready`` — if it's missing here something went wrong
        # upstream and every ``rev-list``/``merge-base`` below would
        # inject the literal string "None" into a git command. Fail
        # cleanly instead of passing "None..HEAD" to git.
        if ws.base_commit is None:
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.running,
                failure_reason=FailureReason.infrastructure_failure,
                message=(
                    "workspace has no base_commit — provisioning must set "
                    "this before the agent run; cannot verify feature-branch "
                    "commits without it"
                ),
            )
            return
        base_commit: str = ws.base_commit

        async def _git_in_worktree(args: list[str]):  # type: ignore[no-untyped-def]
            return await self._runner.run(
                [
                    "git",
                    *git_safe_directory_config_args(worktree_path),
                    "-C",
                    str(worktree_path),
                    *args,
                ]
            )

        has_known_non_plan_output = False

        try:
            if recovery is None:
                # ── Branch-drift recovery ──────────────────────────────────
                # Agent CLIs (Claude Code, Codex) sometimes run
                # ``git checkout -b <descriptive-name>`` mid-session as part
                # of "good git hygiene" — they don't know AWF already
                # created the right branch for them. If they commit on the
                # drifted branch, ``pr_creator.push_and_open`` pushes the
                # original (empty) AWF branch to origin and ``gh pr create``
                # fails with "No commits between development and awf/ws_...".
                #
                # Incident 2026-04-24 (T41 Phase 3, ws_9ca6134a): agent
                # switched to ``awf/t41-phase3-github-app-install-flow``
                # and committed there. AWF's push of the empty
                # ``awf/ws_9ca6134a...`` created a no-op PR. Agent's work
                # stranded in the worktree.
                #
                # Recovery: if HEAD's branch diverged, fast-forward the
                # expected branch to the agent's tip. Both branches share
                # the same base commit (the worktree was created fresh
                # from origin/<base>), so this is a safe pointer update.
                current_branch_r = await _git_in_worktree(["rev-parse", "--abbrev-ref", "HEAD"])
                # If rev-parse itself failed (corrupted git state, missing
                # HEAD, etc.) we can't reliably detect drift. Fail loudly
                # rather than silently skip — a working tree that can't
                # resolve HEAD is broken enough that continuing to push
                # would produce nonsense anyway.
                if not current_branch_r.ok:
                    raise RuntimeError(
                        "branch drift check: ``git rev-parse --abbrev-ref HEAD`` "
                        f"failed with exit {current_branch_r.returncode}: "
                        f"{current_branch_r.stderr!r}"
                    )
                current_branch = (current_branch_r.stdout or "").strip()
                if current_branch and current_branch != expected_branch:
                    _log.warning(
                        "executor.branch_drift_detected",
                        workspace_id=workspace_id,
                        current_branch=current_branch,
                        expected_branch=expected_branch,
                    )
                    agent_head_r = await _git_in_worktree(["rev-parse", "HEAD"])
                    agent_head = (agent_head_r.stdout or "").strip()
                    if not agent_head_r.ok or not agent_head:
                        raise RuntimeError(
                            f"branch drift detected (current={current_branch} "
                            f"expected={expected_branch}) but agent HEAD could not "
                            f"be resolved: {agent_head_r.stderr!r}"
                        )
                    # Preserve uncommitted changes. The executor's core
                    # contract is "salvage whatever the agent left on
                    # disk" — including edits not yet committed on the
                    # drifted branch. ``status --porcelain`` with
                    # ``--untracked-files=all`` catches both staged,
                    # unstaged, and untracked files. If any exist, stash
                    # them (with ``-u`` to include untracked) before the
                    # ``switch`` so they don't get lost. Pop after the
                    # fast-forward so they end up on top of the agent's
                    # commits on the expected branch.
                    status_r = await _git_in_worktree(
                        ["status", "--porcelain=v1", "--untracked-files=all"]
                    )
                    if not status_r.ok:
                        raise RuntimeError(
                            f"branch drift recovery: ``git status`` failed: {status_r.stderr!r}"
                        )
                    has_wip = bool(status_r.stdout.strip())
                    stash_created = False
                    if has_wip:
                        stash_r = await _git_in_worktree(
                            [
                                "stash",
                                "push",
                                "--include-untracked",
                                "--message",
                                f"awf-drift-recovery-{workspace_id}",
                            ]
                        )
                        if not stash_r.ok:
                            raise RuntimeError(
                                f"branch drift recovery: ``git stash push`` failed: "
                                f"{stash_r.stderr!r} (refusing to switch with dirty "
                                f"worktree that couldn't be stashed)"
                            )
                        stash_created = True

                    # Switch to the expected branch. It should exist
                    # locally — AWF created it at worktree-add time.
                    switch_r = await _git_in_worktree(["switch", expected_branch])
                    if not switch_r.ok:
                        # Best-effort: try to restore stashed WIP so it's
                        # not silently lost before bailing out.
                        if stash_created:
                            await _git_in_worktree(["stash", "pop"])
                        raise RuntimeError(
                            f"branch drift recovery: could not switch back to "
                            f"{expected_branch}: {switch_r.stderr!r}"
                        )
                    # Fast-forward the expected branch to the agent's tip
                    # using ``merge --ff-only``. The two branches share
                    # the same base (AWF created the worktree fresh from
                    # ``origin/<base>``) and the agent only added commits
                    # on top, so ff must succeed. ``merge --ff-only`` over
                    # ``reset --hard`` because the latter would also wipe
                    # any WIP the user has in the working tree if the
                    # stash step above silently did nothing (e.g. if
                    # ``status`` missed an edge case).
                    merge_r = await _git_in_worktree(["merge", "--ff-only", agent_head])
                    if not merge_r.ok:
                        if stash_created:
                            await _git_in_worktree(["stash", "pop"])
                        raise RuntimeError(
                            f"branch drift recovery: ``merge --ff-only "
                            f"{agent_head[:10]}`` failed: {merge_r.stderr!r}"
                        )

                    if stash_created:
                        pop_r = await _git_in_worktree(["stash", "pop"])
                        if not pop_r.ok:
                            # A pop conflict means the agent's WIP and the
                            # fast-forwarded commits touch the same
                            # regions. That's a real problem for the
                            # workspace — the WIP is left in the stash
                            # under a named entry, but we can't auto-merge
                            # it. Fail loudly so the operator knows to
                            # inspect.
                            raise RuntimeError(
                                f"branch drift recovery: ``git stash pop`` failed "
                                f"(WIP conflicts with recovered commits): "
                                f"{pop_r.stderr!r}"
                            )

                    _log.info(
                        "executor.branch_drift_recovered",
                        workspace_id=workspace_id,
                        recovered_from=current_branch,
                        recovered_to=expected_branch,
                        head_sha=agent_head,
                        wip_stashed=has_wip,
                    )

                add_result = await _git_in_worktree(["add", "-A"])
                await self._repair_agent_git_ownership(
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    reason="post_agent_git_add",
                )
                if not add_result.ok:
                    raise _PostAgentCommitStepError(
                        stage="git add",
                        result=add_result,
                        classification=None,
                    )
                cached = await _git_in_worktree(["diff", "--cached", "--name-only"])
                staged_paths = _git_name_lines(cached.stdout) if cached.stdout.strip() else []
                supply_chain_result = await self._refresh_supply_chain_policy_for_workspace(
                    workspace_id=workspace_id,
                    command_evidence=agent_command_evidence,
                    changed_paths=staged_paths,
                )
                if supply_chain_result.policy_blocked:
                    await self._mark_failed(
                        workspace_id=workspace_id,
                        from_status=WorkspaceStatus.running,
                        failure_reason=FailureReason.policy_failure,
                        reason_code="SUPPLY_CHAIN_POLICY_BLOCKED",
                        message=_supply_chain_block_message(supply_chain_result.findings)[:2000],
                    )
                    return
                if staged_paths:
                    staged_paths_are_plan_only = changed_paths_are_only_internal_plan_artifacts(
                        staged_paths
                    )
                    if staged_paths_are_plan_only:
                        committed_paths = sorted(
                            path.as_posix()
                            for path in await self._committed_paths_since(
                                worktree_path, base_commit
                            )
                        )
                        committed_output_is_plan_only = (
                            not committed_paths
                            or changed_paths_are_only_internal_plan_artifacts(committed_paths)
                        )
                        if committed_output_is_plan_only and await self._fail_if_plan_only_paths(
                            workspace_id=workspace_id,
                            changed_paths=staged_paths,
                            expected_status=WorkspaceStatus.running,
                        ):
                            return
                    has_known_non_plan_output = True
                    protected_file_diffs = await self._protected_file_diffs_for_staged_paths(
                        worktree_path=worktree_path,
                        changed_paths=staged_paths,
                    )
                    violations = find_protected_quality_gate_changes(
                        changed_paths=staged_paths,
                        owned_paths=list(ws.owned_paths),
                        protected_file_diffs=protected_file_diffs,
                    )
                    if violations:
                        await self._mark_failed(
                            workspace_id=workspace_id,
                            from_status=WorkspaceStatus.running,
                            failure_reason=FailureReason.policy_failure,
                            reason_code="QUALITY_GATE_POLICY_CHANGED",
                            message=quality_gate_violation_message(violations)[:2000],
                        )
                        return
                    commit_msg = f"awf: {ws.task_title}"[:72]
                    commit_body = f"Authored by AWF workspace {workspace_id} (agent: {ws.agent}).\n"

                    async def _run_commit() -> CommandResult:
                        return await self._runner.run(
                            [
                                "git",
                                *git_safe_directory_config_args(worktree_path),
                                "-C",
                                str(worktree_path),
                                *git_identity_config_args(),
                                "commit",
                                "-m",
                                commit_msg,
                                "-m",
                                commit_body,
                            ],
                        )

                    commit_result = await _run_commit()
                    await self._repair_agent_git_ownership(
                        workspace_id=workspace_id,
                        worktree_path=worktree_path,
                        reason="post_agent_git_commit",
                    )
                    if not commit_result.ok:
                        classification = _classify_post_agent_commit_failure(commit_result)
                        if classification.repair_strategy in {"deterministic", "agent"}:
                            await self._run_post_agent_commit_repair(
                                workspace_id=workspace_id,
                                worktree_path=worktree_path,
                                commit_result=commit_result,
                                classification=classification,
                                staged_paths=staged_paths,
                                run_commit=_run_commit,
                                git_in_worktree=_git_in_worktree,
                                adapter=adapter,
                                compose_project=compose_project,
                                compose_file=compose_file,
                                model=default_model,
                                allow_agent_repair=agent_run_failure_reason is None,
                                ws=ws,
                                command_evidence=agent_command_evidence,
                            )
                        else:
                            raise _PostAgentCommitStepError(
                                stage="git commit",
                                result=commit_result,
                                classification=classification,
                                format_repair_attempted=False,
                            )
                # Regardless of whether we just committed, verify HEAD has advanced
                # past the base commit. If not, the agent produced no change.
                rev_count = await _git_in_worktree(["rev-list", "--count", f"{base_commit}..HEAD"])
                if not rev_count.ok or int(rev_count.stdout.strip() or "0") == 0:
                    base_short = base_commit[:10] if base_commit else "unknown"
                    message = (
                        f"agent exited without producing any commits on the feature branch "
                        f"(base={base_short})"
                    )
                    if agent_exit_note is not None:
                        message = f"{message}; {agent_exit_note}"

                    # Provider recovery reads the failed state event, so
                    # persist the structured reason/details first. The
                    # recovery service creates an authorized delayed retry
                    # or fallback workspace and no-ops for ordinary agent
                    # failures.
                    #
                    # Gate provider recovery on
                    # ``agent_run_failure_reason == agent_failure`` rather
                    # than on ``agent_run_reason_code is not None``. The
                    # recovered missing-HEAD path also populates
                    # ``agent_run_reason_code`` (with
                    # ``GIT_OBJECT_MISSING_RECOVERED``) but its upstream
                    # cause is infrastructure recovery, not a provider
                    # failure that warrants a delayed retry.
                    await self._mark_failed(
                        workspace_id=workspace_id,
                        from_status=WorkspaceStatus.running,
                        failure_reason=FailureReason.agent_failure,
                        message=message,
                        reason_code=agent_run_reason_code,
                        details=agent_run_details,
                    )
                    if agent_run_failure_reason == FailureReason.agent_failure:
                        await self._prepare_provider_recovery(workspace_id)
                    return

                # Some agents sever git history (e.g. by accidentally running
                # ``git checkout --orphan`` or by re-initialising the repo).
                # rev-list counts HIGH in that case (every HEAD commit is "new"
                # w.r.t. base because there's no shared ancestor), so the
                # previous check wouldn't notice. Without this guard, the push
                # succeeds but ``gh pr create`` dies with a cryptic
                # ``branch has no history in common with <base>`` error.
                #
                # Recovery: ``git reset --soft <base>`` moves HEAD to the base
                # commit while leaving the index untouched — the index still
                # reflects the orphan's tree. A fresh ``git commit`` then
                # produces a single commit on top of base that contains the
                # cumulative diff, and the branch is reattached to a valid
                # ancestry so the PR can be opened normally.
                #
                # Invariant: ``base_commit`` is always populated by
                # ``_claim_ready`` before this block runs. The ``assert`` both
                # documents and satisfies mypy.
                ancestor = await _git_in_worktree(
                    ["merge-base", "--is-ancestor", base_commit, "HEAD"]
                )
                if not ancestor.ok:
                    _log.warning(
                        "executor.orphan_history_detected",
                        workspace_id=workspace_id,
                        base_commit=base_commit,
                    )
                    reset = await _git_in_worktree(["reset", "--soft", base_commit])
                    await self._repair_agent_git_ownership(
                        workspace_id=workspace_id,
                        worktree_path=worktree_path,
                        reason="orphan_history_reset",
                    )
                    if reset.ok:
                        recovery_msg = f"awf: {ws.task_title} (recovered from orphan)"[:72]
                        recovery_body = (
                            f"AWF detected orphan history on workspace {workspace_id} "
                            f"(agent: {ws.agent}) and squashed the cumulative diff "
                            f"onto base commit {base_commit[:10]}.\n"
                        )
                        recover_commit = await self._runner.run(
                            [
                                "git",
                                *git_safe_directory_config_args(worktree_path),
                                "-C",
                                str(worktree_path),
                                *git_identity_config_args(),
                                "commit",
                                "-m",
                                recovery_msg,
                                "-m",
                                recovery_body,
                            ],
                        )
                        await self._repair_agent_git_ownership(
                            workspace_id=workspace_id,
                            worktree_path=worktree_path,
                            reason="orphan_history_recovery_commit",
                        )
                        if recover_commit.ok:
                            ancestor = await _git_in_worktree(
                                ["merge-base", "--is-ancestor", base_commit, "HEAD"]
                            )
                    if not ancestor.ok:
                        await self._mark_failed(
                            workspace_id=workspace_id,
                            from_status=WorkspaceStatus.running,
                            failure_reason=FailureReason.agent_failure,
                            message=(
                                "agent severed git history — HEAD does not descend from "
                                f"base commit {base_commit[:10] if base_commit else 'unknown'}, "
                                "and automatic recovery (reset --soft + fresh commit) also failed. "
                                "The coding CLI likely ran `git checkout --orphan` or reinitialised "
                                "the repo; inspect the worktree manually."
                            ),
                        )
                        return
                    _log.info(
                        "executor.orphan_history_recovered",
                        workspace_id=workspace_id,
                        base_commit=base_commit,
                    )
            elif recovery.get("recovery_mode") == "rebase_only":
                try:
                    rebase_recovery_result = await self._run_monitor_rebase_recovery(
                        workspace_id=workspace_id,
                        worktree_path=worktree_path,
                        base_branch=ws.branch_base,
                        branch_name=expected_branch,
                        remote_branch=ws.remote_push_branch or expected_branch,
                        reason=str(recovery.get("reason") or "stale"),
                        recovery_payload=recovery,
                    )
                    base_commit = rebase_recovery_result.base_sha
                except _MonitorRebaseRecoveryError as exc:
                    message = str(exc)[:2000]
                    await self._finish_active_recovery_operations(
                        workspace_id=workspace_id,
                        status=OperationStatus.failed,
                        reason_code="MONITOR_RECOVERY_REBASE_FAILED",
                        error_message=message,
                    )
                    await self._mark_failed(
                        workspace_id=workspace_id,
                        from_status=WorkspaceStatus.running,
                        failure_reason=FailureReason.infrastructure_failure,
                        message=message,
                        reason_code="MONITOR_RECOVERY_REBASE_FAILED",
                    )
                    return
        except _PostAgentCommitStepError as exc:
            await self._mark_post_agent_commit_failed(
                workspace_id=workspace_id,
                error=exc,
                agent_run_reason_code=agent_run_reason_code,
                agent_run_details=agent_run_details,
                agent_exit_note=agent_exit_note,
                upstream_failure_reason=agent_run_failure_reason,
            )
            return
        except Exception as exc:  # unexpected — mark infrastructure
            if _git_error_indicates_missing_head_object(str(exc)):
                if await self._recover_missing_git_head_or_mark_failed(
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    base_commit=base_commit,
                    branch_name=expected_branch,
                    from_status=WorkspaceStatus.running,
                    stage="post_agent_commit",
                    error=exc,
                ):
                    _log.warning(
                        "executor.commit_step_missing_head_recovered",
                        workspace_id=workspace_id,
                    )
                    if not await self._verify_recovered_post_agent_commit_or_mark_failed(
                        workspace_id=workspace_id,
                        worktree_path=worktree_path,
                        base_commit=base_commit,
                        owned_paths=list(ws.owned_paths),
                        expected_status=WorkspaceStatus.running,
                    ):
                        return
                else:
                    return
            else:
                _log.exception("executor.commit_step_failed", workspace_id=workspace_id)
                await self._mark_failed(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.running,
                    failure_reason=FailureReason.infrastructure_failure,
                    message=f"post-agent commit step failed: {exc!r}"[:2000],
                )
                return

        # ── Step 2: validation (tests + optional Alembic), with fix-cycle ──
        if not await self._transition_if_current(
            workspace_id,
            from_status=WorkspaceStatus.running,
            to=WorkspaceStatus.validating,
            reason="AGENT_RUN_OK",
            action="start_validation",
        ):
            return

        max_fix_passes = self._config.max_validation_fix_passes
        profile = _profile_for_workspace(
            ws,
            worktree_path=worktree_path,
            planning_max_iterations_default=self._config.planning_max_iterations_default,
        )
        validation_commands = [
            step.command.command
            for step in profile_phase_command_plan(profile, ("post_agent", "validate"))
        ]
        test_commands_tuple = tuple(validation_commands)
        validation_tier = _validation_tier_for_workspace(ws, profile)
        if rebase_recovery_result is not None:
            validation_tier = max(validation_tier, 2)
        last_failure_message: str | None = None
        successful_validation_run_id: str | None = None
        successful_validation_workspace_head_sha: str | None = None
        validation_fix_passes_used = 0
        post_validation_conformance_fix_attempts = 0
        post_validation_conformance_fix_pass_budget = (
            max(
                0,
                planning_validation_handoff.max_iterations - planning_validation_handoff.iteration,
            )
            if planning_validation_handoff is not None and recovery is None
            else 0
        )
        max_validation_attempts = max_fix_passes + post_validation_conformance_fix_pass_budget + 1
        for pass_number in range(max_validation_attempts):
            # This loop covers the initial validation plus any validation or
            # post-validation conformance fix prompts. The per-category
            # counters below enforce their separate budgets.
            if not await self._recheck_status(
                workspace_id,
                expected=WorkspaceStatus.validating,
                action="validate",
            ):
                return
            validation_workspace_head_sha = await self._capture_workspace_head_sha(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
            )
            validation_run_id = await self._start_validation_run(
                workspace_id=workspace_id,
                profile=profile,
                base_commit=base_commit,
                workspace_head_sha=validation_workspace_head_sha,
                target_branch=expected_branch,
                target_head_sha=None,
                tier=validation_tier,
            )
            run_local_coverage = _should_run_local_coverage(profile)
            coverage_evidence = _CoverageEvidenceResult(coverage=None)
            try:
                await self._update_subphase(workspace_id, "validation")
                val_result = await self._validation.run_profile_phases(
                    workspace_id=workspace_id,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    profile=profile,
                    phase_names=("post_agent", "validate"),
                    run_healthchecks=True,
                    worktree_path=worktree_path,
                    include_coverage=False,
                )
                if run_local_coverage and val_result.all_passed:
                    coverage_evidence = await self._run_final_coverage_gate(
                        workspace_id=workspace_id,
                        compose_project=compose_project,
                        compose_file=compose_file,
                        profile=profile,
                        validation_tier=validation_tier,
                        workspace_head_sha=validation_workspace_head_sha,
                    )
                    val_result = replace(val_result, coverage=coverage_evidence.coverage)
            except ComposeExecCleanupError as exc:
                message = cleanup_failure_message(exc)
                _log.error(
                    "executor.validation_cleanup_failed",
                    workspace_id=workspace_id,
                    validation_run_id=validation_run_id,
                    source=exc.source,
                    label=exc.label,
                    invocation_id=exc.invocation_id,
                    reason_code=exc.reason_code,
                )
                if await self._finish_validation_callback_if_terminal(
                    workspace_id=workspace_id,
                    validation_run_id=validation_run_id,
                    requested_tier=validation_tier,
                ):
                    return
                await self._finish_validation_run(
                    validation_run_id,
                    status="failed",
                    reason_code=EXEC_PROCESS_CLEANUP_FAILED,
                )
                await self._finish_pending_validate_operations(
                    workspace_id=workspace_id,
                    status=OperationStatus.failed,
                    validation_run_id=validation_run_id,
                    requested_tier=validation_tier,
                    reason_code=EXEC_PROCESS_CLEANUP_FAILED,
                    error_message=message,
                )
                await self._mark_failed(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.validating,
                    failure_reason=FailureReason.infrastructure_failure,
                    message=message,
                    reason_code=EXEC_PROCESS_CLEANUP_FAILED,
                )
                return
            except Exception as exc:
                _log.exception(
                    "executor.validation_run_unexpected_failed",
                    workspace_id=workspace_id,
                    validation_run_id=validation_run_id,
                )
                if await self._finish_validation_callback_if_terminal(
                    workspace_id=workspace_id,
                    validation_run_id=validation_run_id,
                    requested_tier=validation_tier,
                ):
                    return
                await self._finish_validation_run(
                    validation_run_id,
                    status="failed",
                    reason_code="VALIDATION_INFRASTRUCTURE_ERROR",
                )
                await self._mark_failed(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.validating,
                    failure_reason=FailureReason.infrastructure_failure,
                    message=f"unexpected error during validation run: {exc!r}"[:2000],
                    reason_code="VALIDATION_INFRASTRUCTURE_ERROR",
                )
                return
            if await self._finish_validation_callback_if_terminal(
                workspace_id=workspace_id,
                validation_run_id=validation_run_id,
                requested_tier=validation_tier,
            ):
                return
            val_result = _apply_baseline_coverage_ratchet(
                val_result,
                baseline_coverage=baseline_coverage,
            )
            validation_coverage = _validation_run_coverage_metadata(
                val_result,
                baseline_coverage=baseline_coverage,
            )
            await self._finish_validation_run(
                validation_run_id,
                status="succeeded" if val_result.all_passed else "failed",
                reason_code=_validation_run_reason_code(val_result),
                retry_count=val_result.total_retries,
                coverage=validation_coverage,
                command_retries=[c.retry_count for c in val_result.commands],
                coverage_evidence_status=coverage_evidence.evidence_status,
                coverage_evidence_reason_code=coverage_evidence.reason_code,
                coverage_evidence_source_run_id=coverage_evidence.source_run_id,
            )
            if val_result.all_passed:
                conformance_failure: _PlanningRunFailure | None = None
                if planning_validation_handoff is not None:
                    conformance_handoff = planning_validation_handoff
                    try:
                        if post_validation_conformance_fix_attempts:
                            conformance_handoff = replace(
                                planning_validation_handoff,
                                iteration=(
                                    planning_validation_handoff.iteration
                                    + post_validation_conformance_fix_attempts
                                ),
                            )
                        if recovery is not None:
                            _log.info(
                                "executor.post_validation_conformance_recovery_single_attempt",
                                workspace_id=workspace_id,
                                validation_run_id=validation_run_id,
                                recovery_mode=recovery.get("recovery_mode"),
                                source=recovery.get("source"),
                                max_fix_passes=post_validation_conformance_fix_pass_budget,
                                will_retry=False,
                            )
                        conformance_failure = await self._run_post_validation_conformance_check(
                            adapter=adapter,
                            workspace=ws,
                            profile=profile,
                            compose_project=compose_project,
                            compose_file=compose_file,
                            worktree_path=worktree_path,
                            model=default_model,
                            handoff=conformance_handoff,
                            validation_run_id=validation_run_id,
                        )
                    except ComposeExecCleanupError as exc:
                        message = cleanup_failure_message(exc)
                        _log.error(
                            "executor.post_validation_conformance_cleanup_failed",
                            workspace_id=workspace_id,
                            validation_run_id=validation_run_id,
                            source=exc.source,
                            label=exc.label,
                            invocation_id=exc.invocation_id,
                            reason_code=exc.reason_code,
                        )
                        await self._finish_pending_validate_operations(
                            workspace_id=workspace_id,
                            status=OperationStatus.failed,
                            validation_run_id=validation_run_id,
                            requested_tier=validation_tier,
                            reason_code=EXEC_PROCESS_CLEANUP_FAILED,
                            coverage=validation_coverage,
                            error_message=message,
                        )
                        await self._mark_failed(
                            workspace_id=workspace_id,
                            from_status=WorkspaceStatus.validating,
                            failure_reason=FailureReason.infrastructure_failure,
                            message=message,
                            reason_code=EXEC_PROCESS_CLEANUP_FAILED,
                        )
                        return
                    except AgentRunError as exc:
                        reason_code = exc.reason_code or "AGENT_CLI_FAILED"
                        message = _post_validation_conformance_agent_failure_message(exc)
                        _log.warning(
                            "executor.post_validation_conformance_agent_failed",
                            workspace_id=workspace_id,
                            validation_run_id=validation_run_id,
                            returncode=exc.result.returncode,
                            reason_code=reason_code,
                        )
                        await self._finish_pending_validate_operations(
                            workspace_id=workspace_id,
                            status=OperationStatus.failed,
                            validation_run_id=validation_run_id,
                            requested_tier=validation_tier,
                            reason_code=reason_code,
                            coverage=validation_coverage,
                            error_message=message,
                        )
                        await self._mark_failed(
                            workspace_id=workspace_id,
                            from_status=WorkspaceStatus.validating,
                            failure_reason=FailureReason.agent_failure,
                            message=message,
                            reason_code=reason_code,
                            details=_post_validation_conformance_agent_failure_details(
                                exc,
                                validation_run_id=validation_run_id,
                            ),
                        )
                        return
                    except _PostValidationConformanceReportGitError as exc:
                        reason_code = POST_VALIDATION_CONFORMANCE_REPORT_GIT_FAILED_REASON_CODE
                        message = str(exc)
                        _log.error(
                            "executor.post_validation_conformance_report_git_failed",
                            workspace_id=workspace_id,
                            validation_run_id=validation_run_id,
                            operation=exc.operation,
                            returncode=exc.returncode,
                            command_reason_code=exc.command_reason_code,
                            reason_code=reason_code,
                        )
                        await self._finish_pending_validate_operations(
                            workspace_id=workspace_id,
                            status=OperationStatus.failed,
                            validation_run_id=validation_run_id,
                            requested_tier=validation_tier,
                            reason_code=reason_code,
                            coverage=validation_coverage,
                            error_message=message,
                        )
                        failure_details: dict[str, Any] = {
                            "validation_run_id": validation_run_id,
                            "report_path": conformance_handoff.report_path.as_posix(),
                            "operation": exc.operation,
                            "returncode": exc.returncode,
                        }
                        if exc.command_reason_code is not None:
                            failure_details["command_reason_code"] = exc.command_reason_code
                        if exc.cleanup_operation is not None:
                            failure_details["cleanup_operation"] = exc.cleanup_operation
                            failure_details["cleanup_returncode"] = exc.cleanup_returncode
                            failure_details["report_left_staged"] = True
                        if exc.cleanup_command_reason_code is not None:
                            failure_details["cleanup_command_reason_code"] = (
                                exc.cleanup_command_reason_code
                            )
                        await self._mark_failed(
                            workspace_id=workspace_id,
                            from_status=WorkspaceStatus.validating,
                            failure_reason=FailureReason.infrastructure_failure,
                            message=message,
                            reason_code=reason_code,
                            details=failure_details,
                        )
                        return
                    except _PostValidationConformanceReportWriteError as exc:
                        reason_code = POST_VALIDATION_CONFORMANCE_REPORT_WRITE_FAILED_REASON_CODE
                        message = str(exc)
                        _log.error(
                            "executor.post_validation_conformance_report_write_failed",
                            workspace_id=workspace_id,
                            validation_run_id=validation_run_id,
                            report_path=exc.report_path.as_posix(),
                            error_type=exc.error_type,
                            errno=exc.errno,
                            reason_code=reason_code,
                        )
                        await self._finish_pending_validate_operations(
                            workspace_id=workspace_id,
                            status=OperationStatus.failed,
                            validation_run_id=validation_run_id,
                            requested_tier=validation_tier,
                            reason_code=reason_code,
                            coverage=validation_coverage,
                            error_message=message,
                        )
                        write_failure_details: dict[str, Any] = {
                            "validation_run_id": validation_run_id,
                            "report_path": exc.report_path.as_posix(),
                            "operation": "write",
                            "error_type": exc.error_type,
                        }
                        if exc.errno is not None:
                            write_failure_details["errno"] = exc.errno
                        await self._mark_failed(
                            workspace_id=workspace_id,
                            from_status=WorkspaceStatus.validating,
                            failure_reason=FailureReason.infrastructure_failure,
                            message=message,
                            reason_code=reason_code,
                            details=write_failure_details,
                        )
                        return
                    except Exception as exc:
                        reason_code = POST_VALIDATION_CONFORMANCE_FAILED_REASON_CODE
                        message = (f"post-validation conformance check failed: {exc!r}")[:2000]
                        _log.exception(
                            "executor.post_validation_conformance_unexpected_failed",
                            workspace_id=workspace_id,
                            validation_run_id=validation_run_id,
                            reason_code=reason_code,
                        )
                        await self._finish_pending_validate_operations(
                            workspace_id=workspace_id,
                            status=OperationStatus.failed,
                            validation_run_id=validation_run_id,
                            requested_tier=validation_tier,
                            reason_code=reason_code,
                            coverage=validation_coverage,
                            error_message=message,
                        )
                        await self._mark_failed(
                            workspace_id=workspace_id,
                            from_status=WorkspaceStatus.validating,
                            failure_reason=FailureReason.infrastructure_failure,
                            message=message,
                            reason_code=reason_code,
                        )
                        return
                    if conformance_failure is not None:
                        remaining_conformance_iterations = max(
                            0,
                            conformance_handoff.max_iterations - conformance_handoff.iteration,
                        )
                        # Recovery skips feature execution; retrying this
                        # conformance miss would only rerun validation.
                        if recovery is not None or remaining_conformance_iterations <= 0:
                            await self._finish_pending_validate_operations(
                                workspace_id=workspace_id,
                                status=OperationStatus.failed,
                                validation_run_id=validation_run_id,
                                requested_tier=validation_tier,
                                reason_code=conformance_failure.reason_code
                                or PLAN_CONFORMANCE_UNSATISFIED,
                                coverage=validation_coverage,
                                error_message=conformance_failure.message,
                            )
                            await self._mark_failed(
                                workspace_id=workspace_id,
                                from_status=WorkspaceStatus.validating,
                                failure_reason=FailureReason.agent_failure,
                                message=conformance_failure.message[:2000],
                                reason_code=conformance_failure.reason_code,
                                details=conformance_failure.details,
                            )
                            return
                        _log.info(
                            "executor.post_validation_conformance_needs_fix_pass",
                            workspace_id=workspace_id,
                            validation_run_id=validation_run_id,
                            fix_pass=post_validation_conformance_fix_attempts + 1,
                            max_fix_passes=post_validation_conformance_fix_pass_budget,
                            validation_fix_passes_used=validation_fix_passes_used,
                            remaining_conformance_iterations=remaining_conformance_iterations,
                            reason_code=(
                                conformance_failure.reason_code or PLAN_CONFORMANCE_UNSATISFIED
                            ),
                        )
                        post_validation_conformance_fix_attempts += 1
                        val_result = _post_validation_conformance_fix_result(
                            failure=conformance_failure,
                            workspace_id=workspace_id,
                            artifacts_root=self._config.compose_projects_root,
                            attempt=post_validation_conformance_fix_attempts,
                        )
                if conformance_failure is None:
                    successful_validation_run_id = validation_run_id
                    successful_validation_workspace_head_sha = validation_workspace_head_sha
                    if (
                        recovery is not None
                        and ws.pr_url
                        and planning_validation_handoff is not None
                    ):
                        post_conformance_head_sha = await self._capture_workspace_head_sha(
                            workspace_id=workspace_id,
                            worktree_path=worktree_path,
                        )
                        if post_conformance_head_sha:
                            successful_validation_workspace_head_sha = post_conformance_head_sha
                    await self._finish_pending_validate_operations(
                        workspace_id=workspace_id,
                        status=OperationStatus.succeeded,
                        validation_run_id=validation_run_id,
                        requested_tier=validation_tier,
                        reason_code="VALIDATION_OK",
                        coverage=validation_coverage,
                    )
                    if validation_fix_passes_used or post_validation_conformance_fix_attempts:
                        _log.info(
                            "executor.validation_recovered",
                            workspace_id=workspace_id,
                            fix_passes_used=validation_fix_passes_used,
                            post_validation_conformance_fix_attempts=(
                                post_validation_conformance_fix_attempts
                            ),
                        )
                    break

            first_fail = val_result.first_failure
            is_post_validation_conformance_fix_pass = (
                first_fail is not None
                and first_fail.phase == "conformance"
                and first_fail.command == "post-validation plan conformance"
            )
            _log.info(
                "executor.validation_failed",
                workspace_id=workspace_id,
                failed_command=first_fail.command if first_fail else None,
                fix_pass=pass_number,
                max_fix_passes=max_fix_passes,
                validation_fix_passes_used=validation_fix_passes_used,
                post_validation_conformance_fix_attempts=(post_validation_conformance_fix_attempts),
            )
            last_failure_message = _validation_failure_message(
                val_result,
                baseline_coverage=baseline_coverage,
            )

            if first_fail is None or (
                not is_post_validation_conformance_fix_pass
                and validation_fix_passes_used >= max_fix_passes
            ):
                # Exhausted our budget (or no failure details to anchor a
                # fix prompt on) — mark failed and let the operator triage.
                # If a post-validation conformance fix already consumed a
                # prior successful run, this terminal path intentionally
                # reports coverage from the fresh failing validation result.
                await self._finish_pending_validate_operations(
                    workspace_id=workspace_id,
                    status=OperationStatus.failed,
                    validation_run_id=validation_run_id,
                    requested_tier=validation_tier,
                    reason_code=_validation_run_reason_code(val_result),
                    coverage=_validation_run_coverage_metadata(
                        val_result,
                        baseline_coverage=baseline_coverage,
                    ),
                    error_message=last_failure_message,
                )
                if first_fail is not None and first_fail.phase == "healthcheck":
                    await self._record_health_check_failed_event(
                        workspace_id=workspace_id,
                        failure=first_fail,
                    )
                await self._mark_failed(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.validating,
                    failure_reason=_failure_reason_for_phase(first_fail),
                    message=(
                        last_failure_message
                        + (f" (after {max_fix_passes} fix attempts)" if max_fix_passes > 0 else "")
                    )[:2000],
                    reason_code=_validation_run_reason_code(val_result),
                )
                return

            # Fire a fix pass: re-invoke the coding CLI with the failure
            # context, then re-commit whatever it changed.
            if is_post_validation_conformance_fix_pass:
                fix_pass_number = max(1, post_validation_conformance_fix_attempts)
                fix_pass_total_passes = max(1, post_validation_conformance_fix_pass_budget)
                fix_pass_kind = "post-validation conformance"
            else:
                fix_pass_number = validation_fix_passes_used + 1
                fix_pass_total_passes = max_fix_passes
                fix_pass_kind = "validation"
            fix_context = ValidationFixContext(
                failed_command=first_fail.command,
                returncode=first_fail.returncode,
                stdout_tail=read_output_tail(first_fail.stdout_path),
                stderr_tail=read_output_tail(first_fail.stderr_path),
                pass_number=fix_pass_number,
                total_passes=fix_pass_total_passes,
                test_commands=test_commands_tuple,
                reason_code=_validation_run_reason_code(val_result),
                coverage_percent=val_result.coverage.percent if val_result.coverage else None,
                coverage_minimum_percent=(
                    val_result.coverage.minimum_percent if val_result.coverage else None
                ),
                baseline_coverage_percent=(
                    baseline_coverage.percent if baseline_coverage is not None else None
                ),
                failing_test_node_ids=(
                    tuple(val_result.coverage.failing_test_node_ids)
                    if val_result.coverage is not None
                    else ()
                ),
                failing_test_evidence=(
                    tuple(val_result.coverage.failing_test_evidence)
                    if val_result.coverage is not None
                    else ()
                ),
            )
            fix_prompt = build_fix_prompt(fix_context)
            _log.info(
                "executor.fix_pass_start",
                workspace_id=workspace_id,
                pass_number=fix_pass_number,
                max_fix_passes=fix_pass_total_passes,
                fix_pass_kind=fix_pass_kind,
                failed_command=first_fail.command,
            )
            if not await self._recheck_status(
                workspace_id,
                expected=WorkspaceStatus.validating,
                action="validation_fix_agent_run",
            ):
                return
            if not await self._ensure_worktree_available(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                expected=WorkspaceStatus.validating,
                action="validation_fix_agent_run",
                validation_run_id=validation_run_id,
                requested_tier=validation_tier,
            ):
                return
            fix_command_evidence: list[str] = []
            try:
                fix_result = await adapter.run(
                    compose_project=compose_project,
                    compose_file=compose_file,
                    prompt=fix_prompt,
                    model=default_model,
                    workspace_id=workspace_id,
                )
                append_command_evidence(
                    fix_command_evidence,
                    stdout=fix_result.stdout,
                    stderr=fix_result.stderr,
                )
            except ComposeExecCleanupError as exc:
                message = cleanup_failure_message(exc)
                _log.error(
                    "executor.fix_pass_cleanup_failed",
                    workspace_id=workspace_id,
                    pass_number=fix_pass_number,
                    fix_pass_kind=fix_pass_kind,
                    source=exc.source,
                    label=exc.label,
                    invocation_id=exc.invocation_id,
                    reason_code=exc.reason_code,
                )
                await self._finish_pending_validate_operations(
                    workspace_id=workspace_id,
                    status=OperationStatus.failed,
                    validation_run_id=validation_run_id,
                    requested_tier=validation_tier,
                    reason_code=EXEC_PROCESS_CLEANUP_FAILED,
                    coverage=_validation_run_coverage_metadata(
                        val_result,
                        baseline_coverage=baseline_coverage,
                    ),
                    error_message=message,
                )
                await self._mark_failed(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.validating,
                    failure_reason=FailureReason.infrastructure_failure,
                    message=message,
                    reason_code=EXEC_PROCESS_CLEANUP_FAILED,
                )
                return
            except AgentRunError as exc:
                append_command_evidence(
                    fix_command_evidence,
                    stdout=exc.result.stdout,
                    stderr=exc.result.stderr,
                )
                # Coding CLI exited non-zero on the fix pass. Mirrors the
                # initial-run behaviour: log, remember the note, fall
                # through to commit any salvaged work, then continue the
                # loop (next validation will tell us if it's pushable).
                # Initial no-work provider failures are handled by the
                # post-agent failure path. Fix-pass provider errors keep
                # the validation salvage flow so review/fix recovery
                # remains owned by the PR-monitor path.
                _log.warning(
                    "executor.fix_pass_agent_nonzero_exit",
                    workspace_id=workspace_id,
                    pass_number=fix_pass_number,
                    fix_pass_kind=fix_pass_kind,
                    returncode=exc.result.returncode,
                    reason_code=exc.reason_code,
                )

            if not is_post_validation_conformance_fix_pass:
                validation_fix_passes_used += 1

            if not await self._recheck_status(
                workspace_id,
                expected=WorkspaceStatus.validating,
                action="validation_fix_commit",
            ):
                return
            if not await self._ensure_worktree_available(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                expected=WorkspaceStatus.validating,
                action="validation_fix_git_add",
                validation_run_id=validation_run_id,
                requested_tier=validation_tier,
            ):
                return

            # Commit whatever the fix pass produced. Simpler than the initial
            # post-agent commit block — orphan-history recovery isn't possible
            # here (HEAD already descends from base after the initial run
            # succeeded); zero-change fix passes are allowed.
            fix_add = await _git_in_worktree(["add", "-A"])
            await self._repair_agent_git_ownership(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                reason="validation_fix_git_add",
            )
            if not fix_add.ok:
                _log.warning(
                    "executor.fix_pass_add_failed",
                    workspace_id=workspace_id,
                    stderr=fix_add.stderr[:400],
                )
            if not await self._ensure_worktree_available(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                expected=WorkspaceStatus.validating,
                action="validation_fix_git_diff",
                validation_run_id=validation_run_id,
                requested_tier=validation_tier,
            ):
                return
            fix_cached = await _git_in_worktree(["diff", "--cached", "--name-only"])
            fix_staged_paths = (
                _git_name_lines(fix_cached.stdout) if fix_cached.stdout.strip() else []
            )
            supply_chain_result = await self._refresh_supply_chain_policy_for_workspace(
                workspace_id=workspace_id,
                command_evidence=fix_command_evidence,
                changed_paths=fix_staged_paths,
            )
            if supply_chain_result.policy_blocked:
                message = _supply_chain_block_message(supply_chain_result.findings)
                await self._finish_pending_validate_operations(
                    workspace_id=workspace_id,
                    status=OperationStatus.failed,
                    validation_run_id=validation_run_id,
                    requested_tier=validation_tier,
                    reason_code="SUPPLY_CHAIN_POLICY_BLOCKED",
                    coverage=_validation_run_coverage_metadata(
                        val_result,
                        baseline_coverage=baseline_coverage,
                    ),
                    error_message=message,
                )
                await self._mark_failed(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.validating,
                    failure_reason=FailureReason.policy_failure,
                    reason_code="SUPPLY_CHAIN_POLICY_BLOCKED",
                    message=message[:2000],
                )
                return
            if fix_staged_paths:
                if await self._fail_if_plan_only_paths(
                    workspace_id=workspace_id,
                    changed_paths=fix_staged_paths,
                    expected_status=WorkspaceStatus.validating,
                ):
                    await self._finish_pending_validate_operations(
                        workspace_id=workspace_id,
                        status=OperationStatus.failed,
                        validation_run_id=validation_run_id,
                        requested_tier=validation_tier,
                        reason_code=PLAN_ONLY_OUTPUT_REASON_CODE,
                        coverage=_validation_run_coverage_metadata(
                            val_result,
                            baseline_coverage=baseline_coverage,
                        ),
                        error_message=plan_only_output_message(fix_staged_paths),
                    )
                    return
                has_known_non_plan_output = True
                protected_file_diffs = await self._protected_file_diffs_for_staged_paths(
                    worktree_path=worktree_path,
                    changed_paths=fix_staged_paths,
                )
                violations = find_protected_quality_gate_changes(
                    changed_paths=fix_staged_paths,
                    owned_paths=list(ws.owned_paths),
                    protected_file_diffs=protected_file_diffs,
                )
                if violations:
                    message = quality_gate_violation_message(violations)
                    await self._finish_pending_validate_operations(
                        workspace_id=workspace_id,
                        status=OperationStatus.failed,
                        validation_run_id=validation_run_id,
                        requested_tier=validation_tier,
                        reason_code="QUALITY_GATE_POLICY_CHANGED",
                        coverage=_validation_run_coverage_metadata(
                            val_result,
                            baseline_coverage=baseline_coverage,
                        ),
                        error_message=message,
                    )
                    await self._mark_failed(
                        workspace_id=workspace_id,
                        from_status=WorkspaceStatus.validating,
                        failure_reason=FailureReason.policy_failure,
                        reason_code="QUALITY_GATE_POLICY_CHANGED",
                        message=message[:2000],
                    )
                    return
                if not await self._ensure_worktree_available(
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    expected=WorkspaceStatus.validating,
                    action="validation_fix_git_commit",
                    validation_run_id=validation_run_id,
                    requested_tier=validation_tier,
                ):
                    return
                commit_msg = f"awf: fix pass {fix_pass_number} for {ws.task_title}"[:72]
                commit_body = (
                    f"AWF {fix_pass_kind} fix pass {fix_pass_number} of "
                    f"{fix_pass_total_passes} for workspace {workspace_id} "
                    f"(agent: {ws.agent}). Failed command: "
                    f"{first_fail.command}."
                )
                fix_commit = await self._runner.run(
                    [
                        "git",
                        "-C",
                        str(worktree_path),
                        "commit",
                        "-m",
                        commit_msg,
                        "-m",
                        commit_body,
                    ],
                )
                await self._repair_agent_git_ownership(
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    reason="validation_fix_git_commit",
                )
                if not fix_commit.ok:
                    _log.warning(
                        "executor.fix_pass_commit_failed",
                        workspace_id=workspace_id,
                        stderr=fix_commit.stderr[:400],
                    )
            # Loop back to re-validate.

        # ── Recovery skip-push guard ───────────────────────────────────────
        # Recovery for a workspace that already has an open PR must NOT
        # re-create the PR. Clean validate-only recovery does not push; if a
        # fix pass or handoff report created a new validated local commit,
        # update the existing PR branch before handing back to the monitor.
        # Rebase-only recovery already pushed the rebased branch above, but
        # later validation work can still advance local HEAD.
        if recovery is not None and ws.pr_url:
            recovery_requires_pr_update = _recovery_needs_existing_pr_push(
                recovery,
                validated_workspace_head_sha=successful_validation_workspace_head_sha,
                rebase_recovery_result=rebase_recovery_result,
            )
            if rebase_recovery_result is not None and successful_validation_run_id is not None:
                try:
                    await self._set_validation_run_target_head_sha(
                        validation_run_id=successful_validation_run_id,
                        target_head_sha=rebase_recovery_result.head_sha,
                    )
                    await self._clear_rebase_recovery_staleness(
                        workspace_id=workspace_id,
                    )
                except Exception:
                    _log.exception(
                        "executor.rebase_recovery_staleness_clear_failed",
                        workspace_id=workspace_id,
                        validation_run_id=successful_validation_run_id,
                    )
            if not recovery_requires_pr_update:
                if not await self._recheck_status(
                    workspace_id,
                    expected=WorkspaceStatus.validating,
                    action="recovery_skip_push",
                ):
                    return
                async with self._session_factory() as session:
                    repo = WorkspaceRepository(session)
                    persisted = await repo.get(workspace_id)
                    if persisted is None:  # pragma: no cover - destroyed mid-flight
                        return
                    if persisted.status != WorkspaceStatus.validating.value:
                        await self._record_stale_action_skip(
                            repo,
                            persisted,
                            action="recovery_skip_push",
                            expected=WorkspaceStatus.validating,
                            reason_code="EXECUTOR_STALE_STATUS",
                        )
                        await session.commit()
                        return
                    has_monitor = (
                        self._pr_monitor is not None or self._pr_monitor_factory is not None
                    )
                    await repo.transition(
                        persisted,
                        to=WorkspaceStatus.monitoring_pr
                        if has_monitor
                        else WorkspaceStatus.completed,
                        reason_code="RECOVERY_VALIDATION_OK",
                    )
                    await session.commit()
                _log.info(
                    "executor.recovery_skip_push",
                    workspace_id=workspace_id,
                    pr_url=ws.pr_url,
                    has_monitor=has_monitor,
                )
                if has_monitor:
                    _monitor: _MonitorRunnerProto | None = self._pr_monitor
                    if _monitor is None and self._pr_monitor_factory is not None:
                        _monitor = _call_pr_monitor_factory(
                            self._pr_monitor_factory,
                            adapter=adapter,
                            profile=profile,
                            workspace=persisted,
                            provider_recovery_default_model=(
                                defaults.model if defaults is not None else None
                            ),
                        )
                    if _monitor is not None:
                        _log.info(
                            "executor.recovery_handoff_to_pr_monitor",
                            workspace_id=workspace_id,
                            pr_url=ws.pr_url,
                        )
                        if not await self._recheck_status(
                            workspace_id,
                            expected=WorkspaceStatus.monitoring_pr,
                            action="run_pr_monitor",
                        ):
                            return
                        await _monitor.run(
                            workspace_id=workspace_id,
                            compose_project=compose_project,
                            compose_file=compose_file,
                        )
                return
            _log.info(
                "executor.recovery_existing_pr_update_required",
                workspace_id=workspace_id,
                pr_url=ws.pr_url,
                source_head_sha=recovery.get("source_head_sha"),
                validated_workspace_head_sha=successful_validation_workspace_head_sha,
            )

        try:
            if not has_known_non_plan_output and await self._fail_if_plan_only_committed_output(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                base_commit=base_commit,
                expected_status=WorkspaceStatus.validating,
            ):
                return
        except Exception as exc:
            _log.exception("executor.plan_only_output_check_failed", workspace_id=workspace_id)
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.validating,
                failure_reason=FailureReason.infrastructure_failure,
                message=f"plan-only output check failed: {exc!r}"[:2000],
            )
            return

        # ── Step 3: push + open PR ──────────────────────────────────────────
        if not await self._transition_if_current(
            workspace_id,
            from_status=WorkspaceStatus.validating,
            to=WorkspaceStatus.pushing,
            reason="VALIDATION_OK",
            action="start_push",
        ):
            return
        if not await self._ensure_worktree_available(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            expected=WorkspaceStatus.pushing,
            action="pr_push_open",
        ):
            return

        pr_title = ws.task_title
        pr_body = _build_pr_body(ws, defaults=defaults)
        push_branch_name = ws.branch_name or f"awf/{workspace_id}"
        existing_pr_remote_branch = ws.remote_push_branch if ws.pr_url else None
        existing_pr_remote_url = _existing_pr_remote_push_url(ws) if ws.pr_url else None
        audit_remote_branch = existing_pr_remote_branch or push_branch_name

        try:
            pr = await self._pr_creator.push_and_open(
                worktree_path=worktree_path,
                branch_name=push_branch_name,
                base_branch=ws.branch_base,
                title=pr_title,
                body=pr_body,
                existing_pr_url=ws.pr_url,
                remote_branch_name=existing_pr_remote_branch,
                remote_url=existing_pr_remote_url,
            )
        except PullRequestError as exc:
            _log.error(
                "executor.pr_failed",
                workspace_id=workspace_id,
                operation=exc.operation,
                returncode=exc.returncode,
            )
            if exc.operation != "git push":
                await self._record_executor_pr_audit_event(
                    workspace_id,
                    event_type=_AUDIT_GIT_PUSH_EVENT,
                    action="git_push",
                    outcome="succeeded",
                    reason_code="PR_UPDATED" if ws.pr_url else "PR_OPENED",
                    branch_name=push_branch_name,
                    remote_branch=audit_remote_branch,
                    pr_number=_extract_pr_number(ws.pr_url) if ws.pr_url else None,
                    pr_url=ws.pr_url,
                    source_head_sha=exc.head_sha,
                )
            await self._record_executor_pr_audit_event(
                workspace_id,
                event_type=(
                    _AUDIT_GIT_PUSH_EVENT
                    if exc.operation == "git push"
                    else _AUDIT_PR_CREATED_EVENT
                ),
                action="git_push" if exc.operation == "git push" else "pr_create",
                outcome="failed",
                reason_code=(
                    _GIT_PUSH_FAILED_REASON_CODE
                    if exc.operation == "git push"
                    else _PR_CREATE_FAILED_REASON_CODE
                ),
                branch_name=push_branch_name,
                remote_branch=audit_remote_branch,
                pr_number=_extract_pr_number(ws.pr_url) if ws.pr_url else None,
                pr_url=ws.pr_url,
                source_head_sha=exc.head_sha,
                evidence={
                    "operation": exc.operation,
                    "returncode": exc.returncode,
                    "error_message": exc.stderr.strip() or "<no output>",
                },
            )
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.pushing,
                failure_reason=FailureReason.infrastructure_failure,
                message=str(exc)[:2000],
            )
            return
        except Exception as exc:
            _log.exception("executor.pr_unexpected_failed", workspace_id=workspace_id)
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.pushing,
                failure_reason=FailureReason.infrastructure_failure,
                message=f"unexpected error during PR creation: {exc!r}"[:2000],
            )
            return

        # ── Step 4: persist PR URL + (optionally) hand off to monitor ──────
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            persisted = await repo.get(workspace_id)
            if persisted is None:  # pragma: no cover - destroyed mid-flight
                return
            if persisted.status != WorkspaceStatus.pushing.value:
                await self._record_stale_action_skip(
                    repo,
                    persisted,
                    action="persist_pr",
                    expected=WorkspaceStatus.pushing,
                    reason_code="EXECUTOR_STALE_STATUS",
                )
                await session.commit()
                return
            had_existing_pr_url = bool(persisted.pr_url)
            persisted.pr_url = pr.url
            persisted.pr_number = _extract_pr_number(pr.url)
            if pr.head_sha:
                persisted.monitor_last_commit_sha = pr.head_sha
            if persisted.task_kind == "feature_branch_pr" and not persisted.remote_push_branch:
                persisted.remote_push_branch = (
                    pr.branch or persisted.branch_name or f"awf/{workspace_id}"
                )
            pr_reason_code = "PR_UPDATED" if had_existing_pr_url else "PR_OPENED"
            await self._add_executor_pr_audit_event(
                repo,
                persisted,
                event_type=_AUDIT_GIT_PUSH_EVENT,
                action="git_push",
                outcome="succeeded",
                reason_code=pr_reason_code,
                branch_name=persisted.branch_name or pr.branch,
                remote_branch=persisted.remote_push_branch or pr.branch,
                pr_number=persisted.pr_number,
                pr_url=persisted.pr_url,
                source_head_sha=pr.head_sha,
            )
            await self._add_executor_pr_audit_event(
                repo,
                persisted,
                event_type=_AUDIT_PR_CREATED_EVENT,
                action="pr_create",
                outcome="reused" if had_existing_pr_url else "succeeded",
                reason_code=pr_reason_code,
                branch_name=persisted.branch_name or pr.branch,
                remote_branch=persisted.remote_push_branch or pr.branch,
                pr_number=persisted.pr_number,
                pr_url=persisted.pr_url,
                source_head_sha=pr.head_sha,
            )
            # Resolve which monitor (if any) to hand off to. Pre-constructed
            # ``pr_monitor`` wins (tests); otherwise the factory builds one
            # from the per-task adapter now that we have it.
            monitor: _MonitorRunnerProto | None = self._pr_monitor
            if monitor is None and self._pr_monitor_factory is not None:
                monitor = _call_pr_monitor_factory(
                    self._pr_monitor_factory,
                    adapter=adapter,
                    profile=profile,
                    workspace=persisted,
                    provider_recovery_default_model=(
                        defaults.model if defaults is not None else None
                    ),
                )

            if monitor is not None:
                # Hand off to the monitor — it will transition to completed
                # (on merge) or failed (on abort / cap / close).
                await repo.transition(
                    persisted,
                    to=WorkspaceStatus.monitoring_pr,
                    reason_code=pr_reason_code,
                )
                await session.commit()
            else:
                # No monitor wired (legacy executor path / unit-test shim) —
                # preserve the original ``pushing → completed`` contract.
                await repo.transition(
                    persisted,
                    to=WorkspaceStatus.completed,
                    reason_code=pr_reason_code,
                )
                await session.commit()

        if successful_validation_run_id is not None and pr.head_sha:
            try:
                await self._set_validation_run_target_head_sha(
                    validation_run_id=successful_validation_run_id,
                    target_head_sha=pr.head_sha,
                )
            except Exception:
                _log.exception(
                    "executor.validation_run_target_head_sha_update_failed",
                    workspace_id=workspace_id,
                    validation_run_id=successful_validation_run_id,
                    target_head_sha=pr.head_sha,
                )

        if monitor is not None:
            _log.info(
                "executor.handoff_to_pr_monitor",
                workspace_id=workspace_id,
                pr_url=pr.url,
            )
            if not await self._recheck_status(
                workspace_id,
                expected=WorkspaceStatus.monitoring_pr,
                action="run_pr_monitor",
            ):
                return
            await monitor.run(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
            )
            return

        _log.info(
            "executor.completed",
            workspace_id=workspace_id,
            pr_url=pr.url,
        )

    async def resume_pr_monitor(self, workspace_id: str) -> None:
        """Resume the PR monitor for a workspace already in ``monitoring_pr``.

        This is the service-worker restart path. It intentionally skips setup,
        agent execution, validation, push, and PR creation; those have already
        happened before the workspace entered ``monitoring_pr``.
        """
        ws = await self._load_workspace(workspace_id)
        if ws is None:
            _log.warning("executor.resume_skip_unknown", workspace_id=workspace_id)
            return
        if ws.status != WorkspaceStatus.monitoring_pr.value:
            _log.info(
                "executor.resume_skip_not_monitoring_pr",
                workspace_id=workspace_id,
                status=ws.status,
            )
            return
        if not await self._recheck_status(
            workspace_id,
            expected=WorkspaceStatus.monitoring_pr,
            action="resume_pr_monitor",
        ):
            return

        if not ws.remote_push_branch and ws.task_kind == "feature_branch_pr" and ws.branch_name:
            recovered_remote_push_branch = await self._recover_feature_branch_remote_push_branch(
                workspace_id=workspace_id,
                remote_push_branch=ws.branch_name,
            )
            if recovered_remote_push_branch:
                ws.remote_push_branch = recovered_remote_push_branch

        missing = _missing_monitor_recovery_metadata(ws)
        if missing:
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.monitoring_pr,
                failure_reason=FailureReason.infrastructure_failure,
                message=(
                    "monitor recovery: missing required persisted metadata: " + ", ".join(missing)
                )[:2000],
                reason_code="MONITOR_RECOVERY_METADATA_MISSING",
            )
            return

        compose_project = ws.compose_project_name
        compose_file_path = ws.compose_file_path
        assert compose_project is not None
        assert compose_file_path is not None

        if not await self._recheck_status(
            workspace_id,
            expected=WorkspaceStatus.monitoring_pr,
            action="resume_compose",
        ):
            return

        try:
            await self._compose.ensure_project_up(
                project_name=compose_project,
                compose_file=Path(compose_file_path),
                workspace_id=workspace_id,
                wait=True,
            )
        except ComposeOperationError as exc:
            _log.error(
                "executor.resume_compose_up_failed",
                workspace_id=workspace_id,
                reason_code=exc.reason_code,
                stderr=exc.stderr[:1000],
            )
            await self._record_monitor_runtime_restart_failed(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file_path=compose_file_path,
                error=exc,
            )

        monitor: _MonitorRunnerProto | None = self._pr_monitor
        try:
            if monitor is None and self._pr_monitor_factory is not None:
                agent = AgentRuntime(ws.agent)
                defaults = self._defaults_for(agent)
                adapter_defaults = _agent_defaults_for_workspace(ws, defaults)
                adapter = get_adapter(
                    agent,
                    runner=self._runner,
                    defaults=adapter_defaults,
                    log_store=self._log_store,
                )
                profile = _profile_for_workspace(
                    ws,
                    worktree_path=self._config.worktrees_root / workspace_id,
                    planning_max_iterations_default=(self._config.planning_max_iterations_default),
                )
                monitor = _call_pr_monitor_factory(
                    self._pr_monitor_factory,
                    adapter=adapter,
                    profile=profile,
                    workspace=ws,
                    provider_recovery_default_model=(
                        defaults.model if defaults is not None else None
                    ),
                )
        except Exception as exc:
            _log.exception("executor.pr_monitor_resume_build_failed", workspace_id=workspace_id)
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.monitoring_pr,
                failure_reason=FailureReason.infrastructure_failure,
                message=f"monitor recovery: failed to build PR monitor: {exc!r}"[:2000],
                reason_code="MONITOR_RECOVERY_FAILED",
            )
            return

        if monitor is None:
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.monitoring_pr,
                failure_reason=FailureReason.infrastructure_failure,
                message="monitor recovery: no PR monitor configured",
                reason_code="MONITOR_RECOVERY_FAILED",
            )
            return

        _log.info(
            "executor.resume_pr_monitor",
            workspace_id=workspace_id,
            pr_url=ws.pr_url,
            pr_number=ws.pr_number,
        )
        if not await self._recheck_status(
            workspace_id,
            expected=WorkspaceStatus.monitoring_pr,
            action="resume_monitor_run",
        ):
            return
        await monitor.run(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=Path(compose_file_path),
        )

    # ── Internals ──────────────────────────────────────────────────────────

    async def _record_monitor_runtime_restart_failed(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file_path: str,
        error: ComposeOperationError,
    ) -> None:
        try:
            async with self._session_factory() as session:
                repo = WorkspaceRepository(session)
                ws = await repo.get(workspace_id)
                if ws is None or ws.status != WorkspaceStatus.monitoring_pr.value:
                    return
                await repo.add_event(
                    ws,
                    event_type="workspace.monitor_runtime_restart_failed",
                    reason_code="MONITOR_RECOVERY_COMPOSE_FAILED",
                    payload={
                        "compose_project_name": compose_project,
                        "compose_file_path": compose_file_path,
                        "operation": error.operation,
                        "returncode": error.returncode,
                        "stderr": error.stderr[:1000],
                        "reason_code": error.reason_code,
                    },
                )
                await session.commit()
        except Exception:
            _log.exception(
                "executor.monitor_runtime_restart_failed_record_failed",
                workspace_id=workspace_id,
                compose_project_name=compose_project,
                compose_file_path=compose_file_path,
                reason_code=error.reason_code,
            )

    async def _load_workspace(self, workspace_id: str) -> Workspace | None:
        async with self._session_factory() as session:
            return await WorkspaceRepository(session).get(workspace_id)

    async def _repair_agent_git_ownership(
        self,
        *,
        workspace_id: str,
        worktree_path: Path,
        reason: str,
    ) -> bool:
        try:
            await asyncio.to_thread(repair_agent_writable_worktree, None, worktree_path)
        except Exception:
            _log.exception(
                "executor.agent_git_ownership_repair_failed",
                workspace_id=workspace_id,
                worktree_path=str(worktree_path),
                reason=reason,
            )
            return False
        return True

    async def _run_agent_git_writability_preflight(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        worktree_path: Path,
    ) -> bool:
        # Unit tests often use a plain temp directory as a fake worktree. Real
        # AWF-linked worktrees always have a .git control file, so keep the
        # production preflight active without making those fakes shell out.
        if not (worktree_path / ".git").exists():
            return True
        if not compose_file.exists():
            return True
        if not await self._repair_agent_git_ownership(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            reason="agent_git_writability_preflight",
        ):
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.running,
                failure_reason=FailureReason.infrastructure_failure,
                message=(
                    "agent Git writability preflight failed before container "
                    "execution: control-plane ownership repair raised an error"
                ),
                reason_code=GIT_AGENT_WRITABILITY_FAILED_REASON_CODE,
            )
            return False

        invocation = build_tracked_compose_exec(
            compose_project=compose_project,
            compose_file=compose_file,
            cli_args=[
                "sh",
                "-lc",
                _agent_git_writability_preflight_script(workspace_id),
            ],
            source="executor",
            label="agent_git_writability_preflight",
        )
        result = await self._runner.run(invocation.args, input_bytes=b"")
        if result.ok:
            _log.info(
                "executor.agent_git_writability_preflight_ok",
                workspace_id=workspace_id,
            )
            return True
        output = (result.stderr.strip() or result.stdout.strip() or "<no output>")[:1200]
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.infrastructure_failure,
            message=(
                f"agent Git writability preflight failed (exit={result.returncode}): {output}"
            )[:2000],
            reason_code=GIT_AGENT_WRITABILITY_FAILED_REASON_CODE,
            details={
                "returncode": result.returncode,
                "stdout": result.stdout[:1000],
                "stderr": result.stderr[:1000],
            },
        )
        return False

    async def _recover_missing_git_head_or_mark_failed(
        self,
        *,
        workspace_id: str,
        worktree_path: Path,
        base_commit: str | None,
        branch_name: str,
        from_status: WorkspaceStatus,
        stage: str,
        error: BaseException,
    ) -> bool:
        if base_commit is None:
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=from_status,
                failure_reason=FailureReason.infrastructure_failure,
                message=(
                    "Git object recovery failed: workspace HEAD points at a "
                    "missing object and base_commit is not available"
                ),
                reason_code=GIT_OBJECT_MISSING_REASON_CODE,
            )
            return False
        try:
            recovery = await _recover_missing_head_from_filesystem(
                runner=self._runner,
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                base_commit=base_commit,
                branch_name=branch_name,
            )
        except Exception as exc:
            _log.exception(
                "executor.git_object_filesystem_recovery_failed",
                workspace_id=workspace_id,
                stage=stage,
            )
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=from_status,
                failure_reason=FailureReason.infrastructure_failure,
                message=(
                    "Git object recovery failed: workspace HEAD points at a "
                    f"missing object during {stage}, but AWF could not run "
                    f"filesystem recovery: {exc!r}"
                )[:2000],
                reason_code=GIT_OBJECT_MISSING_REASON_CODE,
            )
            return False
        if recovery is None:
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=from_status,
                failure_reason=FailureReason.infrastructure_failure,
                message=(
                    "Git object recovery failed: workspace HEAD points at a "
                    f"missing object during {stage}, and AWF could not rebuild "
                    f"a valid commit from the filesystem state: {error!r}"
                )[:2000],
                reason_code=GIT_OBJECT_MISSING_REASON_CODE,
            )
            return False
        try:
            await self._record_git_object_recovery_event(
                workspace_id=workspace_id,
                stage=stage,
                recovery=recovery,
            )
        except Exception as exc:
            _log.exception(
                "executor.git_object_recovery_event_record_failed",
                workspace_id=workspace_id,
                stage=stage,
            )
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=from_status,
                failure_reason=FailureReason.infrastructure_failure,
                message=(
                    "Git object recovery failed: rebuilt HEAD during "
                    f"{stage}, but could not record the recovery event: {exc!r}"
                )[:2000],
                reason_code=GIT_OBJECT_MISSING_REASON_CODE,
            )
            return False
        return True

    async def _record_git_object_recovery_event(
        self,
        *,
        workspace_id: str,
        stage: str,
        recovery: _GitObjectRecoveryResult,
    ) -> None:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None:  # pragma: no cover - destroyed mid-flight
                return
            await repo.add_event(
                ws,
                event_type="workspace.git_object_missing_recovered",
                reason_code=GIT_OBJECT_MISSING_RECOVERED_REASON_CODE,
                payload={
                    "stage": stage,
                    "strategy": recovery.strategy,
                    "broken_head_sha": recovery.broken_head_sha,
                    "recovered_head_sha": recovery.recovered_head_sha,
                },
            )
            await session.commit()

    async def _recover_feature_branch_remote_push_branch(
        self,
        *,
        workspace_id: str,
        remote_push_branch: str,
    ) -> str | None:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None or ws.status != WorkspaceStatus.monitoring_pr.value:
                return None
            if ws.remote_push_branch:
                return ws.remote_push_branch
            if ws.task_kind != "feature_branch_pr" or not ws.branch_name:
                return None
            ws.remote_push_branch = remote_push_branch
            await repo.advance_workspace_version(ws)
            await repo.add_event(
                ws,
                event_type="workspace.remote_push_branch_recovered",
                reason_code="REMOTE_PUSH_BRANCH_RECOVERED",
                payload={
                    "remote_push_branch": remote_push_branch,
                    "source": "branch_name",
                },
            )
            await session.commit()
            return remote_push_branch

    async def _block_open_pr_reexecution_without_recovery(
        self,
        *,
        workspace_id: str,
    ) -> _PrReexecutionGuardResult:
        message = "open PR exists; monitor recovery required"
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            persisted = await repo.get_with_operations(workspace_id)
            if persisted is None:  # pragma: no cover - row disappeared mid-flight
                return _PrReexecutionGuardResult(blocked=True)
            if persisted.status != WorkspaceStatus.running.value:
                await self._record_stale_action_skip(
                    repo,
                    persisted,
                    action="pr_reexecution_guard",
                    expected=WorkspaceStatus.running,
                    reason_code="EXECUTOR_STALE_STATUS",
                )
                await session.commit()
                return _PrReexecutionGuardResult(blocked=True)
            recovery = _get_active_recovery_payload(persisted)
            if recovery is not None:
                return _PrReexecutionGuardResult(blocked=False, recovery=recovery)
            if not persisted.pr_url or persisted.monitor_started_at is None:
                return _PrReexecutionGuardResult(blocked=False)
            await repo.add_event(
                persisted,
                event_type="workspace.pr_reexecution_blocked",
                reason_code=PR_REEXECUTION_GUARD_REASON_CODE,
                payload={
                    "pr_number": persisted.pr_number,
                    "pr_url": persisted.pr_url,
                    "status": persisted.status,
                },
            )
            persisted.failure_reason = FailureReason.infrastructure_failure.value
            persisted.failure_message = message
            await repo.transition(
                persisted,
                to=WorkspaceStatus.failed,
                reason_code=PR_REEXECUTION_GUARD_REASON_CODE,
            )
            blocked_pr_number = persisted.pr_number
            blocked_pr_url = persisted.pr_url
            await session.commit()
        _log.error(
            "executor.pr_reexecution_blocked",
            workspace_id=workspace_id,
            pr_number=blocked_pr_number,
            pr_url=blocked_pr_url,
            reason_code=PR_REEXECUTION_GUARD_REASON_CODE,
        )
        return _PrReexecutionGuardResult(blocked=True)

    async def _ensure_worktree_available(
        self,
        *,
        workspace_id: str,
        worktree_path: Path,
        expected: WorkspaceStatus,
        action: str,
        validation_run_id: str | None = None,
        requested_tier: int | None = None,
    ) -> bool:
        if worktree_path.is_dir():
            return True

        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None:  # pragma: no cover - row disappeared mid-flight
                return False
            if ws.status != expected.value:
                await self._record_stale_action_skip(
                    repo,
                    ws,
                    action=action,
                    expected=expected,
                    reason_code="EXECUTOR_STALE_STATUS",
                )
                if _is_callback_terminal_status(ws.status):
                    await self._finish_ignored_stale_callback_operations_in_session(
                        session,
                        workspace_id=workspace_id,
                        callback_source="executor",
                        callback_action=action,
                        expected_status=expected,
                        actual_status=ws.status,
                        validation_run_id=validation_run_id,
                        requested_tier=requested_tier,
                    )
                await session.commit()
                return False

            message = _worktree_missing_message(worktree_path, action)
            _log.error(
                "executor.worktree_missing",
                workspace_id=workspace_id,
                action=action,
                worktree_path=str(worktree_path),
                reason_code=WORKTREE_MISSING_REASON_CODE,
            )
            await repo.add_event(
                ws,
                event_type="workspace.executor_worktree_missing",
                reason_code=WORKTREE_MISSING_REASON_CODE,
                payload={
                    "action": action,
                    "worktree_path": str(worktree_path),
                },
            )
            if validation_run_id is not None and requested_tier is not None:
                await self._finish_pending_validate_operations_in_session(
                    session,
                    workspace_id=workspace_id,
                    status=OperationStatus.failed,
                    validation_run_id=validation_run_id,
                    requested_tier=requested_tier,
                    reason_code=WORKTREE_MISSING_REASON_CODE,
                    error_message=message,
                )
            ws.failure_reason = FailureReason.infrastructure_failure.value
            ws.failure_message = message[:2000]
            await repo.transition(
                ws,
                to=WorkspaceStatus.failed,
                reason_code=WORKTREE_MISSING_REASON_CODE,
            )
            await session.commit()
            return False

    def _defaults_for(self, agent: AgentRuntime) -> AgentDefaults | None:
        defaults = defaults_with_model_overrides(
            self._config.default_models,
            base=self._config.agent_defaults,
        )
        return defaults.get(agent)

    async def _run_baseline_coverage_preflight(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        profile: WorkspaceProfile,
    ) -> ValidationCoverageResult | None:
        """Measure coverage before agent edits so fix prompts know the baseline.

        This is intentionally non-blocking. A repository may already be below a
        newly tightened target, and AWF should still let explicitly launched
        coverage-improvement work proceed. The result is attached to later fix
        prompts and validation metadata so agents are steered toward adding
        tests instead of weakening the gate.
        """
        coverage = profile.validation.coverage
        if profile.validation.strategy.baseline_coverage == "skip":
            _log.info(
                "executor.baseline_coverage_skipped_by_policy",
                workspace_id=workspace_id,
                reason_code="BASELINE_COVERAGE_SKIPPED_BY_POLICY",
            )
            return None
        if coverage.command is None:
            return None
        result = await self._validation.run_profile_coverage(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            profile=profile,
            phase="baseline_coverage",
        )
        if result is not None and not result.ok:
            _log.info(
                "executor.baseline_coverage_below_policy",
                workspace_id=workspace_id,
                percent=result.percent,
                minimum_percent=result.minimum_percent,
                reason_code=result.reason_code,
            )
        return result

    async def _run_final_coverage_gate(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        profile: WorkspaceProfile,
        validation_tier: int,
        workspace_head_sha: str | None,
    ) -> _CoverageEvidenceResult:
        coverage = profile.validation.coverage
        if coverage.command is None:
            return _CoverageEvidenceResult(coverage=None)

        command_records = _validation_run_command_records(
            profile=profile,
            phase_names=("post_agent", "validate"),
            run_healthchecks=True,
        )
        strategy = profile.validation.strategy
        if strategy.reuse_evidence:
            async with self._session_factory() as session:
                reusable = await ValidationRunRepository(session).find_reusable_coverage_evidence(
                    workspace_id=workspace_id,
                    tier=validation_tier,
                    commands=command_records,
                    workspace_head_sha=workspace_head_sha,
                    resolved_profile_digest=resolved_profile_digest(profile),
                    environment_identity_digest=environment_identity_digest(profile),
                    max_age_seconds=strategy.freshness_max_age_seconds,
                    now=datetime.now(UTC),
                )
            if reusable is not None:
                metadata = validation_run_coverage_payload(reusable)
                if metadata:
                    return _CoverageEvidenceResult(
                        coverage=_coverage_result_from_metadata(metadata),
                        evidence_status="reused",
                        reason_code="VALIDATION_EVIDENCE_REUSED",
                        source_run_id=reusable.id,
                    )

        result = await self._validation.run_profile_coverage(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            profile=profile,
            phase="coverage",
            parallel_worker_cpu_limit=await self._parallel_worker_cpu_limit_for_workspace(
                workspace_id,
                profile=profile,
            ),
        )
        return _CoverageEvidenceResult(
            coverage=result,
            evidence_status="executed" if result is not None else None,
            reason_code="VALIDATION_EVIDENCE_EXECUTED" if result is not None else None,
        )

    async def _parallel_worker_cpu_limit_for_workspace(
        self,
        workspace_id: str,
        *,
        profile: WorkspaceProfile,
    ) -> int | None:
        if profile.validation.coverage.parallel_workers is None:
            return None
        async with self._session_factory() as session:
            reservation = await ResourceReservationRepository(session).active_for_workspace(
                workspace_id
            )
        if reservation is None:
            return None
        return max(1, int(reservation.steady_cpu))

    async def _record_planning_validation_handoff_event(
        self,
        *,
        workspace_id: str,
        handoff: _PlanningValidationHandoff,
    ) -> None:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            workspace = await repo.get(workspace_id)
            if workspace is None:
                return
            await repo.add_event(
                workspace,
                event_type="workspace.planning_conformance_requires_awf_validation",
                reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
                payload={
                    "summary": handoff.report.summary,
                    "gaps": list(handoff.report.gaps),
                    "report_reason_code": handoff.report.reason_code,
                    "plan_path": handoff.plan_path.as_posix(),
                    "report_path": handoff.report_path.as_posix(),
                    "iteration": handoff.iteration,
                    "max_iterations": handoff.max_iterations,
                },
            )
            await session.commit()

    async def _run_post_validation_conformance_check(
        self,
        *,
        adapter: AgentAdapter,
        workspace: Workspace,
        profile: WorkspaceProfile,
        compose_project: str,
        compose_file: Path,
        worktree_path: Path,
        model: str | None,
        handoff: _PlanningValidationHandoff,
        validation_run_id: str,
    ) -> _PlanningRunFailure | None:
        # Post-validation conformance is strictly report-only, regardless of
        # ordinary planning unexplained-deviation policy.
        del profile
        evidence = await self._validation_run_evidence_for_conformance(validation_run_id)
        # Snapshot before the adapter run and before any AWF-synthesized
        # satisfied report write below; this scope check only polices changes
        # made during the report-only conformance command.
        before_compare = await self._changed_paths(worktree_path)
        before_compare_head = await self._git_rev_parse_head(worktree_path)
        allowed_paths = {handoff.report_path}
        # Path-set subtraction misses edits to already-dirty paths, so keep a
        # content snapshot for every pre-dirty non-report path.
        before_dirty_digests = {
            path: self._digest_dirty_content(worktree_path, {path})
            for path in before_compare - allowed_paths
        }
        # A stale handoff report may still be present at this path; prefer
        # stdout unless the conformance rerun actually refreshed the file.
        report_path = worktree_path / handoff.report_path
        before_report_text = _read_text_if_present(report_path)
        before_report_digest = _digest_text(before_report_text) if before_report_text else None
        await self._update_subphase(workspace.id, "conformance")
        compare_result = await adapter.run(
            compose_project=compose_project,
            compose_file=compose_file,
            prompt=build_conformance_prompt(
                task_prompt=workspace.task_prompt,
                plan_path=handoff.plan_path,
                report_path=handoff.report_path,
                iteration=handoff.iteration + 1,
                validation_evidence=evidence,
            ),
            model=model,
            workspace_id=workspace.id,
        )
        after_compare = await self._changed_paths(worktree_path)
        committed_compare = (
            await self._committed_paths_since(worktree_path, before_compare_head)
            if before_compare_head is not None
            else set()
        )
        edited_pre_dirty_extra = {
            path
            for path, digest in before_dirty_digests.items()
            if self._digest_dirty_content(worktree_path, {path}) != digest
        }
        dirty_extra = after_compare - before_compare - allowed_paths
        committed_extra = committed_compare - allowed_paths
        extra = sorted(dirty_extra | committed_extra | edited_pre_dirty_extra)
        if extra:
            return _build_planning_scope_failure(
                scope_phase="conformance",
                required_paths=(handoff.report_path,),
                offending_paths=extra,
                summary=(
                    "post-validation conformance phase changed files outside "
                    f"`{handoff.report_path}`"
                ),
            )

        report_text = _read_text_if_present(report_path)
        report_from_fresh_file = (
            report_text is not None and _digest_text(report_text) != before_report_digest
        )
        if not report_from_fresh_file:
            report_text = None
        if report_text is None and compare_result.stdout:
            report_text = compare_result.stdout
        report = parse_conformance_report(report_text or "")
        if report.satisfied:
            if not report_from_fresh_file:
                try:
                    self._write_satisfied_post_validation_conformance_report(
                        worktree_path=worktree_path,
                        report_path=handoff.report_path,
                        report=report,
                    )
                except OSError as exc:
                    raise _PostValidationConformanceReportWriteError(
                        report_path=handoff.report_path,
                        error=exc,
                    ) from exc
            await self._commit_post_validation_conformance_report(
                workspace_id=workspace.id,
                worktree_path=worktree_path,
                report_path=handoff.report_path,
                validation_run_id=validation_run_id,
            )
            await self._record_post_validation_conformance_event(
                workspace_id=workspace.id,
                handoff=handoff,
                report=report,
                validation_run_id=validation_run_id,
            )
            return None

        gap_text = "; ".join(report.gaps) or report.summary
        return _PlanningRunFailure(
            message=f"post-validation plan conformance was not satisfied: {gap_text}",
            reason_code=PLAN_CONFORMANCE_UNSATISFIED,
            details={
                "conformance": build_conformance_failure_evidence(
                    report=report,
                    iterations_used=handoff.iteration + 2,
                    max_iterations=handoff.max_iterations,
                    plan_path=handoff.plan_path,
                    report_path=handoff.report_path,
                ),
                "validation_run_id": validation_run_id,
            },
        )

    @staticmethod
    def _write_satisfied_post_validation_conformance_report(
        *,
        worktree_path: Path,
        report_path: Path,
        report: PlanConformanceReport,
    ) -> None:
        path = worktree_path / report_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "status": report.status.value,
                    "summary": report.summary,
                    "reason_code": report.reason_code,
                    "gaps": list(report.gaps),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    async def _commit_post_validation_conformance_report(
        self,
        *,
        workspace_id: str,
        worktree_path: Path,
        report_path: Path,
        validation_run_id: str,
    ) -> bool:
        report_path_text = report_path.as_posix()
        git_base = [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
        ]
        await self._repair_agent_git_ownership(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            reason="post_validation_conformance_report_git_add",
        )
        add_result = await self._runner.run([*git_base, "add", "--", report_path_text])
        if not add_result.ok:
            raise _PostValidationConformanceReportGitError(
                operation="add",
                result=add_result,
            )

        cached = await self._runner.run(
            [*git_base, "diff", "--cached", "--name-only", "--", report_path_text]
        )
        if not cached.ok:
            reset_result = await self._runner.run(
                [*git_base, "reset", "-q", "--", report_path_text]
            )
            if not reset_result.ok:
                _log.warning(
                    "executor.post_validation_conformance_report_unstage_failed",
                    workspace_id=workspace_id,
                    report_path=report_path_text,
                    triggering_operation="diff",
                    returncode=reset_result.returncode,
                    command_reason_code=reset_result.reason_code,
                )
            raise _PostValidationConformanceReportGitError(
                operation="diff",
                result=cached,
                cleanup_operation="reset" if not reset_result.ok else None,
                cleanup_result=reset_result if not reset_result.ok else None,
            )
        staged_paths = _git_name_lines(cached.stdout) if cached.stdout.strip() else []
        if report_path_text not in staged_paths:
            _log.info(
                "executor.post_validation_conformance_report_no_commit_needed",
                workspace_id=workspace_id,
                report_path=report_path_text,
                validation_run_id=validation_run_id,
            )
            return False

        commit_result = await self._runner.run(
            [
                *git_base,
                *git_identity_config_args(),
                "commit",
                "-m",
                "awf: post-validation conformance report",
                "-m",
                (
                    "Persist satisfied post-validation conformance report "
                    f"for validation run {validation_run_id}."
                ),
                "--",
                report_path_text,
            ],
        )
        await self._repair_agent_git_ownership(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            reason="post_validation_conformance_report_git_commit",
        )
        if not commit_result.ok:
            raise _PostValidationConformanceReportGitError(
                operation="commit",
                result=commit_result,
            )
        return True

    async def _record_post_validation_conformance_event(
        self,
        *,
        workspace_id: str,
        handoff: _PlanningValidationHandoff,
        report: PlanConformanceReport,
        validation_run_id: str,
    ) -> None:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            workspace = await repo.get(workspace_id)
            if workspace is None:
                return
            await repo.add_event(
                workspace,
                event_type="workspace.post_validation_conformance_satisfied",
                reason_code=report.reason_code,
                payload={
                    "summary": report.summary,
                    "plan_path": handoff.plan_path.as_posix(),
                    "report_path": handoff.report_path.as_posix(),
                    "validation_run_id": validation_run_id,
                },
            )
            await session.commit()

    async def _validation_run_evidence_for_conformance(self, validation_run_id: str) -> str:
        async with self._session_factory() as session:
            run = await ValidationRunRepository(session).get(validation_run_id)
            if run is None:
                payload: dict[str, Any] = {
                    "validation_run_id": validation_run_id,
                    "status": "missing",
                    "reason_code": "VALIDATION_RUN_NOT_FOUND",
                }
            else:
                log_stream_refs = dict(run.log_stream_refs or {})
                log_stream_refs.pop("coverage", None)
                coverage = validation_run_coverage_payload(run)
                payload = {
                    "validation_run_id": run.id,
                    "status": run.status,
                    "reason_code": run.reason_code,
                    "coverage": coverage,
                    "workspace_head_sha": run.workspace_head_sha,
                    "target_branch": run.target_branch,
                    "target_head_sha": run.target_head_sha,
                    "base_commit": run.base_commit,
                    "base_sha": run.base_sha,
                    "tier": run.tier,
                    "retry_count": run.retry_count,
                    "command_set_hash": run.command_set_hash,
                    # Command records are metadata-only; stdout/stderr stay in log refs.
                    # If that changes, update evidence compaction before passing them through.
                    "commands": list(run.commands or []),
                    "log_stream_refs": log_stream_refs,
                    "profile_name": run.profile_name,
                    "profile_version": run.profile_version,
                    "profile_source": run.profile_source,
                    "resolved_profile_digest": run.resolved_profile_digest,
                    "environment_identity_digest": run.environment_identity_digest,
                }
        # Preserve the complete evidence shape when it fits. If it does not,
        # compact bulky fields as JSON values so the fenced evidence stays
        # parseable while retaining the validation result fields first.
        safe_serialized_payload = _validation_evidence_json(payload)
        return f"AWF persisted validation run evidence:\n```json\n{safe_serialized_payload}\n```"

    async def _auto_retry_planning_scope_failure(
        self,
        *,
        workspace_id: str,
        failure: _PlanningRunFailure,
    ) -> None:
        """Create one clean retry for a planning-scope violation."""
        if failure.reason_code != AGENT_PLAN_PHASE_SCOPE_VIOLATION:
            return
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            workspace = await repo.get(workspace_id)
            if workspace is None:
                return
            task_policy = (
                workspace.task_policy if isinstance(workspace.task_policy, Mapping) else {}
            )
            scheduler_policy = task_policy.get("scheduler")
            if isinstance(scheduler_policy, Mapping) and scheduler_policy.get(
                "source_workspace_id"
            ):
                await repo.add_event(
                    workspace,
                    event_type="workspace.planning_scope_auto_retry_skipped",
                    reason_code="PLANNING_SCOPE_AUTO_RETRY_ALREADY_RETRIED",
                    payload={"source_reason_code": failure.reason_code},
                )
                await session.commit()
                return
            try:
                result = await retry_workspace_row(session, workspace_id)
            except WorkspaceRetryError as exc:
                await repo.add_event(
                    workspace,
                    event_type="workspace.planning_scope_auto_retry_failed",
                    reason_code="PLANNING_SCOPE_AUTO_RETRY_FAILED",
                    payload={
                        "source_reason_code": failure.reason_code,
                        "error": str(exc)[:2000],
                        "detail": exc.detail,
                    },
                )
                await session.commit()
                return
            await repo.add_event(
                workspace,
                event_type="workspace.planning_scope_auto_retry_requested",
                reason_code="PLANNING_SCOPE_AUTO_RETRY_REQUESTED",
                payload={
                    "source_reason_code": failure.reason_code,
                    "new_workspace_id": result.new_workspace.id,
                },
            )
            await session.commit()

    async def _run_agent_task_with_optional_planning(
        self,
        *,
        adapter: AgentAdapter,
        workspace: Workspace,
        profile: WorkspaceProfile,
        compose_project: str,
        compose_file: Path,
        worktree_path: Path,
        model: str | None,
        command_evidence: list[str] | None = None,
    ) -> str | _PlanningRunFailure | _PlanningValidationHandoff | None:
        planning = profile.planning
        coordination_warnings = coordination_warnings_from_task_policy(
            getattr(workspace, "task_policy", None)
        )
        workspace_runtime_context = render_workspace_runtime_context(profile)
        if not planning.required:
            await self._update_subphase(workspace.id, "agent")
            result = await adapter.run(
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=build_agent_task_prompt(
                    task_prompt=workspace.task_prompt,
                    coordination_warnings=coordination_warnings,
                    workspace_runtime_context=workspace_runtime_context,
                ),
                model=model,
                workspace_id=workspace.id,
            )
            append_command_evidence(
                command_evidence,
                stdout=result.stdout,
                stderr=result.stderr,
            )
            return None

        try:
            plan_path = render_workspace_path(planning.plan_path, workspace_id=workspace.id)
            report_path = render_workspace_path(
                planning.conformance_report_path,
                workspace_id=workspace.id,
            )
        except ValueError as exc:
            return f"planning profile is invalid: {exc}"

        before_plan = await self._changed_paths(worktree_path)
        plan_file_digest_before = _digest_file_if_present(worktree_path / plan_path)
        baseline_sha: str | None = None
        rev_r = await self._runner.run(
            [
                "git",
                *git_safe_directory_config_args(worktree_path),
                "-C",
                str(worktree_path),
                "rev-parse",
                "HEAD",
            ]
        )
        if rev_r.ok and rev_r.stdout.strip():
            baseline_sha = rev_r.stdout.strip()
        await self._update_subphase(workspace.id, "planning")
        plan_result = await adapter.run(
            compose_project=compose_project,
            compose_file=compose_file,
            prompt=build_planning_prompt(
                task_prompt=workspace.task_prompt,
                plan_path=plan_path,
                coordination_warnings=coordination_warnings,
                workspace_runtime_context=workspace_runtime_context,
            ),
            model=model,
            workspace_id=workspace.id,
        )
        append_command_evidence(
            command_evidence,
            stdout=plan_result.stdout,
            stderr=plan_result.stderr,
        )
        dirty_paths = await self._changed_paths(worktree_path)
        committed_paths = (
            await self._committed_paths_since(worktree_path, baseline_sha)
            if baseline_sha is not None
            else set()
        )
        after_plan = dirty_paths | committed_paths
        if plan_path not in after_plan:
            plan_file_digest_after = _digest_file_if_present(worktree_path / plan_path)
            if (
                plan_file_digest_after is not None
                and plan_file_digest_after != plan_file_digest_before
            ):
                after_plan = {*after_plan, plan_path}
        if plan_path not in after_plan:
            return _build_planning_scope_failure(
                scope_phase="planning",
                required_paths=(plan_path,),
                offending_paths=sorted(after_plan - before_plan),
                summary=(
                    f"planning phase did not create or modify required plan file `{plan_path}`"
                ),
            )
        if planning.enforce_plan_only_changes:
            extra = sorted(after_plan - before_plan - {plan_path})
            if extra:
                return _build_planning_scope_failure(
                    scope_phase="planning",
                    required_paths=(plan_path,),
                    offending_paths=extra,
                    summary=f"planning phase changed files outside `{plan_path}`",
                )

        gaps: tuple[str, ...] = ()
        last_report: PlanConformanceReport | None = None
        last_iteration = 0
        stall_policy = ConformanceStallPolicy(
            no_output_seconds=planning.conformance_stall.no_output_seconds,
            over_duration_seconds=planning.conformance_stall.over_duration_seconds,
            repeated_output_threshold=(planning.conformance_stall.repeated_output_threshold),
        )
        iteration_history: list[ConformanceIterationRecord] = []
        # Post-planning HEAD. Serves two purposes:
        #
        # 1. Implementation baseline for stall commit metrics. Pre-planning
        #    HEAD (``baseline_sha``) would inflate ``implementation_commit_count``
        #    if the agent committed the plan artifact during planning — the
        #    scope check accepts ``committed_paths`` and the agent is not
        #    blocked from committing the one allowed file.
        #
        # 2. Seeds the iteration progress digest. Combining the HEAD commit
        #    SHA with hashed file bytes lets re-edits to the same dirty file
        #    *and* commits made during an iteration both register as
        #    progress; without the HEAD signal an agent that commits each
        #    iteration leaves a clean working tree and produces identical
        #    empty digests, which would falsely trip
        #    ``classify_conformance_stall``'s repeated_output detector.
        implementation_baseline_sha = await self._git_rev_parse_head(worktree_path)
        iteration_start_digest = self._digest_dirty_content(
            worktree_path, dirty_paths, head_sha=implementation_baseline_sha
        )
        for iteration in range(planning.max_iterations + 1):
            last_iteration = iteration
            await self._update_subphase(workspace.id, "agent")
            execute_result = await adapter.run(
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=build_execution_prompt(
                    task_prompt=workspace.task_prompt,
                    plan_path=plan_path,
                    iteration=iteration,
                    gaps=gaps,
                    coordination_warnings=coordination_warnings,
                    workspace_runtime_context=workspace_runtime_context,
                ),
                model=model,
                workspace_id=workspace.id,
            )
            append_command_evidence(
                command_evidence,
                stdout=execute_result.stdout,
                stderr=execute_result.stderr,
            )
            before_compare = await self._changed_paths(worktree_path)
            # Snapshot any pre-existing report digest so the timeout branch
            # can distinguish a report this compare call produced from a
            # stale leftover (e.g., a satisfied JSON written by a prior
            # interrupted run on this workspace, or by an out-of-scope
            # earlier-phase write). Without this guard, a satisfied JSON
            # already on disk would short-circuit the loop on
            # AGENT_IDLE_TIMEOUT/AGENT_TIMEOUT with no evidence the current
            # compare call produced it.
            before_report_text = _read_text_if_present(worktree_path / report_path)
            before_report_digest = _digest_text(before_report_text) if before_report_text else None
            iteration_started_at = _monotonic()
            compare_error: AgentRunError | None = None
            compare_result = None
            try:
                await self._update_subphase(workspace.id, "conformance")
                compare_result = await adapter.run(
                    compose_project=compose_project,
                    compose_file=compose_file,
                    prompt=build_conformance_prompt(
                        task_prompt=workspace.task_prompt,
                        plan_path=plan_path,
                        report_path=report_path,
                        iteration=iteration,
                    ),
                    model=model,
                    workspace_id=workspace.id,
                )
                append_command_evidence(
                    command_evidence,
                    stdout=compare_result.stdout,
                    stderr=compare_result.stderr,
                )
            except AgentRunError as exc:
                if exc.reason_code not in {"AGENT_IDLE_TIMEOUT", "AGENT_TIMEOUT"}:
                    raise
                compare_error = exc
                append_command_evidence(
                    command_evidence,
                    stdout=exc.result.stdout,
                    stderr=exc.result.stderr,
                )

            elapsed_seconds = _monotonic() - iteration_started_at
            # Compute after_compare on both success and timeout paths so the
            # scope check runs uniformly. Otherwise an idle/timeout that still
            # leaves a satisfied report could write files outside report_path
            # and slip past the success short-circuit below.
            after_compare = await self._changed_paths(worktree_path)
            if planning.fail_on_unexplained_deviation:
                extra = sorted(after_compare - before_compare - {report_path})
                if extra:
                    return _build_planning_scope_failure(
                        scope_phase="conformance",
                        required_paths=(report_path,),
                        offending_paths=extra,
                        summary=(f"conformance phase changed files outside `{report_path}`"),
                    )
            if compare_error is None:
                stdout = compare_result.stdout if compare_result is not None else ""
                stderr = compare_result.stderr if compare_result is not None else ""
                report_text = _read_text_if_present(worktree_path / report_path) or stdout
                report = parse_conformance_report(report_text)
                last_report = report
                report_digest = _digest_text(report_text) if report_text else None
                fresh_report_written = (
                    report_digest is not None and report_digest != before_report_digest
                )
            else:
                stdout = compare_error.result.stdout
                stderr = compare_error.result.stderr
                # Even when the conformance call idles or times out, the agent
                # may have already written a valid (potentially satisfied)
                # report. Honor the on-disk report only when its digest
                # changed during this call so a stale satisfied JSON cannot
                # short-circuit the loop. Fall back to stdout — which is
                # always produced by this call — when the file is stale or
                # absent. A truly fresh write will produce a digest
                # different from the pre-call snapshot; otherwise the
                # iteration is treated as no_output by stall classification.
                current_report_text = _read_text_if_present(worktree_path / report_path)
                if (
                    current_report_text is not None
                    and _digest_text(current_report_text) != before_report_digest
                ):
                    report_text = current_report_text
                elif stdout:
                    report_text = stdout
                else:
                    report_text = None
                if report_text:
                    report = parse_conformance_report(report_text)
                    last_report = report
                    report_digest = _digest_text(report_text)
                    fresh_report_written = (
                        report_digest is not None and report_digest != before_report_digest
                    )
                else:
                    report = None
                    report_digest = None
                    fresh_report_written = False
            after_head = await self._git_rev_parse_head(worktree_path)
            after_digest = self._digest_dirty_content(
                worktree_path, after_compare, head_sha=after_head
            )
            worktree_changed = iteration_start_digest != after_digest
            iteration_start_digest = after_digest

            iteration_history.append(
                ConformanceIterationRecord(
                    iteration=iteration,
                    elapsed_seconds=elapsed_seconds,
                    report_digest=report_digest,
                    worktree_changed=worktree_changed,
                    stdout=stdout,
                    stderr=stderr,
                    error_reason_code=(
                        compare_error.reason_code if compare_error is not None else None
                    ),
                )
            )

            # Honour conformance success before stall classification so a
            # slow-but-satisfied iteration is not misread as over_duration,
            # and so a run that wrote a satisfied report before idling /
            # timing out is not misread as no_output.
            if report is not None and report.satisfied:
                _log.info(
                    "executor.planning_conformance_satisfied",
                    workspace_id=workspace.id,
                    iteration=iteration,
                    summary=report.summary,
                )
                return None

            if report is not None and conformance_requires_awf_validation(report):
                _log.info(
                    "executor.planning_conformance_requires_awf_validation",
                    workspace_id=workspace.id,
                    iteration=iteration,
                    max_iterations=planning.max_iterations,
                    gaps=list(report.gaps),
                    reason_code=report.reason_code,
                )
                return _PlanningValidationHandoff(
                    report=report,
                    plan_path=plan_path,
                    report_path=report_path,
                    iteration=iteration,
                    max_iterations=planning.max_iterations,
                )

            stall = classify_conformance_stall(
                history=iteration_history,
                policy=stall_policy,
                plan_path=plan_path,
                report_path=report_path,
                latest_error=compare_error,
            )
            if stall is not None and not (
                stall.kind == ConformanceStallKind.over_duration
                and compare_error is None
                and report is not None
                and fresh_report_written
            ):
                return await self._build_conformance_stall_failure(
                    workspace=workspace,
                    worktree_path=worktree_path,
                    baseline_sha=implementation_baseline_sha,
                    last_report=last_report,
                    stall=stall,
                    iterations_used=last_iteration + 1,
                    max_iterations=planning.max_iterations,
                    plan_path=plan_path,
                    report_path=report_path,
                    recovery_action=planning.conformance_stall.recovery_action,
                )

            if compare_error is not None:
                # Not classified as a stall, but the conformance call failed —
                # bubble up so the outer agent_failure handler captures it.
                raise compare_error

            assert report is not None
            gaps = report.gaps or (report.summary,)
            _log.info(
                "executor.planning_conformance_needs_iteration",
                workspace_id=workspace.id,
                iteration=iteration,
                max_iterations=planning.max_iterations,
                gaps=list(gaps),
                reason_code=report.reason_code,
            )

        if last_report is None:  # pragma: no cover - defensive
            return "planning conformance did not run"
        gap_text = "; ".join(last_report.gaps) or last_report.summary
        message = (
            "plan conformance was not satisfied after "
            f"{planning.max_iterations} iteration(s): {gap_text}"
        )
        return _PlanningRunFailure(
            message=message,
            reason_code=PLAN_CONFORMANCE_UNSATISFIED,
            details={
                "conformance": build_conformance_failure_evidence(
                    report=last_report,
                    iterations_used=last_iteration + 1,
                    max_iterations=planning.max_iterations,
                    plan_path=plan_path,
                    report_path=report_path,
                )
            },
        )

    async def _build_conformance_stall_failure(
        self,
        *,
        workspace: Workspace,
        worktree_path: Path,
        baseline_sha: str | None,
        last_report: PlanConformanceReport | None,
        stall: ConformanceStallEvidence,
        iterations_used: int,
        max_iterations: int,
        plan_path: Path,
        report_path: Path,
        recovery_action: str | None = None,
    ) -> _PlanningRunFailure:
        head_sha = await self._git_rev_parse_head(worktree_path)
        commit_count = 0
        changed_paths: list[str] = []
        if baseline_sha:
            commit_count = await self._git_commit_count_since(worktree_path, baseline_sha)
            try:
                changed = await self._committed_paths_since(worktree_path, baseline_sha)
            except RuntimeError:
                _log.exception(
                    "executor.planning_conformance_stalled_diff_failed",
                    workspace_id=workspace.id,
                    baseline_sha=baseline_sha,
                )
            else:
                changed_paths = sorted(path.as_posix() for path in changed)
        stall_evidence_payload = build_conformance_stall_failure_evidence(
            stall=stall,
            head_sha=head_sha,
            base_sha=baseline_sha,
            commit_count=commit_count,
            changed_paths=changed_paths,
            recovery_action=recovery_action,
        )
        details: dict[str, Any] = {"conformance_stall": stall_evidence_payload}
        if last_report is not None:
            details["conformance"] = build_conformance_failure_evidence(
                report=last_report,
                iterations_used=iterations_used,
                max_iterations=max_iterations,
                plan_path=plan_path,
                report_path=report_path,
            )
        message = (
            f"plan conformance stalled in iteration {stall.iteration_index} "
            f"({stall.kind.value}); preserving worktree for recovery"
        )
        _log.info(
            "executor.planning_conformance_stalled",
            workspace_id=workspace.id,
            iteration=stall.iteration_index,
            kind=stall.kind.value,
            elapsed_seconds=stall.elapsed_seconds,
            no_output_seconds=stall.no_output_seconds,
            repeated_output_count=stall.repeated_output_count,
            implementation_commit_count=commit_count,
        )
        try:
            async with self._session_factory() as session:
                repo = WorkspaceRepository(session)
                persisted = await repo.get(workspace.id)
                if persisted is not None:
                    await repo.add_event(
                        persisted,
                        event_type="workspace.planning_conformance_stalled",
                        reason_code=AGENT_STALLED_IN_CONFORMANCE,
                        payload=stall_evidence_payload,
                    )
                    await session.commit()
        except Exception:
            _log.exception(
                "executor.planning_conformance_stalled_record_failed",
                workspace_id=workspace.id,
            )
        return _PlanningRunFailure(
            message=message,
            reason_code=AGENT_STALLED_IN_CONFORMANCE,
            details=details,
        )

    async def _git_rev_parse_head(self, worktree_path: Path) -> str | None:
        result = await self._runner.run(
            [
                "git",
                *git_safe_directory_config_args(worktree_path),
                "-C",
                str(worktree_path),
                "rev-parse",
                "HEAD",
            ]
        )
        if not result.ok:
            return None
        head = result.stdout.strip()
        return head or None

    async def _git_commit_count_since(self, worktree_path: Path, since: str) -> int:
        result = await self._runner.run(
            [
                "git",
                *git_safe_directory_config_args(worktree_path),
                "-C",
                str(worktree_path),
                "rev-list",
                "--count",
                f"{since}..HEAD",
            ]
        )
        if not result.ok:
            return 0
        try:
            return int(result.stdout.strip() or "0")
        except ValueError:
            return 0

    async def _changed_paths(self, worktree_path: Path) -> set[Path]:
        result = await self._runner.run(
            [
                "git",
                *git_safe_directory_config_args(worktree_path),
                "-C",
                str(worktree_path),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ]
        )
        if not result.ok:
            raise RuntimeError(
                f"git status failed while checking workspace changes: {result.stderr}"
            )
        return changed_paths_from_porcelain(result.stdout)

    async def _refresh_supply_chain_policy_for_workspace(
        self,
        *,
        workspace_id: str,
        command_evidence: Sequence[str],
        changed_paths: Sequence[str],
    ) -> SupplyChainPolicyRefreshResult:
        async with self._session_factory() as session:
            result = await SupplyChainPolicyRefreshService(session).refresh_workspace(
                workspace_id,
                command_evidence=command_evidence,
                changed_paths=changed_paths,
            )
            await session.commit()
            return result

    def _digest_dirty_content(
        self,
        worktree_path: Path,
        paths: set[Path],
        *,
        head_sha: str | None = None,
    ) -> str:
        """Progress fingerprint combining HEAD SHA and dirty content bytes.

        Path-set equality alone treats iterative re-edits of the same file as
        no progress; hashing per-file bytes lets repeat edits register as
        work. Folding ``head_sha`` in additionally lets commits register as
        progress — an agent that commits each iteration leaves a clean
        working tree, so the dirty portion would otherwise digest identically
        and falsely trip ``classify_conformance_stall``'s repeated_output
        detector. Missing files contribute a deterministic marker so the
        digest stays stable across iterations whose worktree exists only in
        mocked git output.
        """
        hasher = hashlib.sha256()
        if head_sha is not None:
            hasher.update(head_sha.encode("utf-8"))
            hasher.update(b"\0")
        # Stream file bytes in fixed-size chunks rather than read_bytes() so a
        # large generated artifact in the dirty set does not balloon peak
        # memory on every conformance iteration.
        for path in sorted(paths, key=lambda p: p.as_posix()):
            hasher.update(path.as_posix().encode("utf-8"))
            hasher.update(b"\0")
            try:
                with (worktree_path / path).open("rb") as fh:
                    while chunk := fh.read(_FILE_DIGEST_CHUNK_SIZE):
                        hasher.update(chunk)
            except OSError:
                hasher.update(b"<missing>")
            hasher.update(b"\0")
        return hasher.hexdigest()

    async def _committed_paths_since(self, worktree_path: Path, since: str) -> set[Path]:
        result = await self._runner.run(
            [
                "git",
                *git_safe_directory_config_args(worktree_path),
                "-C",
                str(worktree_path),
                "diff",
                "--name-only",
                f"{since}..HEAD",
            ]
        )
        if not result.ok:
            raise RuntimeError(
                f"git diff --name-only failed while checking committed paths: {result.stderr}"
            )
        return {Path(line.strip()) for line in result.stdout.splitlines() if line.strip()}

    async def _protected_file_diffs_for_staged_paths(
        self,
        *,
        worktree_path: Path,
        changed_paths: Sequence[str],
    ) -> dict[str, ProtectedFileDiff]:
        diffs: dict[str, ProtectedFileDiff] = {}
        for path in diff_classified_protected_paths(changed_paths):
            old_text = await self._git_show_text(
                worktree_path=worktree_path, refspec=f"HEAD:{path}"
            )
            new_text = await self._git_show_text(worktree_path=worktree_path, refspec=f":{path}")
            unified_diff = await self._git_diff_text(
                worktree_path=worktree_path,
                args=["diff", "--cached", "--unified=0", "--", path],
            )
            diffs[path] = ProtectedFileDiff(
                path=path,
                old_text=old_text,
                new_text=new_text,
                unified_diff=unified_diff,
            )
        return diffs

    async def _protected_file_diffs_for_committed_paths(
        self,
        *,
        worktree_path: Path,
        base_ref: str,
        changed_paths: Sequence[str],
    ) -> dict[str, ProtectedFileDiff]:
        diffs: dict[str, ProtectedFileDiff] = {}
        for path in diff_classified_protected_paths(changed_paths):
            old_text = await self._git_show_text(
                worktree_path=worktree_path,
                refspec=f"{base_ref}:{path}",
            )
            new_text = await self._git_show_text(
                worktree_path=worktree_path, refspec=f"HEAD:{path}"
            )
            unified_diff = await self._git_diff_text(
                worktree_path=worktree_path,
                args=["diff", "--unified=0", f"{base_ref}..HEAD", "--", path],
            )
            diffs[path] = ProtectedFileDiff(
                path=path,
                old_text=old_text,
                new_text=new_text,
                unified_diff=unified_diff,
            )
        return diffs

    async def _git_show_text(self, *, worktree_path: Path, refspec: str) -> str | None:
        result = await self._runner.run(
            [
                "git",
                *git_safe_directory_config_args(worktree_path),
                "-C",
                str(worktree_path),
                "show",
                refspec,
            ]
        )
        return result.stdout if result.ok else None

    async def _git_diff_text(
        self,
        *,
        worktree_path: Path,
        args: Sequence[str],
    ) -> str | None:
        result = await self._runner.run(
            [
                "git",
                *git_safe_directory_config_args(worktree_path),
                "-C",
                str(worktree_path),
                *args,
            ]
        )
        return result.stdout if result.ok else None

    async def _verify_recovered_post_agent_commit(
        self,
        *,
        workspace_id: str,
        worktree_path: Path,
        base_commit: str,
        owned_paths: list[str],
        expected_status: WorkspaceStatus,
    ) -> bool:
        changed_paths = sorted(
            path.as_posix()
            for path in await self._committed_paths_since(worktree_path, base_commit)
        )
        if not changed_paths:
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=expected_status,
                failure_reason=FailureReason.agent_failure,
                message=(
                    "AWF recovered a missing Git HEAD object but recovered no "
                    f"committed paths relative to base {base_commit[:10]}"
                )[:2000],
                reason_code=GIT_OBJECT_MISSING_RECOVERED_REASON_CODE,
                details={"recovered_stage": "post_agent_commit"},
            )
            return False
        if await self._fail_if_plan_only_paths(
            workspace_id=workspace_id,
            changed_paths=changed_paths,
            expected_status=expected_status,
        ):
            return False
        protected_file_diffs = await self._protected_file_diffs_for_committed_paths(
            worktree_path=worktree_path,
            base_ref=base_commit,
            changed_paths=changed_paths,
        )
        violations = find_protected_quality_gate_changes(
            changed_paths=changed_paths,
            owned_paths=owned_paths,
            protected_file_diffs=protected_file_diffs,
        )
        if violations:
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=expected_status,
                failure_reason=FailureReason.policy_failure,
                reason_code="QUALITY_GATE_POLICY_CHANGED",
                message=quality_gate_violation_message(violations)[:2000],
            )
            return False
        ancestor = await self._runner.run(
            [
                "git",
                *git_safe_directory_config_args(worktree_path),
                "-C",
                str(worktree_path),
                "merge-base",
                "--is-ancestor",
                base_commit,
                "HEAD",
            ]
        )
        if not ancestor.ok:
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=expected_status,
                failure_reason=FailureReason.agent_failure,
                message=(
                    "AWF recovered a missing Git HEAD object but recovered HEAD "
                    f"does not descend from base commit {base_commit[:10]}"
                )[:2000],
            )
            return False
        return True

    async def _verify_recovered_post_agent_commit_or_mark_failed(
        self,
        *,
        workspace_id: str,
        worktree_path: Path,
        base_commit: str,
        owned_paths: list[str],
        expected_status: WorkspaceStatus,
    ) -> bool:
        try:
            return await self._verify_recovered_post_agent_commit(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                base_commit=base_commit,
                owned_paths=owned_paths,
                expected_status=expected_status,
            )
        except Exception as exc:
            _log.exception(
                "executor.commit_step_missing_head_recovery_verification_failed",
                workspace_id=workspace_id,
            )
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=expected_status,
                failure_reason=FailureReason.infrastructure_failure,
                message=(f"post-agent missing HEAD recovery verification failed: {exc!r}")[:2000],
                reason_code=GIT_OBJECT_MISSING_REASON_CODE,
            )
            return False

    async def _fail_if_plan_only_paths(
        self,
        *,
        workspace_id: str,
        changed_paths: list[str] | tuple[str, ...],
        expected_status: WorkspaceStatus,
        mark_workspace_failed: bool = True,
    ) -> bool:
        if not changed_paths_are_only_internal_plan_artifacts(changed_paths):
            return False
        if not mark_workspace_failed:
            return True
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=expected_status,
            failure_reason=FailureReason.agent_failure,
            message=plan_only_output_message(changed_paths)[:2000],
            reason_code=PLAN_ONLY_OUTPUT_REASON_CODE,
            details={
                "changed_paths": list(changed_paths),
                "reason_code": PLAN_ONLY_OUTPUT_REASON_CODE,
            },
        )
        return True

    async def _fail_if_plan_only_committed_output(
        self,
        *,
        workspace_id: str,
        worktree_path: Path,
        base_commit: str,
        expected_status: WorkspaceStatus,
    ) -> bool:
        changed_paths = sorted(
            path.as_posix()
            for path in await self._committed_paths_since(worktree_path, base_commit)
        )
        return await self._fail_if_plan_only_paths(
            workspace_id=workspace_id,
            changed_paths=changed_paths,
            expected_status=expected_status,
        )

    async def _claim_ready(
        self,
        workspace_id: str,
        *,
        execution_owner_id: str | None = None,
        execution_lease_expires_at: datetime | None = None,
    ) -> Workspace | None:
        """Atomically transition a ready workspace to running before execution."""
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.transition_if_current(
                workspace_id,
                from_status=WorkspaceStatus.ready,
                to=WorkspaceStatus.running,
                reason_code="EXECUTOR_CLAIMED",
            )
            if ws is not None:
                ws.execution_claimed_by = execution_owner_id
                ws.execution_claim_expires_at = execution_lease_expires_at
                await session.commit()
                return ws

            current = await repo.get(workspace_id)
            if current is None:
                _log.warning("executor.skip_unknown", workspace_id=workspace_id)
                return None
            _log.info(
                "executor.skip_not_ready",
                workspace_id=workspace_id,
                status=current.status,
            )
            return None

    async def _update_subphase(self, workspace_id: str, subphase: str) -> None:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            await repo.update_activity(workspace_id, subphase=subphase)
            await session.commit()

    async def _recheck_status(
        self,
        workspace_id: str,
        *,
        expected: WorkspaceStatus,
        action: str,
        reason_code: str = "EXECUTOR_STALE_STATUS",
    ) -> bool:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None:  # pragma: no cover - destroyed mid-flight
                _log.warning(
                    "executor.skip_unknown",
                    workspace_id=workspace_id,
                    action=action,
                )
                return False
            if ws.status == expected.value:
                return True
            await self._record_stale_action_skip(
                repo,
                ws,
                action=action,
                expected=expected,
                reason_code=reason_code,
            )
            if _is_callback_terminal_status(ws.status):
                await self._finish_ignored_stale_callback_operations_in_session(
                    session,
                    workspace_id=workspace_id,
                    callback_source="executor",
                    callback_action=action,
                    expected_status=expected,
                    actual_status=ws.status,
                )
            await session.commit()
            return False

    async def _transition_if_current(
        self,
        workspace_id: str,
        *,
        from_status: WorkspaceStatus,
        to: WorkspaceStatus,
        reason: str,
        action: str,
    ) -> bool:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None:  # pragma: no cover - destroyed mid-flight
                return False
            if ws.status != from_status.value:
                await self._record_stale_action_skip(
                    repo,
                    ws,
                    action=action,
                    expected=from_status,
                    reason_code="EXECUTOR_STALE_STATUS",
                )
                if _is_callback_terminal_status(ws.status):
                    await self._finish_ignored_stale_callback_operations_in_session(
                        session,
                        workspace_id=workspace_id,
                        callback_source="executor",
                        callback_action=action,
                        expected_status=from_status,
                        actual_status=ws.status,
                    )
                await session.commit()
                return False
            await repo.transition(ws, to=to, reason_code=reason)
            await session.commit()
            return True

    async def _record_stale_action_skip(
        self,
        repo: WorkspaceRepository,
        ws: Workspace,
        *,
        action: str,
        expected: WorkspaceStatus,
        reason_code: str,
    ) -> None:
        _log.info(
            "executor.skip_stale_status",
            workspace_id=ws.id,
            action=action,
            expected_status=expected.value,
            status=ws.status,
        )
        if _is_callback_terminal_status(ws.status):
            await repo.record_ignored_stale_callback(
                ws,
                callback_source="executor",
                callback_action=action,
                expected_status=expected,
                reason_code=reason_code,
            )
        await repo.add_event(
            ws,
            event_type="workspace.stale_action_skipped",
            reason_code=reason_code,
            payload={
                "action": action,
                "expected_status": expected.value,
                "actual_status": ws.status,
            },
        )

    async def _record_health_check_failed_event(
        self,
        *,
        workspace_id: str,
        failure: ValidationCommandResult,
    ) -> None:
        metadata = failure.metadata
        stream_ids = metadata.get("stream_ids")
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None or ws.status != WorkspaceStatus.validating.value:
                return
            await repo.add_event(
                ws,
                event_type="workspace.health_check_failed",
                reason_code=failure.reason_code,
                payload={
                    "healthcheck_name": _metadata_str(metadata, "healthcheck_name"),
                    "healthcheck_kind": _metadata_str(metadata, "healthcheck_kind"),
                    "target": _metadata_str(metadata, "target") or failure.command,
                    "attempts": _metadata_int(metadata, "attempts"),
                    "timeout_seconds": _metadata_number(metadata, "timeout_seconds"),
                    "stream_ids": dict(stream_ids) if isinstance(stream_ids, dict) else {},
                },
            )
            await session.commit()

    async def _record_post_agent_commit_format_repair(
        self,
        *,
        workspace_id: str,
        repaired_paths: Sequence[str],
        retry_outcome: str,
        repair_strategy: str = "deterministic",
        failed_hooks: Sequence[str] = (),
        formatter_paths: Sequence[str] = (),
        normalizer_paths: Sequence[str] = (),
        restaged_paths: Sequence[str] = (),
        reason_code: str,
    ) -> None:
        """Emit the structured event describing a post-agent commit repair."""
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None:  # pragma: no cover - destroyed mid-flight
                return
            await repo.add_event(
                ws,
                event_type=POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE,
                reason_code=reason_code,
                payload={
                    "repaired_paths": list(repaired_paths),
                    "restaged_paths": list(restaged_paths),
                    "formatter_paths": list(formatter_paths),
                    "normalizer_paths": list(normalizer_paths),
                    "failed_hooks": list(failed_hooks),
                    "repair_strategy": repair_strategy,
                    "retry_outcome": retry_outcome,
                },
            )
            await session.commit()

    async def _run_post_agent_commit_repair(
        self,
        *,
        workspace_id: str,
        worktree_path: Path,
        commit_result: CommandResult,
        classification: _PostAgentCommitClassification,
        staged_paths: Sequence[str],
        run_commit: Callable[[], Awaitable[CommandResult]],
        git_in_worktree: Callable[[list[str]], Awaitable[CommandResult]],
        adapter: AgentAdapter,
        compose_project: str,
        compose_file: Path,
        model: str | None,
        allow_agent_repair: bool,
        ws: Workspace,
        command_evidence: list[str],
    ) -> None:
        """Repair a failed post-agent pre-commit run and retry the commit once."""
        if classification.repair_strategy == "deterministic":
            await self._run_post_agent_deterministic_precommit_repair(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                commit_result=commit_result,
                classification=classification,
                staged_paths=staged_paths,
                run_commit=run_commit,
                git_in_worktree=git_in_worktree,
            )
            return

        if classification.autofix_repair_files:
            repaired = await self._run_post_agent_autofixable_precommit_repair(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                commit_result=commit_result,
                classification=classification,
                staged_paths=staged_paths,
                run_commit=run_commit,
                git_in_worktree=git_in_worktree,
            )
            if repaired:
                return

        if classification.repair_strategy == "agent" and allow_agent_repair:
            await self._run_post_agent_semantic_precommit_repair(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                commit_result=commit_result,
                classification=classification,
                staged_paths=staged_paths,
                run_commit=run_commit,
                git_in_worktree=git_in_worktree,
                adapter=adapter,
                compose_project=compose_project,
                compose_file=compose_file,
                model=model,
                ws=ws,
                command_evidence=command_evidence,
            )
            return

        reported_repair_strategy = (
            "agent_skipped"
            if classification.repair_strategy == "agent" and not allow_agent_repair
            else classification.repair_strategy
        )
        raise _PostAgentCommitStepError(
            stage="git commit",
            result=commit_result,
            classification=classification,
            repair_strategy=reported_repair_strategy,
        )

    async def _run_post_agent_deterministic_precommit_repair(
        self,
        *,
        workspace_id: str,
        worktree_path: Path,
        commit_result: CommandResult,
        classification: _PostAgentCommitClassification,
        staged_paths: Sequence[str],
        run_commit: Callable[[], Awaitable[CommandResult]],
        git_in_worktree: Callable[[list[str]], Awaitable[CommandResult]],
    ) -> None:
        staged_python_set = {
            path for path in staged_paths if path.endswith(".py") or path.endswith(".pyi")
        }
        repair_paths = [
            path for path in classification.format_repair_files if path in staged_python_set
        ]
        if _AWF_RUFF_FORMAT_CHECK_HOOK_ID in classification.failed_hooks and not repair_paths:
            await self._record_post_agent_commit_format_repair(
                workspace_id=workspace_id,
                repaired_paths=[],
                restaged_paths=[],
                formatter_paths=list(classification.format_repair_files),
                normalizer_paths=classification.normalizer_repair_files,
                failed_hooks=classification.failed_hooks,
                repair_strategy="deterministic",
                retry_outcome="skipped",
                reason_code=classification.reason_code,
            )
            raise _PostAgentCommitStepError(
                stage="git commit",
                result=commit_result,
                classification=classification,
                format_repair_attempted=True,
                precommit_repair_attempted=True,
                repair_strategy="deterministic",
            )

        if repair_paths:
            format_result = await self._runner.run(
                [
                    "uv",
                    "run",
                    "--python",
                    "3.12",
                    "--extra",
                    "dev",
                    "ruff",
                    "format",
                    "--",
                    *repair_paths,
                ],
                cwd=str(worktree_path),
            )
            if not format_result.ok:
                await self._record_post_agent_commit_format_repair(
                    workspace_id=workspace_id,
                    repaired_paths=repair_paths,
                    restaged_paths=[],
                    formatter_paths=repair_paths,
                    normalizer_paths=classification.normalizer_repair_files,
                    failed_hooks=classification.failed_hooks,
                    repair_strategy="deterministic",
                    retry_outcome="error",
                    reason_code=POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
                )
                raise _PostAgentCommitStepError(
                    stage="ruff format",
                    result=format_result,
                    classification=classification,
                    format_repair_attempted=True,
                    precommit_repair_attempted=True,
                    repair_strategy="deterministic",
                    reason_code_override=POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
                )

        restage_paths = list(staged_paths)
        add_again = await git_in_worktree(["add", "--", *restage_paths])
        await self._repair_agent_git_ownership(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            reason="post_agent_format_repair_add",
        )
        if not add_again.ok:
            await self._record_post_agent_commit_format_repair(
                workspace_id=workspace_id,
                repaired_paths=repair_paths,
                restaged_paths=restage_paths,
                formatter_paths=repair_paths,
                normalizer_paths=classification.normalizer_repair_files,
                failed_hooks=classification.failed_hooks,
                repair_strategy="deterministic",
                retry_outcome="error",
                reason_code=POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
            )
            raise _PostAgentCommitStepError(
                stage="git add",
                result=add_again,
                classification=classification,
                format_repair_attempted=True,
                precommit_repair_attempted=True,
                repair_strategy="deterministic",
                reason_code_override=POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
            )
        retry_result = await run_commit()
        await self._repair_agent_git_ownership(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            reason="post_agent_format_repair_commit",
        )
        if retry_result.ok:
            await self._record_post_agent_commit_format_repair(
                workspace_id=workspace_id,
                repaired_paths=repair_paths,
                restaged_paths=restage_paths,
                formatter_paths=repair_paths,
                normalizer_paths=classification.normalizer_repair_files,
                failed_hooks=classification.failed_hooks,
                repair_strategy="deterministic",
                retry_outcome="succeeded",
                reason_code=classification.reason_code,
            )
            return

        retry_classification = _classify_post_agent_commit_failure(retry_result)
        await self._record_post_agent_commit_format_repair(
            workspace_id=workspace_id,
            repaired_paths=repair_paths,
            restaged_paths=restage_paths,
            formatter_paths=repair_paths,
            normalizer_paths=classification.normalizer_repair_files,
            failed_hooks=classification.failed_hooks,
            repair_strategy="deterministic",
            retry_outcome="failed",
            reason_code=classification.reason_code,
        )
        raise _PostAgentCommitStepError(
            stage="git commit",
            result=retry_result,
            classification=retry_classification,
            format_repair_attempted=True,
            precommit_repair_attempted=True,
            repair_strategy="deterministic",
            reason_code_override=(
                POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE
                if retry_classification.reason_code
                == POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE
                else None
            ),
        )

    async def _run_post_agent_autofixable_precommit_repair(
        self,
        *,
        workspace_id: str,
        worktree_path: Path,
        commit_result: CommandResult,
        classification: _PostAgentCommitClassification,
        staged_paths: Sequence[str],
        run_commit: Callable[[], Awaitable[CommandResult]],
        git_in_worktree: Callable[[list[str]], Awaitable[CommandResult]],
    ) -> bool:
        del commit_result
        staged_python_set = {
            path for path in staged_paths if path.endswith(".py") or path.endswith(".pyi")
        }
        repair_paths = [
            path for path in classification.autofix_repair_files if path in staged_python_set
        ]
        if not repair_paths:
            await self._record_post_agent_commit_format_repair(
                workspace_id=workspace_id,
                repaired_paths=[],
                restaged_paths=[],
                formatter_paths=classification.format_repair_files,
                normalizer_paths=classification.normalizer_repair_files,
                failed_hooks=classification.failed_hooks,
                repair_strategy="deterministic_autofix",
                retry_outcome="skipped",
                reason_code=classification.reason_code,
            )
            return False

        fix_result = await self._runner.run(
            [
                "uv",
                "run",
                "--python",
                "3.12",
                "--extra",
                "dev",
                "ruff",
                "check",
                "--fix",
                "--",
                *repair_paths,
            ],
            cwd=str(worktree_path),
        )
        if not fix_result.ok:
            await self._record_post_agent_commit_format_repair(
                workspace_id=workspace_id,
                repaired_paths=repair_paths,
                restaged_paths=[],
                formatter_paths=classification.format_repair_files,
                normalizer_paths=classification.normalizer_repair_files,
                failed_hooks=classification.failed_hooks,
                repair_strategy="deterministic_autofix",
                retry_outcome="error",
                reason_code=POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
            )
            raise _PostAgentCommitStepError(
                stage="ruff check --fix",
                result=fix_result,
                classification=classification,
                format_repair_attempted=True,
                precommit_repair_attempted=True,
                repair_strategy="deterministic_autofix",
                reason_code_override=POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
            )

        restage_paths = list(staged_paths)
        add_again = await git_in_worktree(["add", "--", *restage_paths])
        await self._repair_agent_git_ownership(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            reason="post_agent_autofix_repair_add",
        )
        if not add_again.ok:
            await self._record_post_agent_commit_format_repair(
                workspace_id=workspace_id,
                repaired_paths=repair_paths,
                restaged_paths=restage_paths,
                formatter_paths=classification.format_repair_files,
                normalizer_paths=classification.normalizer_repair_files,
                failed_hooks=classification.failed_hooks,
                repair_strategy="deterministic_autofix",
                retry_outcome="error",
                reason_code=POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
            )
            raise _PostAgentCommitStepError(
                stage="git add",
                result=add_again,
                classification=classification,
                format_repair_attempted=True,
                precommit_repair_attempted=True,
                repair_strategy="deterministic_autofix",
                reason_code_override=POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
            )

        retry_result = await run_commit()
        await self._repair_agent_git_ownership(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            reason="post_agent_autofix_repair_commit",
        )
        if retry_result.ok:
            await self._record_post_agent_commit_format_repair(
                workspace_id=workspace_id,
                repaired_paths=repair_paths,
                restaged_paths=restage_paths,
                formatter_paths=classification.format_repair_files,
                normalizer_paths=classification.normalizer_repair_files,
                failed_hooks=classification.failed_hooks,
                repair_strategy="deterministic_autofix",
                retry_outcome="succeeded",
                reason_code=classification.reason_code,
            )
            return True

        retry_classification = _classify_post_agent_commit_failure(retry_result)
        await self._record_post_agent_commit_format_repair(
            workspace_id=workspace_id,
            repaired_paths=repair_paths,
            restaged_paths=restage_paths,
            formatter_paths=classification.format_repair_files,
            normalizer_paths=classification.normalizer_repair_files,
            failed_hooks=classification.failed_hooks,
            repair_strategy="deterministic_autofix",
            retry_outcome="failed",
            reason_code=classification.reason_code,
        )
        raise _PostAgentCommitStepError(
            stage="git commit",
            result=retry_result,
            classification=retry_classification,
            format_repair_attempted=True,
            precommit_repair_attempted=True,
            repair_strategy="deterministic_autofix",
        )

    async def _run_post_agent_semantic_precommit_repair(
        self,
        *,
        workspace_id: str,
        worktree_path: Path,
        commit_result: CommandResult,
        classification: _PostAgentCommitClassification,
        staged_paths: Sequence[str],
        run_commit: Callable[[], Awaitable[CommandResult]],
        git_in_worktree: Callable[[list[str]], Awaitable[CommandResult]],
        adapter: AgentAdapter,
        compose_project: str,
        compose_file: Path,
        model: str | None,
        ws: Workspace,
        command_evidence: list[str],
    ) -> None:
        del commit_result
        prompt = _build_post_agent_precommit_repair_prompt(
            classification=classification,
            staged_paths=staged_paths,
        )
        repair_error: AgentRunError | None = None
        try:
            repair_result = await adapter.run(
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=prompt,
                model=model,
                workspace_id=workspace_id,
                log_source="post_agent_precommit_repair",
            )
            append_command_evidence(
                command_evidence,
                stdout=repair_result.stdout,
                stderr=repair_result.stderr,
            )
        except AgentRunError as exc:
            repair_error = exc
            append_command_evidence(
                command_evidence,
                stdout=exc.result.stdout,
                stderr=exc.result.stderr,
            )

        add_again = await git_in_worktree(["add", "-A"])
        await self._repair_agent_git_ownership(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            reason="post_agent_precommit_repair_add",
        )
        if not add_again.ok:
            await self._record_post_agent_commit_format_repair(
                workspace_id=workspace_id,
                repaired_paths=[],
                restaged_paths=[],
                formatter_paths=classification.format_repair_files,
                normalizer_paths=classification.normalizer_repair_files,
                failed_hooks=classification.failed_hooks,
                repair_strategy="agent",
                retry_outcome="error",
                reason_code=POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
            )
            raise _PostAgentCommitStepError(
                stage="git add",
                result=add_again,
                classification=classification,
                precommit_repair_attempted=True,
                repair_strategy="agent",
                reason_code_override=POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
            )

        cached = await git_in_worktree(["diff", "--cached", "--name-only"])
        if not cached.ok:
            await self._record_post_agent_commit_format_repair(
                workspace_id=workspace_id,
                repaired_paths=[],
                restaged_paths=[],
                formatter_paths=classification.format_repair_files,
                normalizer_paths=classification.normalizer_repair_files,
                failed_hooks=classification.failed_hooks,
                repair_strategy="agent",
                retry_outcome="error",
                reason_code=POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
            )
            raise _PostAgentCommitStepError(
                stage="git diff --cached",
                result=cached,
                classification=classification,
                precommit_repair_attempted=True,
                repair_strategy="agent",
                reason_code_override=POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE,
            )
        repair_staged_paths = _git_name_lines(cached.stdout) if cached.stdout.strip() else []
        supply_chain_result = await self._refresh_supply_chain_policy_for_workspace(
            workspace_id=workspace_id,
            command_evidence=command_evidence,
            changed_paths=repair_staged_paths,
        )
        if supply_chain_result.policy_blocked:
            result = CommandResult(
                returncode=1,
                stdout="",
                stderr=_supply_chain_block_message(supply_chain_result.findings),
            )
            await self._record_post_agent_commit_format_repair(
                workspace_id=workspace_id,
                repaired_paths=[],
                restaged_paths=repair_staged_paths,
                formatter_paths=classification.format_repair_files,
                normalizer_paths=classification.normalizer_repair_files,
                failed_hooks=classification.failed_hooks,
                repair_strategy="agent",
                retry_outcome="error",
                reason_code="SUPPLY_CHAIN_POLICY_BLOCKED",
            )
            raise _PostAgentCommitStepError(
                stage="post-agent pre-commit repair policy",
                result=result,
                classification=classification,
                precommit_repair_attempted=True,
                repair_strategy="agent",
                reason_code_override="SUPPLY_CHAIN_POLICY_BLOCKED",
                failure_reason_override=FailureReason.policy_failure,
            )
        # Always evaluate the final repair diff. A semantic repair can remove
        # the real implementation change while leaving only hook-normalized
        # plan artifacts staged, and that must not become a PR.
        if await self._fail_if_plan_only_paths(
            workspace_id=workspace_id,
            changed_paths=repair_staged_paths,
            expected_status=WorkspaceStatus.running,
            mark_workspace_failed=False,
        ):
            result = CommandResult(
                returncode=1,
                stdout="",
                stderr=plan_only_output_message(repair_staged_paths),
            )
            await self._record_post_agent_commit_format_repair(
                workspace_id=workspace_id,
                repaired_paths=[],
                restaged_paths=repair_staged_paths,
                formatter_paths=classification.format_repair_files,
                normalizer_paths=classification.normalizer_repair_files,
                failed_hooks=classification.failed_hooks,
                repair_strategy="agent",
                retry_outcome="error",
                reason_code=PLAN_ONLY_OUTPUT_REASON_CODE,
            )
            raise _PostAgentCommitStepError(
                stage="post-agent pre-commit repair policy",
                result=result,
                classification=classification,
                precommit_repair_attempted=True,
                repair_strategy="agent",
                reason_code_override=PLAN_ONLY_OUTPUT_REASON_CODE,
                failure_reason_override=FailureReason.agent_failure,
            )
        violations = find_protected_quality_gate_changes(
            changed_paths=repair_staged_paths,
            owned_paths=list(ws.owned_paths),
            protected_file_diffs=await self._protected_file_diffs_for_staged_paths(
                worktree_path=worktree_path,
                changed_paths=repair_staged_paths,
            ),
        )
        if violations:
            result = CommandResult(
                returncode=1,
                stdout="",
                stderr=quality_gate_violation_message(violations),
            )
            await self._record_post_agent_commit_format_repair(
                workspace_id=workspace_id,
                repaired_paths=[],
                restaged_paths=repair_staged_paths,
                formatter_paths=classification.format_repair_files,
                normalizer_paths=classification.normalizer_repair_files,
                failed_hooks=classification.failed_hooks,
                repair_strategy="agent",
                retry_outcome="error",
                reason_code="QUALITY_GATE_POLICY_CHANGED",
            )
            raise _PostAgentCommitStepError(
                stage="post-agent pre-commit repair policy",
                result=result,
                classification=classification,
                precommit_repair_attempted=True,
                repair_strategy="agent",
                reason_code_override="QUALITY_GATE_POLICY_CHANGED",
                failure_reason_override=FailureReason.policy_failure,
            )

        retry_result = await run_commit()
        await self._repair_agent_git_ownership(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            reason="post_agent_precommit_repair_commit",
        )
        if retry_result.ok:
            await self._record_post_agent_commit_format_repair(
                workspace_id=workspace_id,
                repaired_paths=[],
                restaged_paths=repair_staged_paths,
                formatter_paths=classification.format_repair_files,
                normalizer_paths=classification.normalizer_repair_files,
                failed_hooks=classification.failed_hooks,
                repair_strategy="agent",
                retry_outcome=(
                    "agent_error_partial_commit" if repair_error is not None else "succeeded"
                ),
                reason_code=classification.reason_code,
            )
            return

        retry_classification = _classify_post_agent_commit_failure(retry_result)
        if retry_classification.repair_strategy == "deterministic" and repair_error is None:
            await self._record_post_agent_commit_format_repair(
                workspace_id=workspace_id,
                repaired_paths=[],
                restaged_paths=repair_staged_paths,
                formatter_paths=classification.format_repair_files,
                normalizer_paths=classification.normalizer_repair_files,
                failed_hooks=classification.failed_hooks,
                repair_strategy="agent",
                retry_outcome="failed",
                reason_code=POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE,
            )
            await self._run_post_agent_deterministic_precommit_repair(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                commit_result=retry_result,
                classification=retry_classification,
                staged_paths=repair_staged_paths,
                run_commit=run_commit,
                git_in_worktree=git_in_worktree,
            )
            return

        await self._record_post_agent_commit_format_repair(
            workspace_id=workspace_id,
            repaired_paths=[],
            restaged_paths=repair_staged_paths,
            formatter_paths=classification.format_repair_files,
            normalizer_paths=classification.normalizer_repair_files,
            failed_hooks=classification.failed_hooks,
            repair_strategy="agent",
            retry_outcome="error" if repair_error is not None else "failed",
            reason_code=classification.reason_code,
        )
        if repair_error is not None:
            raise _PostAgentCommitStepError(
                stage="post-agent pre-commit repair",
                result=repair_error.result,
                classification=classification,
                precommit_repair_attempted=True,
                repair_strategy="agent",
                reason_code_override=POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE,
            ) from repair_error
        raise _PostAgentCommitStepError(
            stage="git commit",
            result=retry_result,
            classification=retry_classification,
            precommit_repair_attempted=True,
            repair_strategy="agent",
        )

    async def _mark_post_agent_commit_failed(
        self,
        *,
        workspace_id: str,
        error: _PostAgentCommitStepError,
        agent_run_reason_code: str | None,
        agent_run_details: Mapping[str, Any] | None,
        agent_exit_note: str | None,
        upstream_failure_reason: FailureReason | None,
    ) -> None:
        """Route a ``_PostAgentCommitStepError`` to ``_mark_failed`` with
        structured reason codes.

        When the agent already failed upstream (e.g.
        ``AgentRunError(reason_code=AGENT_IDLE_TIMEOUT)``), the agent's
        reason code AND ``FailureReason.agent_failure`` classification
        win on the terminal event so the workspace mirrors the no-commit
        agent failure path. The commit-step diagnostics live under
        ``details["post_agent_commit"]`` for observability without
        overwriting the original classification.

        ``upstream_failure_reason`` is the explicit signal for that
        branch. ``agent_run_reason_code`` alone is not sufficient: the
        recovered missing-HEAD path also sets a reason code
        (``GIT_OBJECT_MISSING_RECOVERED``), but its semantics are
        git/infrastructure recovery, not an agent/provider failure — so
        a downstream commit failure must NOT be re-classified as
        ``agent_failure`` and MUST NOT queue provider recovery.
        """
        classification = error.classification
        if error.reason_code_override is not None:
            commit_reason_code = error.reason_code_override
        elif classification is not None:
            commit_reason_code = classification.reason_code
        else:
            commit_reason_code = POST_AGENT_GIT_ADD_FAILED_REASON_CODE
        commit_details: dict[str, Any] = {
            "stage": error.stage,
            "reason_code": commit_reason_code,
            "returncode": error.result.returncode,
            "format_repair_attempted": error.format_repair_attempted,
            "precommit_repair_attempted": error.precommit_repair_attempted,
        }
        if error.repair_strategy:
            commit_details["repair_strategy"] = error.repair_strategy
        if classification is not None:
            if classification.failed_hooks:
                commit_details["failed_hooks"] = list(classification.failed_hooks)
            if classification.format_repair_files:
                commit_details["format_repair_files"] = list(classification.format_repair_files)
            if classification.normalizer_repair_files:
                commit_details["normalizer_repair_files"] = list(
                    classification.normalizer_repair_files
                )
            if classification.deterministic_hooks:
                commit_details["deterministic_hooks"] = list(classification.deterministic_hooks)
            if classification.semantic_hooks:
                commit_details["semantic_hooks"] = list(classification.semantic_hooks)
        # ``classification`` holds the parsed output for the FAILING commit step:
        # - for ``ruff format`` crashes and post-format ``git add`` failures it
        #   is the FIRST ``git commit`` output (and stays stale — "Would
        #   reformat..." — even though the real failure is elsewhere).
        # - for retry-commit failures it is ``retry_classification`` (parsed from
        #   the retry output), so ``failed_hooks`` / ``format_repair_files``
        #   reflect the retry, not the initial commit.
        # Trust the classification summary only when this is a commit-stage
        # failure with no override; otherwise prefer the actual sub-step output.
        if (
            classification is not None
            and error.stage == "git commit"
            and error.reason_code_override is None
        ):
            summary = classification.summary
        else:
            summary = (error.result.stderr or error.result.stdout or "").strip()
        if summary:
            commit_details["summary"] = redact_audit_text(summary, limit=1000)

        if upstream_failure_reason == FailureReason.agent_failure:
            details: dict[str, Any] = dict(agent_run_details or {})
            details["post_agent_commit"] = commit_details
            base_message = f"post-agent {error.stage} failed (exit={error.result.returncode})"
            summary_text = commit_details.get("summary")
            if summary_text:
                base_message = f"{base_message}: {summary_text}"
            if agent_exit_note is not None:
                base_message = f"{base_message}; {agent_exit_note}"
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.running,
                failure_reason=FailureReason.agent_failure,
                message=base_message[:2000],
                reason_code=agent_run_reason_code,
                details=details,
            )
            await self._prepare_provider_recovery(workspace_id)
            return

        _log.warning(
            "executor.post_agent_commit_failed",
            workspace_id=workspace_id,
            stage=error.stage,
            reason_code=commit_reason_code,
            returncode=error.result.returncode,
            format_repair_attempted=error.format_repair_attempted,
        )
        failure_reason = error.failure_reason_override or FailureReason.infrastructure_failure
        base_message = f"post-agent {error.stage} failed (exit={error.result.returncode})"
        summary_text = commit_details.get("summary")
        if summary_text:
            base_message = f"{base_message}: {summary_text}"
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=failure_reason,
            message=base_message[:2000],
            reason_code=commit_reason_code,
            details={"post_agent_commit": commit_details},
        )

    async def _mark_failed(
        self,
        *,
        workspace_id: str,
        from_status: WorkspaceStatus,
        failure_reason: FailureReason,
        message: str,
        reason_code: str | None = None,
        details: Mapping[str, Any] | None = None,
        salvage: Mapping[str, Any] | None = None,
    ) -> None:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None:  # pragma: no cover
                return
            if ws.status != from_status.value:
                # Already moved (e.g. cancelled) — respect it.
                await self._record_stale_action_skip(
                    repo,
                    ws,
                    action="mark_failed",
                    expected=from_status,
                    reason_code="EXECUTOR_MARK_FAILED_SKIPPED",
                )
                if _is_callback_terminal_status(ws.status):
                    await self._finish_ignored_stale_callback_operations_in_session(
                        session,
                        workspace_id=workspace_id,
                        callback_source="executor",
                        callback_action="mark_failed",
                        expected_status=from_status,
                        actual_status=ws.status,
                    )
                await session.commit()
                return
            safe_message = redact_audit_text(message, limit=2000)
            ws.failure_reason = failure_reason.value
            ws.failure_message = safe_message
            if reason_code == EXEC_PROCESS_CLEANUP_FAILED:
                await repo.add_event(
                    ws,
                    event_type="workspace.exec_process_cleanup_failed",
                    reason_code=EXEC_PROCESS_CLEANUP_FAILED,
                    payload={"message": safe_message[:1000]},
                )
            payload: dict[str, Any] | None = None
            if details is not None or salvage is not None:
                payload = {
                    "failure_reason": failure_reason.value,
                    "reason_code": reason_code or failure_reason.value.upper(),
                    "message": safe_message,
                }
                if details is not None:
                    payload["details"] = dict(details)
                if salvage is not None:
                    payload["salvage"] = dict(salvage)
            await repo.transition(
                ws,
                to=WorkspaceStatus.failed,
                reason_code=reason_code or failure_reason.value.upper(),
                payload=payload,
            )
            await session.commit()

    async def _prepare_provider_recovery(self, workspace_id: str) -> None:
        async with self._session_factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            configured_default_model: str | None = None
            if workspace is not None:
                try:
                    defaults = self._defaults_for(AgentRuntime(workspace.agent))
                except ValueError:
                    defaults = None
                configured_default_model = defaults.model if defaults is not None else None
            result = await create_provider_recovery_attempt_row(
                session,
                workspace_id,
                effective_default_model=configured_default_model,
            )
            if result is None or result == "terminal" or result == "stale":
                await session.commit()
                return
            await session.commit()
            _log.info(
                "executor.provider_recovery_prepared",
                workspace_id=workspace_id,
                new_workspace_id=result.new_workspace_id,
                action=result.action,
                reason_code=result.reason_code,
            )

    async def _start_validation_run(
        self,
        *,
        workspace_id: str,
        profile: WorkspaceProfile,
        base_commit: str | None,
        workspace_head_sha: str | None,
        target_branch: str | None,
        target_head_sha: str | None,
        tier: int,
        coverage_evidence_status: str | None = None,
        coverage_evidence_reason_code: str | None = None,
    ) -> str:
        command_records = _validation_run_command_records(
            profile=profile,
            phase_names=("post_agent", "validate"),
            run_healthchecks=True,
            coverage_evidence_status=coverage_evidence_status,
            coverage_evidence_reason_code=coverage_evidence_reason_code,
        )
        async with self._session_factory() as session:
            attempt = await TaskAttemptRepository(session).get_by_workspace_id(workspace_id)
            run = await ValidationRunRepository(session).start(
                workspace_id=workspace_id,
                attempt_id=attempt.id if attempt is not None else None,
                tier=tier,
                commands=command_records,
                base_commit=base_commit,
                base_sha=base_commit,
                workspace_head_sha=workspace_head_sha,
                target_branch=target_branch,
                target_head_sha=target_head_sha,
                profile_name=profile.name,
                profile_version=profile.version,
                profile_source=profile.source,
                resolved_profile_digest=resolved_profile_digest(profile),
                environment_identity_digest=environment_identity_digest(profile),
                environment_identity_inputs=environment_identity_inputs(profile),
                log_stream_refs=_validation_run_log_stream_refs(command_records),
                started_at=datetime.now(UTC),
            )
            await session.commit()
            return run.id

    async def _capture_workspace_head_sha(
        self,
        *,
        workspace_id: str,
        worktree_path: Path,
    ) -> str | None:
        result = await self._runner.run(
            ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
        )
        head_sha = result.stdout.strip()
        if result.ok and head_sha:
            return head_sha
        _log.warning(
            "executor.validation_workspace_head_sha_capture_failed",
            workspace_id=workspace_id,
            returncode=result.returncode,
            stderr=result.stderr[:400],
        )
        return None

    async def _begin_rebase_recovery_operation(
        self,
        *,
        workspace_id: str,
        base_branch: str,
        remote_branch: str,
        reason: str,
        reason_code: str,
        source_base_sha: str | None,
        source_head_sha: str | None,
        recovery_payload: Mapping[str, Any],
    ) -> MonitorOperationHandle | None:
        session_factory_obj: object = self._session_factory
        if not callable(session_factory_obj):  # test-only lightweight executor
            return None
        session_factory = cast(async_sessionmaker[AsyncSession], session_factory_obj)
        async with session_factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            if workspace is None:  # pragma: no cover - destroyed mid-recovery
                return None
            repo = OperationRepository(session)
            existing_rebase = await self._find_active_rebase_recovery_operation(
                repo,
                workspace_id=workspace_id,
                recovery_payload=recovery_payload,
            )
            if existing_rebase is not None:
                await repo.start(existing_rebase)
                await session.commit()
                return MonitorOperationHandle(
                    operation_id=existing_rebase.id,
                    should_finish=True,
                )
            pr_number = _int_or_none(recovery_payload.get("pr_number")) or workspace.pr_number
            if pr_number is None:
                pr_number = 0
            payload = build_monitor_operation_payload(
                workspace=workspace,
                action="rebase_only",
                requested_action="rebase",
                reason=reason,
                reason_code=reason_code,
                pr_number=pr_number,
                source_head_sha=source_head_sha or workspace.monitor_last_commit_sha,
                source_base_sha=source_base_sha or workspace.base_commit,
                target_branch=base_branch,
                remote_branch=remote_branch,
                recovery_mode="rebase_only",
            )
            handle = await create_or_start_monitor_operation(
                session,
                workspace_id=workspace_id,
                operation_type=OperationType.rebase,
                payload=payload,
                idempotency_key=monitor_operation_idempotency_key(
                    workspace_id=workspace_id,
                    action="rebase_only",
                    pr_number=pr_number,
                    reason_code=reason_code,
                    source_head_sha=source_head_sha or workspace.monitor_last_commit_sha,
                    source_base_sha=source_base_sha or workspace.base_commit,
                ),
                status=OperationStatus.running,
            )
            await session.commit()
            return handle

    async def _find_active_rebase_recovery_operation(
        self,
        repo: OperationRepository,
        *,
        workspace_id: str,
        recovery_payload: Mapping[str, Any],
    ) -> Operation | None:
        for payload_identity in _rebase_recovery_operation_payload_identities(recovery_payload):
            operation = await repo.find_active_matching_payload(
                workspace_id=workspace_id,
                operation_type=OperationType.rebase,
                payload_identity=payload_identity,
            )
            if operation is not None and _is_validate_only_recovery_payload(operation.payload):
                return operation
        return None

    async def _finish_rebase_recovery_operation(
        self,
        operation: MonitorOperationHandle | None,
        *,
        status: OperationStatus,
        result: Mapping[str, Any],
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if operation is None or not operation.should_finish:
            return
        async with self._session_factory() as session:
            await finish_monitor_operation(
                session,
                operation_id=operation.operation_id,
                status=status,
                result=result,
                error_code=error_code,
                error_message=error_message,
            )
            await session.commit()

    async def _run_monitor_rebase_recovery(
        self,
        *,
        workspace_id: str,
        worktree_path: Path,
        base_branch: str,
        branch_name: str,
        remote_branch: str,
        reason: str,
        recovery_payload: Mapping[str, Any] | None = None,
    ) -> _RebaseRecoveryResult:
        """Rebase an already-open PR branch onto the latest target branch.

        The PR monitor dispatches ``recovery_mode='rebase_only'`` when a
        merge candidate is stale because the target branch moved. Older
        executor code treated that as validation-only, which left the same
        stale reason active and caused an infinite
        ``monitoring_pr -> ready -> running -> validating`` loop. This
        recovery performs the real branch update once, pushes it, records a
        rebase operation, and lets the normal Tier 2 validation pass prove the
        rebased branch.
        """

        async def git(args: list[str]) -> CommandResult:
            return await self._runner.run(
                [
                    "git",
                    *git_safe_directory_config_args(worktree_path),
                    "-C",
                    str(worktree_path),
                    *args,
                ]
            )

        resolved_recovery_payload = recovery_payload or {}
        source_base_sha = _str_or_none(resolved_recovery_payload.get("source_base_sha"))
        source_head_sha = _str_or_none(resolved_recovery_payload.get("source_head_sha"))
        operation = await self._begin_rebase_recovery_operation(
            workspace_id=workspace_id,
            base_branch=base_branch,
            remote_branch=remote_branch,
            reason=reason,
            reason_code=_str_or_none(resolved_recovery_payload.get("reason_code"))
            or "MONITOR_REBASE_RECOVERY",
            source_base_sha=source_base_sha,
            source_head_sha=source_head_sha,
            recovery_payload=resolved_recovery_payload,
        )
        try:
            fetch = await git(["fetch", "origin", base_branch])
            if not fetch.ok:
                raise _MonitorRebaseRecoveryError(
                    f"rebase recovery: git fetch origin {base_branch} failed: {fetch.stderr}"
                )

            switch = await git(["switch", branch_name])
            if not switch.ok:
                raise _MonitorRebaseRecoveryError(
                    f"rebase recovery: git switch {branch_name} failed: {switch.stderr}"
                )

            target_ref = f"origin/{base_branch}"
            already_contains_target = await git(["merge-base", "--is-ancestor", target_ref, "HEAD"])
            if already_contains_target.ok:
                return await self._record_current_rebase_recovery_head(
                    git=git,
                    workspace_id=workspace_id,
                    target_ref=target_ref,
                    operation=operation,
                    source_base_sha=source_base_sha,
                    source_head_sha=source_head_sha,
                    rebased=False,
                    pushed=False,
                )
            if already_contains_target.returncode not in {1}:
                raise _MonitorRebaseRecoveryError(
                    "rebase recovery: git merge-base --is-ancestor "
                    f"{target_ref} HEAD failed: {already_contains_target.stderr}"
                )

            rebase = await git(["rebase", target_ref])
            if not rebase.ok:
                await git(["rebase", "--abort"])
                raise _MonitorRebaseRecoveryError(
                    f"rebase recovery: git rebase {target_ref} failed: {rebase.stderr}"
                )

            return await self._record_current_rebase_recovery_head(
                git=git,
                workspace_id=workspace_id,
                target_ref=target_ref,
                remote_branch=remote_branch,
                operation=operation,
                source_base_sha=source_base_sha,
                source_head_sha=source_head_sha,
                rebased=True,
                pushed=True,
            )
        except Exception as exc:
            await self._finish_rebase_recovery_operation(
                operation,
                status=OperationStatus.failed,
                result={
                    "status": "failed",
                    "reason_code": "MONITOR_RECOVERY_REBASE_FAILED",
                    "source_base_sha": source_base_sha,
                    "source_head_sha": source_head_sha,
                },
                error_code="MONITOR_RECOVERY_REBASE_FAILED",
                error_message=str(exc),
            )
            raise

    async def _record_current_rebase_recovery_head(
        self,
        *,
        git: Callable[[list[str]], Awaitable[CommandResult]],
        workspace_id: str,
        target_ref: str,
        remote_branch: str | None = None,
        operation: MonitorOperationHandle | None,
        source_base_sha: str | None,
        source_head_sha: str | None,
        rebased: bool,
        pushed: bool,
    ) -> _RebaseRecoveryResult:
        """Record the current branch head after rebase-style recovery.

        A monitor may dispatch rebase recovery after GitHub has already
        synced the PR branch with the target branch. In that case the
        branch already contains ``origin/<base>`` and running ``git rebase``
        again can fail while replaying commits from a merge-synced branch.
        Treating the already-synced state as a successful refresh keeps the
        recovery path idempotent; Tier 2 validation still proves the branch
        before merge eligibility is restored.
        """

        base_sha_result = await git(["rev-parse", target_ref])
        if not base_sha_result.ok or not base_sha_result.stdout.strip():
            raise _MonitorRebaseRecoveryError(
                f"rebase recovery: could not resolve {target_ref}: {base_sha_result.stderr}"
            )
        base_sha = base_sha_result.stdout.strip()

        head_sha_result = await git(["rev-parse", "HEAD"])
        if not head_sha_result.ok or not head_sha_result.stdout.strip():
            raise _MonitorRebaseRecoveryError(
                f"rebase recovery: could not resolve HEAD: {head_sha_result.stderr}"
            )
        head_sha = head_sha_result.stdout.strip()

        if remote_branch is not None:
            push = await git(["push", "--force-with-lease", "origin", f"HEAD:{remote_branch}"])
            if not push.ok:
                await self._record_executor_pr_audit_event(
                    workspace_id,
                    event_type=_AUDIT_GIT_PUSH_EVENT,
                    action="rebase_recovery_push",
                    outcome="failed",
                    reason_code="MONITOR_RECOVERY_REBASE_FAILED",
                    operation_id=operation.operation_id if operation is not None else None,
                    operation_type=OperationType.rebase.value,
                    source_head_sha=head_sha,
                    source_base_sha=base_sha,
                    remote_branch=remote_branch,
                    evidence={
                        "operation": "git push --force-with-lease",
                        "returncode": push.returncode,
                        "error_message": push.stderr.strip() or "<no output>",
                        "previous_source_base_sha": source_base_sha,
                        "previous_source_head_sha": source_head_sha,
                    },
                )
                raise _MonitorRebaseRecoveryError(
                    f"rebase recovery: git push --force-with-lease failed: {push.stderr}"
                )

        await self._record_rebase_recovery_success(
            workspace_id=workspace_id,
            base_sha=base_sha,
            head_sha=head_sha,
            remote_branch=remote_branch,
            source_base_sha=source_base_sha,
            source_head_sha=source_head_sha,
            operation=operation,
            pushed=pushed,
            rebased=rebased,
        )
        return _RebaseRecoveryResult(base_sha=base_sha, head_sha=head_sha)

    async def _record_rebase_recovery_success(
        self,
        *,
        workspace_id: str,
        base_sha: str,
        head_sha: str,
        remote_branch: str | None = None,
        source_base_sha: str | None,
        source_head_sha: str | None,
        operation: MonitorOperationHandle | None,
        pushed: bool,
        rebased: bool,
    ) -> None:
        async with self._session_factory() as session:
            workspace_repo = WorkspaceRepository(session)
            workspace = await workspace_repo.get(workspace_id)
            if workspace is None:  # pragma: no cover - destroyed mid-recovery
                return
            if _is_callback_terminal_status(workspace.status):
                await workspace_repo.record_ignored_stale_callback(
                    workspace,
                    callback_source="executor",
                    callback_action="rebase_recovery",
                    expected_status=WorkspaceStatus.running,
                    reason_code="STALE_CALLBACK_IGNORED",
                )
                await self._finish_ignored_stale_callback_operations_in_session(
                    session,
                    workspace_id=workspace_id,
                    callback_source="executor",
                    callback_action="rebase_recovery",
                    expected_status=WorkspaceStatus.running,
                    actual_status=workspace.status,
                )
                await session.commit()
                return
            workspace.base_commit = base_sha
            workspace.monitor_last_commit_sha = head_sha

            candidate = await MergeCandidateRepository(
                session
            ).get_open_for_workspace_with_merge_inputs(workspace_id)
            if candidate is not None:
                candidate.base_sha = base_sha
                candidate.head_sha = head_sha
                candidate.workspace.base_commit = base_sha
                candidate.workspace.monitor_last_commit_sha = head_sha
                sync_candidate_readiness(
                    candidate,
                    workspace=candidate.workspace,
                    attempt=candidate.attempt,
                    sync_validation_staleness=False,
                )

            if operation is not None and operation.should_finish:
                await finish_monitor_operation(
                    session,
                    operation_id=operation.operation_id,
                    status=OperationStatus.succeeded,
                    result={
                        "status": "succeeded",
                        "reason_code": "REBASE_OK",
                        "source_base_sha": source_base_sha,
                        "source_head_sha": source_head_sha,
                        "target_base_sha": base_sha,
                        "target_head_sha": head_sha,
                        "pushed": pushed,
                        "rebased": rebased,
                    },
                )
            if pushed:
                await self._add_executor_pr_audit_event(
                    workspace_repo,
                    workspace,
                    event_type=_AUDIT_GIT_PUSH_EVENT,
                    action="rebase_recovery_push",
                    outcome="succeeded",
                    reason_code="REBASE_OK",
                    operation_id=operation.operation_id if operation is not None else None,
                    operation_type=OperationType.rebase.value,
                    pr_number=getattr(workspace, "pr_number", None),
                    pr_url=getattr(workspace, "pr_url", None),
                    source_head_sha=head_sha,
                    source_base_sha=base_sha,
                    remote_branch=remote_branch or getattr(workspace, "remote_push_branch", None),
                    branch_name=getattr(workspace, "branch_name", None),
                    evidence={
                        "previous_source_base_sha": source_base_sha,
                        "previous_source_head_sha": source_head_sha,
                        "rebased": rebased,
                    },
                )
            await session.commit()

    async def _clear_rebase_recovery_staleness(
        self,
        *,
        workspace_id: str,
    ) -> None:
        async with self._session_factory() as session:
            candidate = await MergeCandidateRepository(
                session
            ).get_open_for_workspace_with_merge_inputs(workspace_id)
            if candidate is None:
                return
            await StaleReasonRepository(session).replace_active_findings(
                workspace_id=candidate.workspace_id,
                candidate_id=candidate.id,
                attempt_id=candidate.attempt_id,
                task_id=candidate.task_id,
                findings=[],
            )
            candidate.stale = False
            candidate.stale_reason = None
            sync_candidate_readiness(
                candidate,
                workspace=candidate.workspace,
                attempt=candidate.attempt,
                sync_validation_staleness=False,
            )
            await session.commit()

    async def _start_pending_recovery_operations(
        self,
        *,
        workspace_id: str,
    ) -> None:
        """Flush pending validate-only recovery operations to ``running``.

        Recovery dispatch creates the validate Operation in ``pending``;
        without an explicit transition the row would jump straight to
        ``succeeded``/``failed`` with
        ``started_at == finished_at``, which loses the recovery
        lifecycle for observability tooling.
        """
        async with self._session_factory() as session:
            repo = OperationRepository(session)
            pending = await repo.list_for_workspace(
                workspace_id,
                status=OperationStatus.pending,
                limit=100,
            )
            for operation in pending:
                if not _is_validate_only_recovery_payload(operation.payload):
                    continue
                await repo.start(operation)
            await session.commit()

    async def _finish_active_recovery_operations(
        self,
        *,
        workspace_id: str,
        status: OperationStatus,
        reason_code: str | None,
        error_message: str | None = None,
    ) -> None:
        async with self._session_factory() as session:
            await self._finish_active_recovery_operations_in_session(
                session,
                workspace_id=workspace_id,
                status=status,
                reason_code=reason_code,
                error_message=error_message,
            )
            await session.commit()

    async def _finish_active_recovery_operations_in_session(
        self,
        session: AsyncSession,
        *,
        workspace_id: str,
        status: OperationStatus,
        reason_code: str | None,
        error_message: str | None = None,
        result_extra: Mapping[str, Any] | None = None,
    ) -> None:
        repo = OperationRepository(session)
        pending = await repo.list_for_workspace(
            workspace_id,
            status=OperationStatus.pending,
            limit=100,
        )
        running = await repo.list_for_workspace(
            workspace_id,
            status=OperationStatus.running,
            limit=100,
        )
        result: dict[str, Any] = {"reason_code": reason_code}
        if result_extra is not None:
            result.update(result_extra)
        safe_error_message = redact_audit_text(error_message) if error_message is not None else None
        for operation in [*pending, *running]:
            if not _is_validate_only_recovery_payload(operation.payload):
                continue
            await repo.finish(
                operation,
                status=status,
                result=result,
                error_code=reason_code if status == OperationStatus.failed else None,
                error_message=safe_error_message,
            )

    async def _finish_ignored_stale_callback_operations_in_session(
        self,
        session: AsyncSession,
        *,
        workspace_id: str,
        callback_source: str,
        callback_action: str,
        expected_status: WorkspaceStatus,
        actual_status: str,
        validation_run_id: str | None = None,
        requested_tier: int | None = None,
    ) -> None:
        result: dict[str, Any] = {
            "status": "ignored",
            "reason_code": "STALE_CALLBACK_IGNORED",
            "callback_source": callback_source,
            "callback_action": callback_action,
            "expected_status": expected_status.value,
            "actual_status": actual_status,
        }
        if validation_run_id is not None:
            result["validation_run_id"] = validation_run_id
            validation_run = await ValidationRunRepository(session).get(validation_run_id)
            if validation_run is not None and isinstance(validation_run.log_stream_refs, dict):
                result["log_stream_refs"] = dict(validation_run.log_stream_refs)
        if requested_tier is not None:
            result["requested_tier"] = requested_tier
        await self._finish_active_recovery_operations_in_session(
            session,
            workspace_id=workspace_id,
            status=OperationStatus.cancelled,
            reason_code="STALE_CALLBACK_IGNORED",
            result_extra=result,
        )

    async def _finish_pending_validate_operations(
        self,
        *,
        workspace_id: str,
        status: OperationStatus,
        validation_run_id: str,
        requested_tier: int,
        reason_code: str | None,
        error_message: str | None = None,
        coverage: dict[str, object] | None = None,
    ) -> None:
        async with self._session_factory() as session:
            await self._finish_pending_validate_operations_in_session(
                session,
                workspace_id=workspace_id,
                status=status,
                validation_run_id=validation_run_id,
                requested_tier=requested_tier,
                reason_code=reason_code,
                error_message=error_message,
                coverage=coverage,
            )
            await session.commit()

    async def _finish_pending_validate_operations_in_session(
        self,
        session: AsyncSession,
        *,
        workspace_id: str,
        status: OperationStatus,
        validation_run_id: str,
        requested_tier: int,
        reason_code: str | None,
        error_message: str | None = None,
        coverage: dict[str, object] | None = None,
    ) -> None:
        repo = OperationRepository(session)
        pending = await repo.list_for_workspace(
            workspace_id,
            operation_type=OperationType.validate,
            status=OperationStatus.pending,
            limit=100,
        )
        running = await repo.list_for_workspace(
            workspace_id,
            operation_type=OperationType.validate,
            status=OperationStatus.running,
            limit=100,
        )
        result = {
            "validation_run_id": validation_run_id,
            "requested_tier": requested_tier,
            "reason_code": reason_code,
        }
        validation_run = await ValidationRunRepository(session).get(validation_run_id)
        if validation_run is not None and isinstance(validation_run.log_stream_refs, dict):
            result["log_stream_refs"] = dict(validation_run.log_stream_refs)
        if coverage is not None:
            result["coverage"] = coverage
        safe_error_message = redact_audit_text(error_message) if error_message is not None else None
        for operation in [*pending, *running]:
            payload = dict(operation.payload or {})
            payload.setdefault("requested_tier", requested_tier)
            operation.payload = payload
            await repo.finish(
                operation,
                status=status,
                result=result,
                error_code=reason_code if status == OperationStatus.failed else None,
                error_message=safe_error_message,
            )

    async def _finish_validation_run(
        self,
        validation_run_id: str,
        *,
        status: str,
        reason_code: str | None,
        retry_count: int = 0,
        coverage: dict[str, object] | None = None,
        command_retries: list[int] | None = None,
        coverage_evidence_status: str | None = None,
        coverage_evidence_reason_code: str | None = None,
        coverage_evidence_source_run_id: str | None = None,
    ) -> None:
        async with self._session_factory() as session:
            await ValidationRunRepository(session).finish(
                validation_run_id,
                status=status,
                reason_code=reason_code,
                finished_at=datetime.now(UTC),
                retry_count=retry_count,
                coverage=coverage,
                command_retries=command_retries,
                coverage_evidence_status=coverage_evidence_status,
                coverage_evidence_reason_code=coverage_evidence_reason_code,
                coverage_evidence_source_run_id=coverage_evidence_source_run_id,
            )
            await session.commit()

    async def _finish_validation_callback_if_terminal(
        self,
        *,
        workspace_id: str,
        validation_run_id: str,
        requested_tier: int,
    ) -> bool:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None:  # pragma: no cover - row disappeared mid-validation
                return True
            if ws.status == WorkspaceStatus.validating.value:
                return False
            if not _is_callback_terminal_status(ws.status):
                return False
            await self._record_stale_action_skip(
                repo,
                ws,
                action="validate",
                expected=WorkspaceStatus.validating,
                reason_code="STALE_CALLBACK_IGNORED",
            )
            await ValidationRunRepository(session).finish(
                validation_run_id,
                status="failed",
                reason_code="STALE_CALLBACK_IGNORED",
                finished_at=datetime.now(UTC),
            )
            await self._finish_ignored_stale_callback_operations_in_session(
                session,
                workspace_id=workspace_id,
                callback_source="executor",
                callback_action="validate",
                expected_status=WorkspaceStatus.validating,
                actual_status=ws.status,
                validation_run_id=validation_run_id,
                requested_tier=requested_tier,
            )
            await session.commit()
            return True

    async def _set_validation_run_target_head_sha(
        self,
        *,
        validation_run_id: str,
        target_head_sha: str,
        workspace_head_sha: str | None = None,
    ) -> None:
        async with self._session_factory() as session:
            await ValidationRunRepository(session).update_target_head_sha(
                validation_run_id,
                target_head_sha=target_head_sha,
                workspace_head_sha=workspace_head_sha,
            )
            await session.commit()


def _validation_evidence_json(payload: dict[str, Any]) -> str:
    safe_payload = cast(dict[str, Any], redact_audit_value(payload))
    serialized = _serialize_validation_evidence_payload(safe_payload)
    if len(serialized) <= _VALIDATION_EVIDENCE_JSON_LIMIT:
        return serialized

    compact_payload = dict(safe_payload)
    compact_payload["evidence_truncated"] = True
    compact_payload["commands"] = _validation_evidence_size_summary(safe_payload.get("commands"))
    compact_payload["log_stream_refs"] = _validation_evidence_size_summary(
        safe_payload.get("log_stream_refs")
    )
    serialized = _serialize_validation_evidence_payload(compact_payload)
    if len(serialized) <= _VALIDATION_EVIDENCE_JSON_LIMIT:
        return serialized

    compact_payload["coverage"] = _validation_evidence_coverage_summary(
        safe_payload.get("coverage")
    )
    serialized = _serialize_validation_evidence_payload(compact_payload)
    if len(serialized) <= _VALIDATION_EVIDENCE_JSON_LIMIT:
        return serialized

    minimal_payload = {
        key: compact_payload.get(key)
        for key in _VALIDATION_EVIDENCE_CORE_KEYS
        if key in compact_payload
    }
    minimal_payload["evidence_truncated"] = True
    minimal_payload["commands"] = _validation_evidence_size_summary(safe_payload.get("commands"))
    minimal_payload["log_stream_refs"] = _validation_evidence_size_summary(
        safe_payload.get("log_stream_refs")
    )
    serialized = _serialize_validation_evidence_payload(minimal_payload)
    if len(serialized) <= _VALIDATION_EVIDENCE_JSON_LIMIT:
        return serialized

    oversized_serialized_length = len(json.dumps(safe_payload, default=str))
    floor_payload = _validation_evidence_floor_payload(
        safe_payload,
        oversized_serialized_length=oversized_serialized_length,
    )
    serialized = _serialize_validation_evidence_payload(floor_payload)
    if len(serialized) <= _VALIDATION_EVIDENCE_JSON_LIMIT:
        return serialized

    return _serialize_validation_evidence_payload(
        {
            "evidence_truncated": True,
            "truncation_reason": "validation_evidence_json_limit",
            "oversized_serialized_length": oversized_serialized_length,
        }
    )


def _serialize_validation_evidence_payload(payload: Mapping[str, Any]) -> str:
    serialized = json.dumps(payload, default=str)
    return redact_audit_text(serialized, limit=_VALIDATION_EVIDENCE_JSON_LIMIT)


def _validation_evidence_coverage_summary(value: object) -> object:
    if not isinstance(value, Mapping):
        return _validation_evidence_size_summary(value)
    summary = _validation_evidence_size_summary(value)
    for key in _VALIDATION_EVIDENCE_COVERAGE_PRIORITY_KEYS:
        if key in value:
            summary[key] = value[key]
    return summary


def _validation_evidence_floor_payload(
    payload: Mapping[str, Any],
    *,
    oversized_serialized_length: int,
) -> dict[str, Any]:
    floor_payload = {
        key: _validation_evidence_floor_value(payload[key])
        for key in _VALIDATION_EVIDENCE_CORE_KEYS
        if key in payload and key != "coverage"
    }
    if "coverage" in payload:
        floor_payload["coverage"] = _validation_evidence_size_summary(payload["coverage"])
    floor_payload["evidence_truncated"] = True
    floor_payload["truncation_reason"] = "validation_evidence_json_limit"
    floor_payload["oversized_serialized_length"] = oversized_serialized_length
    floor_payload["commands"] = _validation_evidence_size_summary(payload.get("commands"))
    floor_payload["log_stream_refs"] = _validation_evidence_size_summary(
        payload.get("log_stream_refs")
    )
    return floor_payload


def _validation_evidence_floor_value(value: object) -> object:
    if isinstance(value, str):
        if len(value) <= 512:
            return value
        return _validation_evidence_size_summary(value)
    if isinstance(value, int | float | bool) or value is None:
        return value
    return _validation_evidence_size_summary(value)


def _validation_evidence_size_summary(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        retained_keys = [str(key) for key in list(value)[:_VALIDATION_EVIDENCE_RETAINED_KEY_COUNT]]
        return {
            "truncated": True,
            "original_type": "mapping",
            "original_entry_count": len(value),
            "retained_keys": retained_keys,
        }
    if isinstance(value, list):
        return {
            "truncated": True,
            "original_type": "list",
            "original_length": len(value),
        }
    if isinstance(value, str):
        return {
            "truncated": True,
            "original_type": "string",
            "original_length": len(value),
        }
    return {
        "truncated": True,
        "original_type": type(value).__name__,
    }


_PR_NUMBER_RE = re.compile(r"/pull/(\d+)(?:/|$)")


def _extract_pr_number(pr_url: str) -> int | None:
    """Parse the PR number from a GitHub PR URL.

    Matches both the canonical ``https://github.com/<owner>/<repo>/pull/123``
    and the trailing-slash variant. Returns ``None`` if the URL doesn't look
    like a PR URL — the monitor then simply won't run (executor logs a
    warning via the transition to ``monitoring_pr`` still succeeding; the
    monitor itself asserts on pr_number and terminates with a clear failure).
    """
    match = _PR_NUMBER_RE.search(pr_url)
    return int(match.group(1)) if match else None


def _worktree_missing_message(worktree_path: Path, action: str) -> str:
    return (
        f"{WORKTREE_MISSING_REASON_CODE}: managed worktree path is missing or not a "
        f"directory while preparing `{action}`: {worktree_path}"
    )


def _missing_monitor_recovery_metadata(ws: Workspace) -> list[str]:
    missing: list[str] = []
    if ws.pr_number is None:
        missing.append("pr_number")
    if not ws.pr_url:
        missing.append("pr_url")
    if not ws.remote_push_branch:
        missing.append(
            f"remote_push_branch (task_kind={ws.task_kind}, branch_name={ws.branch_name!r})"
        )
    if not ws.compose_project_name:
        missing.append("compose_project_name")
    if not ws.compose_file_path:
        missing.append("compose_file_path")
    return missing


def _sync_feature_pr_adoption_metadata(ws: Workspace) -> Mapping[str, object]:
    policy = ws.task_policy if isinstance(ws.task_policy, Mapping) else {}
    adoption = policy.get("pr_adoption")
    return adoption if isinstance(adoption, Mapping) else {}


def _existing_pr_remote_push_url(ws: Workspace) -> str | None:
    if ws.task_kind != "sync_feature_pr":
        return None
    try:
        base_repo = RepoRef.from_url(ws.repo_url)
    except ValueError:
        return None
    return remote_push_url_for_workspace(ws, base_repo=base_repo)


def _missing_sync_feature_pr_adoption_metadata(
    ws: Workspace,
    metadata: Mapping[str, object],
) -> list[str]:
    missing: list[str] = []
    if ws.pr_number is None and _metadata_int(metadata, "pr_number") is None:
        missing.append("pr_number")
    if not ws.pr_url and _nonblank_metadata_str(metadata, "pr_url") is None:
        missing.append("pr_url")
    if not ws.remote_push_branch and _nonblank_metadata_str(metadata, "head_ref") is None:
        missing.append("remote_push_branch")
    for key in ("head_ref", "base_ref", "head_sha", "base_sha"):
        if _nonblank_metadata_str(metadata, key) is None:
            missing.append(f"task_policy.pr_adoption.{key}")
    return missing


def _sync_feature_pr_missing_metadata_message(missing: Sequence[str]) -> str:
    return "adopted PR workspace is missing required monitor handoff metadata: " + ", ".join(
        missing
    )


def _redacted_exception_traceback(exc: BaseException) -> str:
    formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return redact_audit_text(formatted, limit=_EXCEPTION_TRACEBACK_LIMIT)


def _required_metadata_str(metadata: Mapping[str, object], key: str) -> str:
    value = _nonblank_metadata_str(metadata, key)
    if value is None:
        raise ValueError(f"missing adoption metadata key: {key}")
    return value


def _nonblank_metadata_str(metadata: Mapping[str, object], key: str) -> str | None:
    value = _metadata_str(metadata, key)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _call_pr_monitor_factory(
    factory: Callable[..., _MonitorRunnerProto],
    *,
    adapter: AgentAdapter,
    profile: WorkspaceProfile,
    workspace: Workspace,
    provider_recovery_default_model: str | None = None,
) -> _MonitorRunnerProto:
    """Call a monitor factory with the richest supported context.

    Production factories may need the persisted workspace row for monitor
    policy. Older tests and scripts predate profile/workspace-aware execution
    and expose one- or two-argument factories. We inspect arity before the call
    so a ``TypeError`` raised inside the factory body is never mistaken for an
    argument-count mismatch.
    """
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        # Without a signature, preserve the historical two-argument fallback;
        # probing by calling would risk masking TypeErrors raised inside the
        # factory body.
        return factory(adapter, profile)

    bind_errors: list[TypeError] = []
    provider_recovery_kwargs = {"provider_recovery_default_model": provider_recovery_default_model}
    for args, kwargs in (
        ((adapter, profile, workspace), provider_recovery_kwargs),
        ((adapter, profile, workspace), {}),
        ((adapter, profile), {}),
        ((adapter,), {}),
    ):
        try:
            signature.bind(*args, **kwargs)
        except TypeError as exc:
            bind_errors.append(exc)
            continue
        return factory(*args, **kwargs)

    raise bind_errors[0]


def _build_pr_body(ws: Workspace, *, defaults: AgentDefaults | None = None) -> str:
    """Standard PR description generated from the workspace's task metadata."""
    external_id = f"\n**External task ID**: {ws.task_external_id}" if ws.task_external_id else ""
    return (
        f"Automatically opened by AWF workspace `{ws.id}` "
        f"({_agent_pr_identity(ws, defaults=defaults)}).\n"
        f"{external_id}\n\n"
        f"### Task\n{ws.task_prompt}\n\n"
        f"---\nValidation: "
        f"{_validation_command_count(ws)} profile command(s) passed inside the workspace container.\n"
    )


def _agent_pr_identity(ws: Workspace, *, defaults: AgentDefaults | None = None) -> str:
    policy = ws.task_policy if isinstance(ws.task_policy, dict) else {}
    model = _nonblank_policy_string(policy, "agent_model") or (defaults.model if defaults else None)
    effort = _nonblank_policy_string(policy, "agent_effort") or (
        defaults.effort if defaults else None
    )

    parts = [f"agent: `{ws.agent}`"]
    if model is not None:
        parts.append(f"model: `{model}`")
    if effort is not None:
        parts.append(f"effort: `{effort}`")
    return ", ".join(parts)


def _nonblank_policy_string(policy: Mapping[str, Any], key: str) -> str | None:
    value = policy.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _profile_for_workspace(
    ws: Workspace,
    *,
    worktree_path: Path,
    planning_max_iterations_default: int = 3,
) -> WorkspaceProfile:
    if ws.resolved_profile:
        profile = WorkspaceProfile.model_validate(ws.resolved_profile)
        return _profile_with_planning_iteration_default(
            profile,
            planning_max_iterations_default,
            raw_profile=ws.resolved_profile,
        )
    profile = resolve_workspace_profile(
        worktree_path=worktree_path,
        inline_profile=ws.requested_profile,
        profile_ref=ws.profile_ref or ws.env_profile or "auto",
        validation_commands=list(ws.test_commands),
    ).profile
    return _profile_with_planning_iteration_default(
        profile,
        planning_max_iterations_default,
        raw_profile=ws.requested_profile,
    )


def _profile_with_planning_iteration_default(
    profile: WorkspaceProfile,
    planning_max_iterations_default: int,
    *,
    raw_profile: Mapping[str, Any] | None = None,
) -> WorkspaceProfile:
    """Apply the settings default only when the profile omitted max_iterations."""

    explicit = (
        _raw_profile_has_explicit_planning_max_iterations(raw_profile)
        if raw_profile is not None
        else "max_iterations" in profile.planning.model_fields_set
    )
    if explicit or profile.planning.max_iterations == planning_max_iterations_default:
        return profile
    return profile.model_copy(
        deep=True,
        update={
            "planning": profile.planning.model_copy(
                update={"max_iterations": planning_max_iterations_default}
            )
        },
    )


def _raw_profile_has_explicit_planning_max_iterations(
    raw_profile: Mapping[str, Any] | None,
) -> bool:
    if raw_profile is None:
        return False
    planning = raw_profile.get("planning")
    return isinstance(planning, Mapping) and "max_iterations" in planning


def _failure_salvage_payload(
    workspace: Workspace,
    *,
    worktree_path: Path,
) -> dict[str, str]:
    branch_name = workspace.branch_name
    remote_push_branch = workspace.remote_push_branch or branch_name
    payload = {
        "hint": "Workspace worktree and branch were preserved for salvage.",
        "worktree_path": str(worktree_path),
    }
    if branch_name:
        payload["branch_name"] = branch_name
    if remote_push_branch:
        payload["remote_push_branch"] = remote_push_branch
    return payload


def _agent_model_for_workspace(
    ws: Workspace,
    defaults: AgentDefaults | None,
) -> str | None:
    workspace_defaults = _agent_defaults_for_workspace(ws, defaults)
    return workspace_defaults.model if workspace_defaults is not None else None


def _agent_defaults_for_workspace(
    ws: Workspace,
    defaults: AgentDefaults | None,
) -> AgentDefaults | None:
    """Return adapter defaults after applying workspace-persisted policy.

    Agent adapters are handed to the PR monitor, which invokes recovery
    prompts without passing an explicit ``model`` each time. Binding the
    workspace's effective model into the adapter is therefore important:
    an opencode workspace launched with ``ollama/glm-5.1:cloud`` must not
    drift back to AWF's opencode default (currently Kimi) while resolving
    PR comments.
    """
    policy = ws.task_policy if isinstance(ws.task_policy, dict) else {}
    model = _nonblank_policy_string(policy, "agent_model")
    effort = _nonblank_policy_string(policy, "agent_effort")
    if model is None and effort is None:
        return defaults
    if defaults is not None:
        return replace(
            defaults,
            model=model or defaults.model,
            effort=effort or defaults.effort,
        )
    if model is None:
        return None
    return AgentDefaults(model=model, effort=effort)


def _failure_reason_for_phase(first_fail: object | None) -> FailureReason:
    phase = getattr(first_fail, "phase", None)
    reason_code = getattr(first_fail, "reason_code", None)
    if phase == "healthcheck":
        return FailureReason.health_check_failure
    if reason_code in {
        "PHASE_TIMEOUT",
        DATABASE_GENERATED_SETUP_TIMEOUT,
        DATABASE_REFRESH_TIMEOUT,
    }:
        return FailureReason.phase_timeout
    if reason_code == PROFILE_VALIDATION_TOOL_UNAVAILABLE:
        return FailureReason.profile_resolution_failure
    if phase in {"setup", "pre_agent", DB_GENERATED_SETUP_PHASE}:
        return FailureReason.service_startup_failure
    return FailureReason.validation_failure


def _validation_command_count(ws: Workspace) -> int:
    if ws.resolved_profile:
        profile = WorkspaceProfile.model_validate(ws.resolved_profile)
        coverage_count = 1 if _should_run_local_coverage(profile) else 0
        return (
            len(profile.phases.post_agent)
            + len(profile.database.pre_validation_refresh)
            + len(profile.phases.validate_commands)
            + coverage_count
        )
    return len(ws.test_commands)


def _read_text_if_present(path: Path) -> str | None:
    try:
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            return text or None
    except OSError:
        return None
    return None


def _digest_file_if_present(path: Path) -> str | None:
    try:
        if path.is_file():
            hasher = hashlib.sha256()
            with path.open("rb") as fh:
                while chunk := fh.read(_FILE_DIGEST_CHUNK_SIZE):
                    hasher.update(chunk)
            return hasher.hexdigest()
    except OSError:
        return None
    return None


def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validation_run_command_records(
    *,
    profile: WorkspaceProfile,
    phase_names: tuple[str, ...],
    run_healthchecks: bool,
    coverage_evidence_status: str | None = None,
    coverage_evidence_reason_code: str | None = None,
) -> list[dict[str, Any]]:
    ordered: list[dict[str, Any]] = []
    if "validate" in phase_names and profile.validation.alembic.enabled:
        ordered.append(
            {
                "phase": ALEMBIC_MIGRATION_POLICY_PHASE,
                "command": ALEMBIC_MIGRATION_POLICY_COMMAND,
            }
        )
    command_plan = profile_phase_command_plan(profile, phase_names)
    healthcheck_before_phase = (
        "validate"
        if profile.database.pre_validation_refresh and "validate" in set(phase_names)
        else None
    )
    pending_healthchecks = list(profile.validation.healthchecks) if run_healthchecks else []
    if healthcheck_before_phase is None:
        ordered.extend(_healthcheck_command_records(pending_healthchecks))
        pending_healthchecks = []
    for step in command_plan:
        if (
            healthcheck_before_phase is not None
            and pending_healthchecks
            and step.phase == healthcheck_before_phase
        ):
            ordered.extend(_healthcheck_command_records(pending_healthchecks))
            pending_healthchecks = []
        record: dict[str, Any] = {"phase": step.phase, "command": step.command.command}
        if step.database_hook:
            record.update(
                {
                    "database_hook": True,
                    "hook_kind": step.hook_kind,
                    "timeout_seconds": step.command.timeout_seconds,
                }
            )
        ordered.append(record)
    if pending_healthchecks:
        ordered.extend(_healthcheck_command_records(pending_healthchecks))
    if "validate" in phase_names and _should_run_local_coverage(profile):
        coverage_command = profile.validation.coverage.command
        if coverage_command is None:
            raise RuntimeError(
                "_should_run_local_coverage returned True but coverage.command is None"
            )
        coverage_record = {
            "phase": "coverage",
            "command": coverage_command.command,
        }
        if coverage_evidence_status is not None:
            coverage_record["evidence_status"] = coverage_evidence_status
        if coverage_evidence_reason_code is not None:
            coverage_record["evidence_reason_code"] = coverage_evidence_reason_code
        ordered.append(coverage_record)

    records: list[dict[str, Any]] = []
    phase_indices: dict[str, int] = {}
    for item in ordered:
        phase = str(item["phase"])
        phase_indices[phase] = phase_indices.get(phase, 0) + 1
        command_index = phase_indices[phase]
        label = f"{command_index:02d}_{phase}"
        record = dict(item)
        record.update(
            {
                "command_index": command_index,
                "stream_ids": {
                    "stdout": f"validation.{label}.stdout",
                    "stderr": f"validation.{label}.stderr",
                },
            }
        )
        records.append(record)
    return records


def _healthcheck_command_records(healthchecks: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "phase": "healthcheck",
            "command": healthcheck.display_command(),
            "healthcheck_name": healthcheck.name,
            "healthcheck_kind": healthcheck.kind or ("http" if healthcheck.url else "command"),
            "target": healthcheck.target(),
        }
        for healthcheck in healthchecks
    ]


def _validation_tier_for_workspace(workspace: Workspace, profile: WorkspaceProfile) -> int:
    profile_tier = profile.validation.requested_tier
    task_class_tier = 1
    if workspace.task_class == TaskClass.migration_task.value:
        task_class_tier = 3
    elif workspace.task_class in {
        TaskClass.refactor_task.value,
        TaskClass.dependency_task.value,
        TaskClass.build_config_task.value,
    }:
        task_class_tier = 2
    operation_tier = _validate_operation_requested_tier(workspace) or 1
    return max(profile_tier, task_class_tier, operation_tier)


def _validate_operation_requested_tier(workspace: Workspace) -> int | None:
    tiers: list[int] = []
    operations = getattr(workspace, "operations", None) or []
    for operation in operations:
        operation_type = getattr(operation, "type", None)
        if isinstance(operation_type, OperationType):
            operation_type = operation_type.value
        if operation_type != OperationType.validate.value:
            continue

        operation_status = getattr(operation, "status", None)
        if isinstance(operation_status, OperationStatus):
            operation_status = operation_status.value
        metadata: tuple[object, ...]
        if operation_status == OperationStatus.succeeded.value:
            metadata = (
                getattr(operation, "result", None),
                getattr(operation, "payload", None),
            )
        elif operation_status in _RECOVERY_ACTIVE_OPERATION_STATUSES:
            metadata = (getattr(operation, "payload", None),)
        else:
            continue

        operation_tiers = [
            tier
            for tier in (_requested_tier_from_metadata(item) for item in metadata)
            if tier is not None
        ]
        operation_max = max(operation_tiers, default=None)
        if operation_max is not None:
            tiers.append(operation_max)
    return max(tiers, default=None)


def _requested_tier_from_metadata(metadata: object) -> int | None:
    if not isinstance(metadata, Mapping):
        return None
    requested_tier = metadata.get("requested_tier")
    if type(requested_tier) is int and requested_tier > 0:
        return requested_tier
    validation = metadata.get("validation")
    if not isinstance(validation, Mapping):
        return None
    requested_tier = validation.get("requested_tier")
    if type(requested_tier) is int and requested_tier > 0:
        return requested_tier
    return None


def _should_run_local_coverage(profile: WorkspaceProfile) -> bool:
    return (
        profile.validation.strategy.final_gate == "coverage"
        and profile.validation.coverage.command is not None
    )


def _validation_run_log_stream_refs(
    command_records: list[dict[str, Any]],
) -> dict[str, list[dict[str, str | None]]]:
    refs: list[dict[str, str | None]] = []
    for command in command_records:
        stream_ids = command.get("stream_ids")
        if not isinstance(stream_ids, dict):
            refs.append({"stdout": None, "stderr": None})
            continue
        stdout = stream_ids.get("stdout")
        stderr = stream_ids.get("stderr")
        refs.append(
            {
                "stdout": stdout if isinstance(stdout, str) else None,
                "stderr": stderr if isinstance(stderr, str) else None,
            }
        )
    return {"commands": refs}


def _validation_run_reason_code(result: ValidationResult) -> str:
    if result.all_passed:
        return "VALIDATION_OK"
    first_failure = result.first_failure
    if _coverage_has_failing_tests(result.coverage):
        return PYTEST_TEST_FAILURE
    if result.coverage is not None and not result.coverage.ok:
        return result.coverage.reason_code
    if first_failure is None:
        return "VALIDATION_FAILED"
    return first_failure.reason_code


def _apply_baseline_coverage_ratchet(
    result: ValidationResult,
    *,
    baseline_coverage: ValidationCoverageResult | None,
) -> ValidationResult:
    """Accept coverage baseline debt only when a workspace does not regress it.

    AWF's self profile carries an aspirational 99% coverage target. Until the
    existing repo baseline reaches that target, unrelated feature PRs should
    not be forced to repay the whole historical debt. They must, however,
    preserve or improve the measured baseline and must not lower the gate.
    """
    coverage = result.coverage
    if not _coverage_preserves_below_threshold_baseline(
        coverage,
        baseline_coverage=baseline_coverage,
    ):
        return result

    assert coverage is not None  # narrowed by helper above
    command_result = coverage.command_result
    adjusted_command = (
        replace(
            command_result,
            returncode=0,
            reason_code="COVERAGE_BASELINE_DEBT_NO_REGRESSION",
            policy_failed=False,
        )
        if command_result is not None
        else None
    )
    adjusted_coverage = replace(
        coverage,
        status="baseline_debt",
        reason_code="COVERAGE_BASELINE_DEBT_NO_REGRESSION",
        command_result=adjusted_command,
    )
    adjusted_commands = list(result.commands)
    if adjusted_command is not None and command_result is not None:
        adjusted_commands = [
            adjusted_command
            if command.stdout_path == command_result.stdout_path
            and command.stderr_path == command_result.stderr_path
            else command
            for command in result.commands
        ]
    return replace(result, commands=adjusted_commands, coverage=adjusted_coverage)


def _coverage_preserves_below_threshold_baseline(
    coverage: ValidationCoverageResult | None,
    *,
    baseline_coverage: ValidationCoverageResult | None,
) -> bool:
    if coverage is None or baseline_coverage is None:
        return False
    if _coverage_has_failing_tests(coverage):
        return False
    if coverage.reason_code != "COVERAGE_BELOW_THRESHOLD":
        return False
    if coverage.percent is None or baseline_coverage.percent is None:
        return False
    if baseline_coverage.percent >= coverage.minimum_percent:
        return False
    return coverage.percent + 0.005 >= baseline_coverage.percent


def _validation_run_coverage_metadata(
    result: ValidationResult,
    *,
    baseline_coverage: ValidationCoverageResult | None = None,
) -> dict[str, object] | None:
    if result.coverage is None:
        return None
    metadata = result.coverage.as_metadata()
    if baseline_coverage is not None:
        metadata["baseline_percent"] = (
            float(baseline_coverage.percent) if baseline_coverage.percent is not None else None
        )
        metadata["baseline_status"] = baseline_coverage.status
        metadata["baseline_reason_code"] = baseline_coverage.reason_code
    return metadata


def _extract_string_tokens(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def _coverage_result_from_metadata(metadata: Mapping[str, object]) -> ValidationCoverageResult:
    percent = metadata.get("percent")
    minimum = metadata.get("minimum_percent")
    enforce = metadata.get("enforce")
    gaps = metadata.get("gaps")
    raw_failing_node_ids = metadata.get("failing_test_node_ids")
    raw_failing_evidence = metadata.get("failing_test_evidence")
    raw_provider_failure_evidence = metadata.get("provider_failure_evidence")
    parallel_requested = metadata.get("parallel_workers_requested")
    parallel_effective = metadata.get("parallel_workers_effective")
    parallel_distribution = metadata.get("parallel_distribution")
    return ValidationCoverageResult(
        provider=str(metadata.get("provider") or "python"),
        percent=float(percent) if isinstance(percent, int | float) else None,
        minimum_percent=float(minimum) if isinstance(minimum, int | float) else 0.0,
        enforce=bool(enforce) if isinstance(enforce, bool) else True,
        status=str(metadata.get("status") or "passed"),
        reason_code=str(metadata.get("reason_code") or "COVERAGE_OK"),
        gaps=[item for item in gaps if isinstance(item, dict)] if isinstance(gaps, list) else [],
        failing_test_node_ids=_extract_string_tokens(raw_failing_node_ids),
        failing_test_evidence=_extract_string_tokens(raw_failing_evidence),
        provider_failure_evidence=_extract_string_tokens(raw_provider_failure_evidence),
        parallel_workers_requested=(
            int(parallel_requested) if isinstance(parallel_requested, int) else None
        ),
        parallel_workers_effective=(
            int(parallel_effective) if isinstance(parallel_effective, int) else None
        ),
        parallel_distribution=(
            str(parallel_distribution) if isinstance(parallel_distribution, str) else None
        ),
    )


def _format_coverage_gaps(gaps: list[dict[str, object]]) -> str:
    if not gaps:
        return ""
    top = gaps[:5]
    lines = ["top uncovered areas:"]
    for g in top:
        file_name = g.get("file", "")
        missing = g.get("missing_lines", [])
        missing_cast = missing if isinstance(missing, list) else []
        missing_str = (
            ", ".join(str(m) for m in missing_cast) if missing_cast else "(no missing lines)"
        )
        lines.append(f"  {file_name}: {missing_str}")
    return "\n".join(lines)


def _coverage_has_failing_tests(coverage: ValidationCoverageResult | None) -> bool:
    if coverage is None:
        return False
    return bool(coverage.failing_test_node_ids or coverage.failing_test_evidence)


def _format_failing_test_evidence(coverage: ValidationCoverageResult) -> str:
    node_ids = [str(value) for value in coverage.failing_test_node_ids[:5]]
    evidence = [str(value) for value in coverage.failing_test_evidence[:5]]
    if node_ids and evidence:
        return f"{', '.join(node_ids)}; evidence: {' | '.join(evidence)}"
    return ", ".join(node_ids if node_ids else evidence)


def _coverage_wrapped_pytest_failure_message(
    coverage: ValidationCoverageResult,
) -> str:
    tests = _format_failing_test_evidence(coverage)
    tests_fragment = f": {tests}" if tests else ""
    if coverage.reason_code == "COVERAGE_BELOW_THRESHOLD" and coverage.percent is not None:
        gap_lines = _format_coverage_gaps(coverage.gaps if coverage.gaps else [])
        gap_text = f"\n{gap_lines}" if gap_lines else ""
        return (
            f"validation failed: pytest reported failing tests{tests_fragment}; "
            f"coverage {coverage.percent:.1f}% is also below required "
            f"{coverage.minimum_percent:.1f}%; fix the failing test first, "
            "then address coverage if it remains below threshold"
            f"{gap_text}"
        )
    if coverage.reason_code == "COVERAGE_FAIL_UNDER_NOT_REACHED":
        displayed = (
            f"; displayed rounded coverage was {coverage.percent:.2f}%"
            if coverage.percent is not None
            else ""
        )
        return (
            f"validation failed: pytest reported failing tests{tests_fragment}; "
            "coverage provider also reported that fail-under was not reached"
            f"{displayed}; required coverage is {coverage.minimum_percent:.2f}%"
        )
    if coverage.percent is not None:
        return (
            f"validation failed: pytest reported failing tests{tests_fragment}; "
            f"coverage met the {coverage.minimum_percent:.1f}% requirement "
            f"at {coverage.percent:.1f}%"
        )
    return (
        f"validation failed: pytest reported failing tests{tests_fragment}; "
        "coverage output was not available because the coverage-wrapped test command failed"
    )


def _validation_failure_message(
    result: ValidationResult,
    *,
    baseline_coverage: ValidationCoverageResult | None = None,
) -> str:
    coverage = result.coverage
    first_fail = result.first_failure
    if coverage is not None and _coverage_has_failing_tests(coverage):
        return _coverage_wrapped_pytest_failure_message(coverage)
    if coverage is not None and not coverage.ok:
        baseline_debt = (
            baseline_coverage is not None
            and baseline_coverage.percent is not None
            and baseline_coverage.percent < coverage.minimum_percent
        )
        baseline_suffix = (
            f"; pre-agent base coverage was {baseline_coverage.percent:.1f}%"
            f" against the same {coverage.minimum_percent:.1f}% requirement"
            if baseline_debt
            and baseline_coverage is not None
            and baseline_coverage.percent is not None
            else ""
        )
        if coverage.reason_code == "COVERAGE_BELOW_THRESHOLD" and coverage.percent is not None:
            gap_lines = _format_coverage_gaps(coverage.gaps if coverage.gaps else [])
            gap_text = f"\n{gap_lines}" if gap_lines else ""
            return (
                "validation failed: coverage "
                f"{coverage.percent:.1f}% is below required {coverage.minimum_percent:.1f}%"
                f"{baseline_suffix}; add meaningful tests and do not lower coverage thresholds"
                f"{gap_text}"
            )
        if coverage.reason_code == "COVERAGE_NOT_FOUND":
            return "validation failed: coverage output was not found"
        if coverage.reason_code == "COVERAGE_COMMAND_FAILED":
            return (
                "validation failed: coverage command failed"
                f"{baseline_suffix}; fix the failing tests or add meaningful coverage, "
                "do not lower coverage thresholds"
            )
        if coverage.reason_code == "COVERAGE_FAIL_UNDER_NOT_REACHED":
            displayed = (
                f"; displayed rounded coverage was {coverage.percent:.2f}%"
                if coverage.percent is not None
                else ""
            )
            return (
                "validation failed: coverage provider reported that fail-under was not reached"
                f"{displayed}; required coverage is {coverage.minimum_percent:.2f}%"
                f"{baseline_suffix}; treat provider fail-under output as authoritative "
                "and add meaningful tests instead of relying on rounded coverage"
            )
        if coverage.reason_code == "COVERAGE_PROVIDER_UNSUPPORTED":
            return f"validation failed: unsupported coverage provider {coverage.provider}"

    if first_fail is not None and first_fail.phase == "healthcheck":
        metadata = first_fail.metadata
        name = metadata.get("healthcheck_name")
        kind = metadata.get("healthcheck_kind")
        target = metadata.get("target") or first_fail.command
        attempts = metadata.get("attempts")
        timeout = metadata.get("timeout_seconds")
        stream_ids = first_fail.stream_ids
        stdout_stream = stream_ids.get("stdout")
        stderr_stream = stream_ids.get("stderr")
        log_hint = ", ".join(
            stream for stream in (stdout_stream, stderr_stream) if isinstance(stream, str)
        )
        details = (
            f"validation failed: health check {name if isinstance(name, str) else first_fail.command}"
            f" ({kind if isinstance(kind, str) else 'unknown'} target={target}) "
            f"failed with {first_fail.reason_code}"
        )
        if isinstance(timeout, int | float):
            details += f" after {_format_duration_for_message(timeout)}s"
        if isinstance(attempts, int):
            details += f" across {attempts} attempt(s)"
        if log_hint:
            details += f"; logs: {log_hint}"
        return details
    return f"validation failed: {first_fail.command}" if first_fail else "validation failed"


def _post_validation_conformance_fix_result(
    *,
    failure: _PlanningRunFailure,
    workspace_id: str,
    artifacts_root: Path,
    attempt: int | None = None,
) -> ValidationResult:
    artifacts_dir = artifacts_root / workspace_id / "post_validation_conformance"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    attempt_value: object = attempt
    if attempt_value is None and failure.details:
        attempt_value = failure.details.get("attempt")
    suffix = (
        f".{attempt_value}"
        if isinstance(attempt_value, int)
        and not isinstance(attempt_value, bool)
        and attempt_value > 0
        else ""
    )
    stdout_path = artifacts_dir / f"post_validation_conformance{suffix}.stdout"
    stderr_path = artifacts_dir / f"post_validation_conformance{suffix}.stderr"
    stdout_path.write_text(_post_validation_conformance_failure_text(failure), encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    return ValidationResult(
        commands=[
            ValidationCommandResult(
                command="post-validation plan conformance",
                returncode=1,
                duration_seconds=0.0,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                phase="conformance",
                reason_code=failure.reason_code or PLAN_CONFORMANCE_UNSATISFIED,
                policy_failed=True,
            )
        ]
    )


def _post_validation_conformance_failure_text(failure: _PlanningRunFailure) -> str:
    lines = [failure.message]
    details = failure.details or {}
    conformance = details.get("conformance")
    if isinstance(conformance, Mapping):
        summary = conformance.get("summary")
        if isinstance(summary, str) and summary.strip():
            lines.append(f"Summary: {summary.strip()}")
        report_reason = conformance.get("report_reason_code")
        if isinstance(report_reason, str) and report_reason.strip():
            lines.append(f"Report reason code: {report_reason.strip()}")
        gaps = conformance.get("gaps")
        if isinstance(gaps, list):
            gap_lines = [str(gap).strip() for gap in gaps if str(gap).strip()]
            if gap_lines:
                lines.append("Remaining conformance gaps:")
                lines.extend(f"- {gap}" for gap in gap_lines)
    return "\n".join(lines)


def _post_validation_conformance_agent_failure_message(exc: AgentRunError) -> str:
    reason_code = exc.reason_code or "AGENT_CLI_FAILED"
    output = exc.result.stderr.strip() or exc.result.stdout.strip() or "<no output>"
    safe_output = redact_audit_text(output, limit=1000)
    return (
        "post-validation conformance agent failed "
        f"({reason_code}, exit {exc.result.returncode}): {safe_output}"
    )[:2000]


def _post_validation_conformance_agent_failure_details(
    exc: AgentRunError,
    *,
    validation_run_id: str,
) -> dict[str, Any]:
    reason_code = exc.reason_code or "AGENT_CLI_FAILED"
    conformance: dict[str, Any] = {
        "phase": "post_validation",
        "reason_code": reason_code,
        "returncode": exc.result.returncode,
    }
    stdout = redact_audit_text(exc.result.stdout.strip(), limit=1000)
    stderr = redact_audit_text(exc.result.stderr.strip(), limit=1000)
    if stdout:
        conformance["stdout"] = stdout
    if stderr:
        conformance["stderr"] = stderr
    details: dict[str, Any] = {
        "conformance": conformance,
        "validation_run_id": validation_run_id,
    }
    if exc.details:
        details["agent"] = redact_audit_value(exc.details)
    return details


def _git_name_lines(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if line.strip()]


def _format_duration_for_message(value: int | float) -> str:
    return f"{value:g}"
