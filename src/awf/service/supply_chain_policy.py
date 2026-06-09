"""Supply-chain guardrail evaluation for agent-authored workspace output."""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final, Literal
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.models import MergeCandidate, PolicyFinding, TaskAttempt, Workspace
from awf.db.repositories import (
    PolicyFindingCreate,
    PolicyFindingRepository,
    TaskAttemptRepository,
    WorkspaceEventCreate,
    WorkspaceRepository,
    sync_candidate_readiness,
)
from awf.profiles.models import ProfileSupplyChainPolicy

SUPPLY_CHAIN_UNPINNED_DEPENDENCY_INSTALL: Final[str] = "SUPPLY_CHAIN_UNPINNED_DEPENDENCY_INSTALL"
SUPPLY_CHAIN_REMOTE_SCRIPT_EXECUTION: Final[str] = "SUPPLY_CHAIN_REMOTE_SCRIPT_EXECUTION"
SUPPLY_CHAIN_UNEXPECTED_REGISTRY_HOST: Final[str] = "SUPPLY_CHAIN_UNEXPECTED_REGISTRY_HOST"
SUPPLY_CHAIN_LOCKFILE_OUTSIDE_OWNED_PATHS: Final[str] = "SUPPLY_CHAIN_LOCKFILE_OUTSIDE_OWNED_PATHS"
SUPPLY_CHAIN_REASON_CODES: Final[tuple[str, ...]] = (
    SUPPLY_CHAIN_UNPINNED_DEPENDENCY_INSTALL,
    SUPPLY_CHAIN_REMOTE_SCRIPT_EXECUTION,
    SUPPLY_CHAIN_UNEXPECTED_REGISTRY_HOST,
    SUPPLY_CHAIN_LOCKFILE_OUTSIDE_OWNED_PATHS,
)
POLICY_FINDING_EVENT_TYPE: Final[str] = "workspace.policy_finding"

PolicySeverity = Literal["warning", "blocking"]

_LOCKFILE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
        "Cargo.lock",
        "go.sum",
        "composer.lock",
        "Gemfile.lock",
        "mix.lock",
        "gradle.lockfile",
        "packages.lock.json",
    }
)
_KNOWN_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        "npm",
        "pnpm",
        "yarn",
        "bun",
        "pip",
        "pip3",
        "uv",
        "python",
        "python3",
        "curl",
        "wget",
        "sh",
        "bash",
        "zsh",
        "dash",
        "fish",
        "ruby",
        "perl",
        "node",
        "sudo",
        "env",
        "export",
        "command",
    }
)
_PYTHON_EXECUTABLE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^python(?:\d+(?:\.\d+)*)?$")
_PIP_EXECUTABLE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^pip(?:\d+(?:\.\d+)*)?$")
_REMOTE_SCRIPT_INTERPRETERS: Final[frozenset[str]] = frozenset(
    {"sh", "bash", "zsh", "dash", "fish", "python", "python3", "ruby", "perl", "node"}
)
_REMOTE_SCRIPT_INTERPRETER_ENV_VARS: Final[frozenset[str]] = frozenset(
    {
        "BASH",
        "DASH",
        "FISH",
        "NODE",
        "NODE_BINARY",
        "NODE_BIN",
        "PERL",
        "PYTHON",
        "PYTHON3",
        "PYTHON_BINARY",
        "PYTHON_BIN",
        "PYTHON_EXECUTABLE",
        "RUBY",
        "SHELL",
        "SH",
        "ZSH",
    }
)
_SHELL_COMMAND_INTERPRETERS: Final[frozenset[str]] = frozenset(
    {"sh", "bash", "zsh", "dash", "fish"}
)
_SHELL_COMMAND_PAYLOAD_MAX_DEPTH: Final[int] = 4
_SHELL_COMMAND_VALUE_OPTIONS: Final[frozenset[str]] = frozenset({"-o", "+o", "-O", "+O"})
_URL_CREDENTIAL_PATTERN: Final[re.Pattern[str]] = re.compile(r"(https?://)[^/@\s]+(?::[^/@\s]+)?@")
_SHELL_ASSIGNMENT_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PIP_VCS_SPEC_PREFIXES: Final[tuple[str, ...]] = ("git+", "hg+", "svn+", "bzr+")
_NODE_REMOTE_SPEC_PREFIXES: Final[tuple[str, ...]] = (
    "git+",
    "git://",
    "http://",
    "https://",
    "ssh://",
    "github:",
    "gitlab:",
    "bitbucket:",
)
_NODE_LOCAL_SPEC_PREFIXES: Final[tuple[str, ...]] = ("workspace:", "link:")
_NODE_UNPINNED_VERSION_MARKERS: Final[frozenset[str]] = frozenset({"", "*", "latest"})
_NODE_SEMVER_RANGE_PREFIXES: Final[tuple[str, ...]] = ("^", "~", ">", "<")
_NODE_PARTIAL_SEMVER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^v?\d+(?:\.\d+)?$")
_NODE_SEMVER_WILDCARD_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(^|[.\-])(?:x|\*)($|[.\-])",
    re.IGNORECASE,
)
_NODE_GIT_COMMIT_FRAGMENT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$",
    re.IGNORECASE,
)
_NODE_SCP_GIT_SPEC_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[^@\s]+@[^:\s]+:.+")
_PIP_REGISTRY_ENV_VARS: Final[frozenset[str]] = frozenset({"PIP_EXTRA_INDEX_URL", "PIP_INDEX_URL"})
_NODE_REGISTRY_ENV_VARS: Final[frozenset[str]] = frozenset(
    {
        "BUN_CONFIG_REGISTRY",
        "COREPACK_NPM_REGISTRY",
        "NPM_CONFIG_REGISTRY",
        "PNPM_CONFIG_REGISTRY",
        "YARN_NPM_REGISTRY_SERVER",
        "bun_config_registry",
        "corepack_npm_registry",
        "npm_config_registry",
        "pnpm_config_registry",
        "yarn_npm_registry_server",
    }
)
_PIP_GLOBAL_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {
        "--cache-dir",
        "--cert",
        "--client-cert",
        "--exists-action",
        "--keyring-provider",
        "--log",
        "--proxy",
        "--python",
        "--retries",
        "--timeout",
        "--trusted-host",
        "--use-deprecated",
        "--use-feature",
    }
)
_PYTHON_INTERPRETER_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {"--check-hash-based-pycs", "-W", "-X"}
)
_PYTHON_INTERPRETER_ATTACHED_VALUE_PREFIXES: Final[tuple[str, ...]] = ("-W", "-X")
_SHELL_CONTROL_OPERATORS: Final[frozenset[str]] = frozenset({";", "&&", "||"})
_SHELL_PIPE_OPERATORS: Final[frozenset[str]] = frozenset({"|", "|&"})
_SHELL_PACKAGE_BOUNDARIES: Final[frozenset[str]] = _SHELL_CONTROL_OPERATORS | _SHELL_PIPE_OPERATORS


@dataclass(frozen=True)
class SupplyChainFinding:
    reason_code: str
    severity: PolicySeverity
    subject_path: str | None
    explanation: str
    details: dict[str, object]


@dataclass(frozen=True)
class SupplyChainPolicyRefreshResult:
    workspace_id: str
    candidate_id: str | None
    changed_paths: tuple[str, ...]
    findings: list[SupplyChainFinding]
    newly_added: list[PolicyFinding]
    newly_resolved: list[PolicyFinding]
    policy_blocked: bool


class SupplyChainPolicyRefreshError(RuntimeError):
    """Raised when supply-chain policy refresh cannot proceed."""


def evaluate_supply_chain_policy(
    *,
    command_evidence: str | Sequence[str],
    changed_paths: Sequence[str],
    owned_paths: Sequence[str],
    policy: ProfileSupplyChainPolicy,
) -> list[SupplyChainFinding]:
    """Return supply-chain findings from command evidence and changed files."""

    findings: list[SupplyChainFinding] = []
    for command in _command_lines(command_evidence):
        tokens = _shell_tokens(command)
        if not tokens:
            continue
        remote_script = _remote_script_execution(command, tokens, policy=policy)
        if remote_script is not None:
            findings.append(remote_script)
        exported_env_assignments: dict[str, str] = {}
        for package_tokens in _command_token_segments(tokens, _SHELL_PACKAGE_BOUNDARIES):
            _remember_env_assignments(
                exported_env_assignments,
                _export_env_assignments(package_tokens),
            )
            package_command = _package_command(
                command,
                package_tokens,
                env_assignments=tuple(exported_env_assignments.values()),
            )
            if package_command is None:
                continue
            unpinned = _unpinned_dependency_install_finding(
                command,
                package_command,
                policy=policy,
            )
            if unpinned is not None:
                findings.append(unpinned)
            registry = _unexpected_registry_finding(command, package_command, policy=policy)
            if registry is not None:
                findings.append(registry)

    for path in _normalized_unique_paths(changed_paths):
        if not _is_lockfile_path(path):
            continue
        if _matches_any(path, owned_paths):
            continue
        severity = _severity(policy.lockfile_changes_outside_owned_paths.mode.value)
        findings.append(
            SupplyChainFinding(
                reason_code=SUPPLY_CHAIN_LOCKFILE_OUTSIDE_OWNED_PATHS,
                severity=severity,
                subject_path=path,
                explanation=(f"Lockfile '{path}' changed outside declared owned_paths."),
                details={
                    "guardrail": "lockfile_changes_outside_owned_paths",
                    "owned_paths": list(owned_paths),
                    "mode": policy.lockfile_changes_outside_owned_paths.mode.value,
                    "recovery_guidance": (
                        "Add the lockfile path to owned_paths for dependency work, "
                        "or remove the lockfile change before retrying."
                    ),
                },
            )
        )
    return findings


def supply_chain_policy_for_workspace(workspace: Workspace) -> ProfileSupplyChainPolicy:
    """Resolve supply-chain policy from the workspace's resolved profile snapshot."""

    section = _nested_dict(workspace.resolved_profile, "security", "supply_chain")
    if section is None:
        return ProfileSupplyChainPolicy()
    try:
        return ProfileSupplyChainPolicy.model_validate(section)
    except ValueError:
        return ProfileSupplyChainPolicy()


class SupplyChainPolicyRefreshService:
    """Persist active supply-chain findings for a workspace or merge candidate."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def refresh_workspace(
        self,
        workspace_id: str,
        *,
        command_evidence: str | Sequence[str] = (),
        changed_paths: Sequence[str] = (),
    ) -> SupplyChainPolicyRefreshResult:
        workspace = await WorkspaceRepository(self._session).get(workspace_id)
        if workspace is None:
            raise SupplyChainPolicyRefreshError(f"Workspace {workspace_id!r} not found")
        attempt = await TaskAttemptRepository(self._session).get_by_workspace_id(workspace_id)
        return await self._refresh(
            workspace=workspace,
            candidate=None,
            attempt=attempt,
            command_evidence=command_evidence,
            changed_paths=changed_paths,
        )

    async def refresh_workspace_open_candidate(
        self,
        workspace_id: str,
        *,
        command_evidence: str | Sequence[str] = (),
        changed_paths: Sequence[str] = (),
    ) -> SupplyChainPolicyRefreshResult:
        candidate = await _load_open_candidate_for_workspace(self._session, workspace_id)
        if candidate is None:
            return await self.refresh_workspace(
                workspace_id,
                command_evidence=command_evidence,
                changed_paths=changed_paths,
            )
        return await self._refresh(
            workspace=candidate.workspace,
            candidate=candidate,
            attempt=candidate.attempt,
            command_evidence=command_evidence,
            changed_paths=changed_paths,
        )

    async def refresh_candidate(
        self,
        candidate_id: str,
        *,
        command_evidence: str | Sequence[str] = (),
        changed_paths: Sequence[str] = (),
    ) -> SupplyChainPolicyRefreshResult:
        candidate = await _load_candidate(self._session, candidate_id)
        if candidate is None:
            raise SupplyChainPolicyRefreshError(f"Merge candidate {candidate_id!r} not found")
        return await self._refresh(
            workspace=candidate.workspace,
            candidate=candidate,
            attempt=candidate.attempt,
            command_evidence=command_evidence,
            changed_paths=changed_paths,
        )

    async def _refresh(
        self,
        *,
        workspace: Workspace,
        candidate: MergeCandidate | None,
        attempt: TaskAttempt | None,
        command_evidence: str | Sequence[str],
        changed_paths: Sequence[str],
    ) -> SupplyChainPolicyRefreshResult:
        normalized_changed_paths = tuple(_normalized_unique_paths(changed_paths))
        policy = supply_chain_policy_for_workspace(workspace)
        owned_paths = (
            tuple(workspace.owned_paths)
            if workspace.owned_paths
            else (tuple(attempt.owned_paths) if attempt is not None else ())
        )
        findings = evaluate_supply_chain_policy(
            command_evidence=command_evidence,
            changed_paths=normalized_changed_paths,
            owned_paths=owned_paths,
            policy=policy,
        )
        findings_by_reason = {
            reason_code: [finding for finding in findings if finding.reason_code == reason_code]
            for reason_code in SUPPLY_CHAIN_REASON_CODES
        }
        repo = PolicyFindingRepository(self._session)
        newly_added: list[PolicyFinding] = []
        newly_resolved: list[PolicyFinding] = []
        for reason_code, reason_findings in findings_by_reason.items():
            added, resolved = await repo.replace_active_findings(
                workspace_id=workspace.id,
                candidate_id=candidate.id if candidate is not None else None,
                attempt_id=attempt.id if attempt is not None else None,
                task_id=attempt.task_id if attempt is not None else None,
                reason_code=reason_code,
                findings=[
                    PolicyFindingCreate(
                        reason_code=f.reason_code,
                        severity=f.severity,
                        subject_path=f.subject_path,
                        explanation=f.explanation,
                        details=f.details,
                    )
                    for f in reason_findings
                ],
            )
            newly_added.extend(added)
            newly_resolved.extend(resolved)

        policy_blocked = any(f.severity == "blocking" for f in findings)
        if candidate is not None:
            active_findings = await repo.list_active_for_candidate(candidate.id)
            policy_blocked = any(f.severity == "blocking" for f in active_findings)
            if candidate.policy_blocked != policy_blocked:
                candidate.policy_blocked = policy_blocked
            sync_candidate_readiness(
                candidate,
                workspace=candidate.workspace,
                attempt=candidate.attempt,
            )
        if newly_added:
            await self._emit_events(
                workspace=workspace,
                candidate=candidate,
                attempt=attempt,
                added=newly_added,
            )
        await self._session.flush()
        return SupplyChainPolicyRefreshResult(
            workspace_id=workspace.id,
            candidate_id=candidate.id if candidate is not None else None,
            changed_paths=normalized_changed_paths,
            findings=findings,
            newly_added=list(newly_added),
            newly_resolved=list(newly_resolved),
            policy_blocked=policy_blocked,
        )

    async def _emit_events(
        self,
        *,
        workspace: Workspace,
        candidate: MergeCandidate | None,
        attempt: TaskAttempt | None,
        added: Iterable[PolicyFinding],
    ) -> None:
        events = [
            WorkspaceEventCreate(
                event_type=POLICY_FINDING_EVENT_TYPE,
                reason_code=row.reason_code,
                payload={
                    "finding_id": row.id,
                    "candidate_id": candidate.id if candidate is not None else None,
                    "attempt_id": attempt.id if attempt is not None else None,
                    "task_id": attempt.task_id if attempt is not None else None,
                    "severity": row.severity,
                    "path": row.subject_path,
                    "explanation": row.explanation,
                    "details": row.details,
                    "detected_at": _isoformat(row.detected_at),
                },
            )
            for row in added
        ]
        await WorkspaceRepository(self._session).add_events(workspace, events=events)


@dataclass(frozen=True)
class _PackageCommand:
    manager: str
    operation: str
    package_specs: tuple[str, ...]
    registry_hosts: tuple[str, ...]


@dataclass(frozen=True)
class _PackageCommandPrefixes:
    tokens: list[str]
    env_assignments: tuple[str, ...]


@dataclass(frozen=True)
class _SkippedWrapperOptions:
    index: int
    env_assignments: tuple[str, ...]


def _remote_script_execution(
    command: str,
    tokens: list[str],
    *,
    policy: ProfileSupplyChainPolicy,
) -> SupplyChainFinding | None:
    if not (
        _has_piped_remote_script_execution(tokens)
        or _has_chained_remote_script_execution(tokens)
        or _has_process_substitution_remote_script_execution(tokens)
        or _has_command_substitution_remote_script_execution(tokens)
    ):
        return None
    severity = _severity(policy.remote_script_execution.mode.value)
    return SupplyChainFinding(
        reason_code=SUPPLY_CHAIN_REMOTE_SCRIPT_EXECUTION,
        severity=severity,
        subject_path=None,
        explanation="Remote script execution through an interpreter was detected.",
        details={
            "guardrail": "remote_script_execution",
            "command_excerpt": _redact_command_excerpt(command),
            "mode": policy.remote_script_execution.mode.value,
            "recovery_guidance": (
                "Download the remote script, inspect and pin its source, then "
                "run a local checked-in script or documented installer step."
            ),
        },
    )


def _has_piped_remote_script_execution(tokens: Sequence[str]) -> bool:
    for segment in _command_token_segments(tokens, _SHELL_CONTROL_OPERATORS):
        for index, token in enumerate(segment):
            if token not in _SHELL_PIPE_OPERATORS:
                continue
            if not _is_remote_fetch(segment[:index]):
                continue
            if not _pipe_target_is_interpreter(segment[index + 1 :]):
                continue
            return True
    return False


def _has_chained_remote_script_execution(tokens: Sequence[str]) -> bool:
    segments = _command_token_segments(tokens, _SHELL_CONTROL_OPERATORS)
    for index, fetch_tokens in enumerate(segments[:-1]):
        artifact_names = _remote_fetch_artifact_names(fetch_tokens)
        if not artifact_names:
            continue
        for interpreter_tokens in segments[index + 1 :]:
            target = _interpreter_script_target(interpreter_tokens)
            if target is None:
                continue
            if PurePosixPath(target).name in artifact_names:
                return True
    return False


def _has_process_substitution_remote_script_execution(tokens: Sequence[str]) -> bool:
    for segment in _command_token_segments(tokens, _SHELL_CONTROL_OPERATORS):
        if not _pipe_target_is_interpreter(segment):
            continue
        for index, token in enumerate(segment):
            fetch_tokens: Sequence[str]
            if token == "<(":
                fetch_tokens = segment[index + 1 :]
            elif token.startswith("<("):
                fetch_tokens = (token[2:], *segment[index + 1 :])
            else:
                continue
            if not fetch_tokens:
                continue
            command = PurePosixPath(_shell_token_word(fetch_tokens[0])).name
            if command not in {"curl", "wget"}:
                continue
            for arg in _process_substitution_args(fetch_tokens[1:]):
                if _is_remote_url_token(arg):
                    return True
    return False


def _has_command_substitution_remote_script_execution(tokens: Sequence[str]) -> bool:
    for segment in _command_token_segments(tokens, _SHELL_CONTROL_OPERATORS):
        if not segment[0].strip().startswith("$("):
            continue
        if not any(token.rstrip().endswith(")") for token in segment):
            continue
        if _is_remote_fetch(segment):
            return True
    return False


def _unpinned_dependency_install_finding(
    command: str,
    package_command: _PackageCommand,
    *,
    policy: ProfileSupplyChainPolicy,
) -> SupplyChainFinding | None:
    if not package_command.package_specs:
        return None
    unpinned = [
        spec
        for spec in package_command.package_specs
        if not _is_pinned_or_local_spec(package_command.manager, spec)
    ]
    if not unpinned:
        return None
    severity = _severity(policy.unpinned_dependency_installs.mode.value)
    return SupplyChainFinding(
        reason_code=SUPPLY_CHAIN_UNPINNED_DEPENDENCY_INSTALL,
        severity=severity,
        subject_path=None,
        explanation=(
            "Package installation command contains unpinned dependency "
            f"specifier(s): {', '.join(unpinned)}."
        ),
        details={
            "guardrail": "unpinned_dependency_installs",
            "manager": package_command.manager,
            "operation": package_command.operation,
            "unpinned_specs": unpinned,
            "command_excerpt": _redact_command_excerpt(command),
            "mode": policy.unpinned_dependency_installs.mode.value,
            "recovery_guidance": (
                "Pin the dependency version or use the repository lockfile-aware "
                "installer before retrying."
            ),
        },
    )


def _unexpected_registry_finding(
    command: str,
    package_command: _PackageCommand,
    *,
    policy: ProfileSupplyChainPolicy,
) -> SupplyChainFinding | None:
    allowed_hosts = set(policy.unexpected_registry_hosts.allowed_hosts)
    unexpected = [host for host in package_command.registry_hosts if host not in allowed_hosts]
    if not unexpected:
        return None
    severity = _severity(policy.unexpected_registry_hosts.mode.value)
    return SupplyChainFinding(
        reason_code=SUPPLY_CHAIN_UNEXPECTED_REGISTRY_HOST,
        severity=severity,
        subject_path=None,
        explanation=(
            "Package installation command references unexpected registry host(s): "
            f"{', '.join(unexpected)}."
        ),
        details={
            "guardrail": "unexpected_registry_hosts",
            "manager": package_command.manager,
            "registry_hosts": unexpected,
            "allowed_hosts": list(policy.unexpected_registry_hosts.allowed_hosts),
            "command_excerpt": _redact_command_excerpt(command),
            "mode": policy.unexpected_registry_hosts.mode.value,
            "recovery_guidance": (
                "Use a declared registry host, add the intended host to the "
                "profile allowlist, or remove the registry override."
            ),
        },
    )


def _package_command(
    command: str,
    tokens: list[str],
    *,
    env_assignments: Sequence[str] = (),
) -> _PackageCommand | None:
    del command
    prefixes = _strip_package_command_prefixes(tokens)
    prefix_env_assignments = _combined_env_assignments(
        env_assignments,
        prefixes.env_assignments,
    )
    if not prefixes.tokens:
        return None
    first = PurePosixPath(_shell_token_word(prefixes.tokens[0])).name
    if _is_python_executable(first):
        pip_args = _python_m_pip_args(prefixes.tokens)
        if pip_args is None:
            return None
        return _pip_command(
            pip_args,
            manager="pip",
            env_assignments=prefix_env_assignments,
        )
    if len(prefixes.tokens) >= 2 and first == "uv" and prefixes.tokens[1] == "pip":
        return _pip_command(
            prefixes.tokens[2:],
            manager="uv pip",
            env_assignments=prefix_env_assignments,
        )
    if _is_pip_executable(first):
        return _pip_command(
            prefixes.tokens[1:],
            manager="pip",
            env_assignments=prefix_env_assignments,
        )
    if first in {"npm", "pnpm", "yarn", "bun"}:
        return _node_package_command(
            prefixes.tokens[1:],
            manager=first,
            env_assignments=prefix_env_assignments,
        )
    return None


def _python_m_pip_args(tokens: list[str]) -> list[str] | None:
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return None
        if token == "-m":
            if index + 1 < len(tokens) and _shell_token_word(tokens[index + 1]) == "pip":
                return tokens[index + 2 :]
            return None
        if token.startswith("-m") and token != "-m":
            return tokens[index + 1 :] if token[2:] == "pip" else None
        if token in _PYTHON_INTERPRETER_VALUE_FLAGS:
            index += 2
            continue
        if any(token.startswith(f"{flag}=") for flag in _PYTHON_INTERPRETER_VALUE_FLAGS):
            index += 1
            continue
        if any(
            token.startswith(prefix) and token != prefix
            for prefix in _PYTHON_INTERPRETER_ATTACHED_VALUE_PREFIXES
        ):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return None
    return None


def _strip_package_command_prefixes(tokens: list[str]) -> _PackageCommandPrefixes:
    index = 0
    env_assignments: list[str] = []
    while index < len(tokens):
        token = tokens[index]
        command = PurePosixPath(_shell_token_word(token)).name
        if _is_shell_assignment(token):
            env_assignments.append(token)
            index += 1
            continue
        if command == "sudo":
            index += 1
            skipped = _skip_wrapper_options(
                tokens,
                index,
                value_flags={
                    "-a",
                    "--auth-type",
                    "-C",
                    "--close-from",
                    "-c",
                    "--login-class",
                    "-g",
                    "--group",
                    "-h",
                    "--host",
                    "-p",
                    "--prompt",
                    "-R",
                    "--chroot",
                    "-r",
                    "--role",
                    "-t",
                    "--type",
                    "-U",
                    "--other-user",
                    "-u",
                    "--user",
                },
            )
            index = skipped.index
            continue
        if command == "env":
            index += 1
            skipped = _skip_wrapper_options(
                tokens,
                index,
                value_flags={
                    "--argv0",
                    "-C",
                    "--chdir",
                    "-S",
                    "--split-string",
                    "-u",
                    "--unset",
                },
                skip_assignments=True,
            )
            env_assignments.extend(skipped.env_assignments)
            index = skipped.index
            continue
        if command == "export":
            index += 1
            skipped = _skip_wrapper_options(tokens, index, skip_assignments=True)
            env_assignments.extend(skipped.env_assignments)
            index = skipped.index
            continue
        if command == "command":
            index += 1
            skipped = _skip_wrapper_options(tokens, index)
            index = skipped.index
            continue
        break
    return _PackageCommandPrefixes(
        tokens=tokens[index:],
        env_assignments=tuple(env_assignments),
    )


def _skip_wrapper_options(
    tokens: list[str],
    index: int,
    *,
    value_flags: set[str] | None = None,
    skip_assignments: bool = False,
) -> _SkippedWrapperOptions:
    flags_with_values = value_flags or set()
    env_assignments: list[str] = []
    while index < len(tokens):
        token = tokens[index]
        if skip_assignments and _is_shell_assignment(token):
            env_assignments.append(token)
            index += 1
            continue
        if token == "--":
            return _SkippedWrapperOptions(
                index=index + 1,
                env_assignments=tuple(env_assignments),
            )
        if token in flags_with_values:
            index += 2
            continue
        if any(token.startswith(f"{flag}=") for flag in flags_with_values):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return _SkippedWrapperOptions(
            index=index,
            env_assignments=tuple(env_assignments),
        )
    return _SkippedWrapperOptions(index=index, env_assignments=tuple(env_assignments))


def _is_shell_assignment(token: str) -> bool:
    return _SHELL_ASSIGNMENT_PATTERN.match(token) is not None


def _export_env_assignments(tokens: Sequence[str]) -> tuple[str, ...]:
    if not tokens:
        return ()
    command = PurePosixPath(_shell_token_word(tokens[0])).name
    if command != "export":
        return ()
    skipped = _skip_wrapper_options(list(tokens), 1, skip_assignments=True)
    return skipped.env_assignments


def _remember_env_assignments(
    target: dict[str, str],
    assignments: Sequence[str],
) -> None:
    for assignment in assignments:
        name, separator, _ = assignment.partition("=")
        if separator == "=":
            target[name] = assignment


def _combined_env_assignments(
    *assignment_groups: Sequence[str],
) -> tuple[str, ...]:
    combined: dict[str, str] = {}
    for assignments in assignment_groups:
        _remember_env_assignments(combined, assignments)
    return tuple(combined.values())


def _pip_command(
    tokens: list[str],
    *,
    manager: str,
    env_assignments: Sequence[str] = (),
) -> _PackageCommand | None:
    install_index = _pip_install_index(tokens)
    if install_index is None:
        return None
    args = tokens[install_index + 1 :]
    packages = tuple(_package_args(args, manager="pip"))
    registries = tuple(_registry_hosts(args, manager="pip", env_assignments=env_assignments))
    return _PackageCommand(
        manager=manager,
        operation="install",
        package_specs=packages,
        registry_hosts=registries,
    )


def _pip_install_index(tokens: list[str]) -> int | None:
    skipped = _skip_wrapper_options(
        tokens,
        0,
        value_flags=set(_PIP_GLOBAL_VALUE_FLAGS),
    )
    if skipped.index >= len(tokens) or tokens[skipped.index] != "install":
        return None
    return skipped.index


def _node_package_command(
    tokens: list[str],
    *,
    manager: str,
    env_assignments: Sequence[str] = (),
) -> _PackageCommand | None:
    if not tokens:
        return None
    operation = tokens[0]
    install_ops = {"install", "i", "add"}
    args = tokens[1:]
    if manager == "npm" and operation == "ci":
        return _PackageCommand(
            manager=manager,
            operation=operation,
            package_specs=(),
            registry_hosts=tuple(
                _registry_hosts(args, manager=manager, env_assignments=env_assignments)
            ),
        )
    if operation not in install_ops:
        return None
    packages: tuple[str, ...]
    if operation in {"install", "i"} and _has_any_flag(
        args,
        {
            "--frozen-lockfile",
            "--frozen",
            "--immutable",
            "--pure-lockfile",
            "--offline",
        },
    ):
        packages = ()
    else:
        packages = tuple(_package_args(args, manager=manager))
    return _PackageCommand(
        manager=manager,
        operation=operation,
        package_specs=packages,
        registry_hosts=tuple(
            _registry_hosts(args, manager=manager, env_assignments=env_assignments)
        ),
    )


def _package_args(args: Sequence[str], *, manager: str) -> list[str]:
    packages: list[str] = []
    skip_next = False
    value_flags = _value_flags(manager)
    for index, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if arg == "--":
            packages.extend(item for item in args[index + 1 :] if not item.startswith("-"))
            break
        if arg in {"-e", "--editable"} and manager == "pip":
            next_arg = args[index + 1] if index + 1 < len(args) else ""
            if _is_local_spec(next_arg):
                skip_next = True
                continue
            if next_arg:
                packages.append(next_arg)
                skip_next = True
            continue
        if arg in value_flags:
            skip_next = True
            continue
        if any(arg.startswith(f"{flag}=") for flag in value_flags):
            continue
        if arg.startswith("-"):
            continue
        if manager == "pip" and _looks_like_requirements_file(arg):
            continue
        packages.append(arg)
    return packages


def _registry_hosts(
    args: Sequence[str],
    *,
    manager: str,
    env_assignments: Sequence[str] = (),
) -> list[str]:
    hosts: list[str] = []
    if manager == "pip":
        for env_host in _pip_env_registry_hosts(env_assignments):
            if env_host not in hosts:
                hosts.append(env_host)
    elif manager in {"npm", "pnpm", "yarn", "bun"}:
        for env_host in _node_env_registry_hosts(env_assignments):
            if env_host not in hosts:
                hosts.append(env_host)
    flags = (
        {"--registry"}
        if manager in {"npm", "pnpm", "yarn", "bun"}
        else {"--index-url", "--extra-index-url", "-i"}
    )
    attached_short_flags = {"-i"} if manager == "pip" else set()
    for index, arg in enumerate(args):
        value: str | None = None
        if arg in flags and index + 1 < len(args):
            value = args[index + 1]
        else:
            for flag in flags:
                prefix = f"{flag}="
                if arg.startswith(prefix):
                    value = arg[len(prefix) :]
                    break
                if flag in attached_short_flags and arg.startswith(flag):
                    value = arg[len(flag) :]
                    break
        if value is None:
            continue
        host = _host_from_url(value)
        if host is not None and host not in hosts:
            hosts.append(host)
    return hosts


def _pip_env_registry_hosts(env_assignments: Sequence[str]) -> list[str]:
    hosts: list[str] = []
    for assignment in env_assignments:
        name, separator, value = assignment.partition("=")
        if separator != "=" or name not in _PIP_REGISTRY_ENV_VARS:
            continue
        values = _shell_tokens(value) or [value]
        for item in values:
            host = _host_from_url(item)
            if host is not None and host not in hosts:
                hosts.append(host)
    return hosts


def _node_env_registry_hosts(env_assignments: Sequence[str]) -> list[str]:
    hosts: list[str] = []
    for assignment in env_assignments:
        name, separator, value = assignment.partition("=")
        if separator != "=" or name not in _NODE_REGISTRY_ENV_VARS:
            continue
        values = _shell_tokens(value) or [value]
        for item in values:
            host = _host_from_url(item)
            if host is not None and host not in hosts:
                hosts.append(host)
    return hosts


def _host_from_url(value: str) -> str | None:
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    host = parsed.hostname
    if host is None:
        return None
    return host.lower().rstrip(".")


def _value_flags(manager: str) -> set[str]:
    if manager == "pip":
        return {
            "-r",
            "--requirement",
            "-c",
            "--constraint",
            "-i",
            "--index-url",
            "--extra-index-url",
            "-C",
            "--config-settings",
            "-f",
            "--find-links",
            "--abi",
            "--build-option",
            "--global-option",
            "--group",
            "--implementation",
            "--no-binary",
            "--only-binary",
            "--platform",
            "--progress-bar",
            "--python-version",
            "--report",
            "--root",
            "--src",
            "--trusted-host",
            "-t",
            "--target",
            "--upgrade-strategy",
            "--python",
        }
    return {
        "--registry",
        "--cache",
        "--prefix",
        "--workspace",
        "-w",
        "--tag",
        "--filter",
        "--include",
        "--omit",
        "--install-strategy",
        "--save-prefix",
        "--mode",
        "--cache-folder",
        "--modules-folder",
        "--cwd",
        "--cpu",
        "--os",
        "--libc",
    }


def _is_pinned_or_local_spec(manager: str, spec: str) -> bool:
    if _is_local_spec(spec):
        return True
    if manager in {"npm", "pnpm", "yarn", "bun"}:
        return _is_pinned_node_spec(spec)
    return _is_pinned_pip_spec(spec)


def _is_pinned_pip_spec(spec: str) -> bool:
    return (
        "==" in spec
        or "===" in spec
        or spec.startswith(("-r", "--requirement"))
        or spec.startswith(("file:", "git+file:"))
        or _is_pinned_pip_vcs_spec(spec)
    )


def _is_pinned_pip_vcs_spec(spec: str) -> bool:
    direct_ref = " @ "
    target = spec.split(direct_ref, maxsplit=1)[1] if direct_ref in spec else spec
    normalized = target.strip()
    prefix = next(
        (candidate for candidate in _PIP_VCS_SPEC_PREFIXES if normalized.startswith(candidate)),
        None,
    )
    if prefix is None:
        return False
    vcs_url = normalized.removeprefix(prefix).split("#", maxsplit=1)[0]
    parsed = urlsplit(vcs_url)
    if "@" not in parsed.path:
        return False
    revision = parsed.path.rsplit("@", maxsplit=1)[1]
    return bool(revision.strip())


def _is_pinned_node_spec(spec: str) -> bool:
    if spec.startswith(_NODE_LOCAL_SPEC_PREFIXES):
        return True
    fragment = _node_spec_fragment(spec)
    if fragment is not None:
        return _node_git_fragment_is_pinned(fragment)
    if _looks_like_node_remote_spec(spec):
        return False
    version = _node_package_version(spec)
    return version is not None and _node_pin_value_is_pinned(version)


def _node_spec_fragment(spec: str) -> str | None:
    if "#" not in spec:
        return None
    return spec.rsplit("#", maxsplit=1)[1]


def _looks_like_node_remote_spec(spec: str) -> bool:
    return spec.startswith(_NODE_REMOTE_SPEC_PREFIXES) or (
        _NODE_SCP_GIT_SPEC_PATTERN.match(spec) is not None
    )


def _node_package_version(spec: str) -> str | None:
    alias_marker = "@npm:"
    if alias_marker in spec:
        return _node_package_version(spec.split(alias_marker, maxsplit=1)[1])
    if spec.startswith("@"):
        _, _, package_part = spec.partition("/")
        if "@" not in package_part:
            return None
        return package_part.rsplit("@", maxsplit=1)[1]
    if "@" not in spec:
        return None
    return spec.rsplit("@", maxsplit=1)[1]


def _node_git_fragment_is_pinned(fragment: str) -> bool:
    return _NODE_GIT_COMMIT_FRAGMENT_PATTERN.fullmatch(fragment.strip()) is not None


def _node_pin_value_is_pinned(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized.startswith("semver:"):
        normalized = normalized.removeprefix("semver:").strip()
    return normalized not in _NODE_UNPINNED_VERSION_MARKERS and not _node_pin_value_is_semver_range(
        normalized
    )


def _node_pin_value_is_semver_range(value: str) -> bool:
    return (
        value.startswith(_NODE_SEMVER_RANGE_PREFIXES)
        or "||" in value
        or " - " in value
        or any(character.isspace() for character in value)
        or _NODE_PARTIAL_SEMVER_PATTERN.fullmatch(value) is not None
        or _NODE_SEMVER_WILDCARD_PATTERN.search(value) is not None
    )


def _is_local_spec(spec: str) -> bool:
    return spec in {".", "./"} or spec.startswith(("./", "../", "/", "file:"))


def _looks_like_requirements_file(spec: str) -> bool:
    if _looks_like_pip_url_install_target(spec):
        return False
    return spec.endswith((".txt", ".in", ".lock")) or "/" in spec


def _looks_like_pip_url_install_target(spec: str) -> bool:
    normalized = spec.strip().lower()
    return "://" in normalized or normalized.startswith(_PIP_VCS_SPEC_PREFIXES)


def _remote_fetch_artifact_names(tokens: Sequence[str]) -> set[str]:
    if not _is_remote_fetch(tokens):
        return set()
    artifact_names: set[str] = set()
    for token in tokens[1:]:
        url = _remote_url_value(token)
        if url is None:
            continue
        name = PurePosixPath(urlsplit(url).path).name
        if name:
            artifact_names.add(name)
    for target in _remote_fetch_output_targets(tokens):
        name = PurePosixPath(target).name
        if name:
            artifact_names.add(name)
    return artifact_names


def _remote_fetch_output_targets(tokens: Sequence[str]) -> list[str]:
    if not tokens:
        return []
    command = PurePosixPath(_shell_token_word(tokens[0])).name
    targets: list[str] = []
    args = list(tokens[1:])
    for index, arg in enumerate(args):
        if command == "curl":
            if arg in {"-o", "--output"} and index + 1 < len(args):
                targets.append(args[index + 1])
            elif arg.startswith("--output="):
                targets.append(arg.split("=", maxsplit=1)[1])
            elif arg.startswith("-") and not arg.startswith("--"):
                option_tail = arg[1:]
                output_flag_index = option_tail.rfind("o")
                if output_flag_index == -1:
                    continue
                attached_value = option_tail[output_flag_index + 1 :]
                if attached_value:
                    targets.append(attached_value)
                elif index + 1 < len(args):
                    targets.append(args[index + 1])
        elif command == "wget":
            if arg in {"-O", "--output-document"} and index + 1 < len(args):
                targets.append(args[index + 1])
            elif arg.startswith("--output-document="):
                targets.append(arg.split("=", maxsplit=1)[1])
            elif arg.startswith("-") and not arg.startswith("--"):
                output_flag_index = arg.rfind("O")
                if output_flag_index == -1:
                    continue
                attached_value = arg[output_flag_index + 1 :]
                if attached_value:
                    targets.append(attached_value)
                elif index + 1 < len(args):
                    targets.append(args[index + 1])
    return [target for target in targets if target != "-"]


def _has_any_flag(args: Sequence[str], flags: set[str]) -> bool:
    return any(arg in flags or any(arg.startswith(f"{flag}=") for flag in flags) for arg in args)


def _is_remote_fetch(tokens: Sequence[str]) -> bool:
    if not tokens:
        return False
    command = PurePosixPath(_shell_token_word(tokens[0])).name
    if command not in {"curl", "wget"}:
        return False
    return any(_is_remote_url_token(token) for token in tokens[1:])


def _pipe_target_is_interpreter(tokens: Sequence[str]) -> bool:
    return _interpreter_index(tokens) is not None


def _interpreter_index(tokens: Sequence[str]) -> int | None:
    if not tokens:
        return None
    for index, token in enumerate(tokens):
        if token in {"env", "sudo", "command"} or "=" in token:
            continue
        command = PurePosixPath(_shell_token_word(token)).name
        if _is_remote_script_interpreter(command):
            return index
        return None
    return None


def _is_remote_script_interpreter(command: str) -> bool:
    return (
        command in _REMOTE_SCRIPT_INTERPRETERS
        or _PYTHON_EXECUTABLE_PATTERN.fullmatch(command) is not None
        or _is_remote_script_interpreter_env_var(command)
    )


def _is_remote_script_interpreter_env_var(command: str) -> bool:
    name = _shell_variable_name(command)
    return name in _REMOTE_SCRIPT_INTERPRETER_ENV_VARS if name is not None else False


def _shell_variable_name(value: str) -> str | None:
    if value.startswith("${"):
        candidate = value[2:]
        if candidate.endswith("}"):
            candidate = candidate[:-1]
    elif value.startswith("$"):
        candidate = value[1:]
    else:
        return None
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", candidate):
        return candidate
    return None


def _interpreter_script_target(tokens: Sequence[str]) -> str | None:
    interpreter_index = _interpreter_index(tokens)
    if interpreter_index is None:
        return None
    for token in tokens[interpreter_index + 1 :]:
        if token.startswith("-") or token in {"<", ">", ">>", "2>", "1>"}:
            continue
        return token
    return None


def _process_substitution_args(tokens: Sequence[str]) -> Iterable[str]:
    for token in tokens:
        yield token
        if token.endswith(")"):
            return


def _is_remote_url_token(token: str) -> bool:
    return _remote_url_value(token) is not None


def _remote_url_value(token: str) -> str | None:
    value = _shell_token_word(token)
    if value.startswith(("http://", "https://")):
        return value
    return None


def _shell_token_word(token: str) -> str:
    word = token.strip()
    while word.startswith("$("):
        word = word[2:]
    return word.strip().strip("()[]{};,")


def _command_lines(command_evidence: str | Sequence[str]) -> list[str]:
    blobs = [command_evidence] if isinstance(command_evidence, str) else list(command_evidence)
    commands: list[str] = []
    for blob in blobs:
        for line in str(blob).splitlines():
            command = _command_from_line(line)
            if command:
                commands.extend(_command_with_shell_payloads(command))
    return commands


def _command_with_shell_payloads(command: str, *, depth: int = 0) -> list[str]:
    commands = [command]
    if depth >= _SHELL_COMMAND_PAYLOAD_MAX_DEPTH:
        return commands
    for payload in _shell_command_payloads(_shell_tokens(command)):
        for line in payload.splitlines():
            nested = line.strip()
            if nested:
                commands.extend(_command_with_shell_payloads(nested, depth=depth + 1))
    return commands


def _shell_command_payloads(tokens: Sequence[str]) -> list[str]:
    payloads: list[str] = []
    for segment in _command_token_segments(tokens, _SHELL_PACKAGE_BOUNDARIES):
        prefixes = _strip_package_command_prefixes(list(segment))
        payload = _shell_command_payload(prefixes.tokens)
        if payload:
            payloads.append(payload)
    return payloads


def _shell_command_payload(tokens: Sequence[str]) -> str | None:
    if not tokens:
        return None
    command = PurePosixPath(_shell_token_word(tokens[0])).name
    if command not in _SHELL_COMMAND_INTERPRETERS:
        return None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "-c" or (command == "fish" and token == "--command"):
            return tokens[index + 1] if index + 1 < len(tokens) else None
        if command == "fish" and token.startswith("--command="):
            return token.split("=", maxsplit=1)[1]
        if token in _SHELL_COMMAND_VALUE_OPTIONS:
            index += 2
            continue
        if token.startswith("-") and not token.startswith("--"):
            if "c" in token[1:]:
                return tokens[index + 1] if index + 1 < len(tokens) else None
            index += 1
            continue
        if token.startswith("--") or token.startswith("+"):
            index += 1
            continue
        return None
    return None


def _command_from_line(line: str) -> str | None:
    stripped = line.strip()
    if not stripped:
        return None
    for prefix in ("$ ", "> ", "+ "):
        if stripped.startswith(prefix):
            return stripped[len(prefix) :].strip()
    lower = stripped.lower()
    for label in ("command:", "executed:", "shell:", "run:"):
        if lower.startswith(label):
            return stripped[len(label) :].strip()
    first = stripped.split(maxsplit=1)[0]
    command = PurePosixPath(first).name
    if _is_known_evidence_command(command):
        return stripped
    tokens = _shell_tokens(stripped)
    if not tokens:
        return None
    prefixes = _strip_package_command_prefixes(tokens)
    if not prefixes.tokens:
        return None
    command = PurePosixPath(_shell_token_word(prefixes.tokens[0])).name
    return stripped if _is_known_evidence_command(command) else None


def _is_known_evidence_command(command: str) -> bool:
    return (
        command in _KNOWN_COMMANDS or _is_python_executable(command) or _is_pip_executable(command)
    )


def _is_python_executable(command: str) -> bool:
    return _PYTHON_EXECUTABLE_PATTERN.fullmatch(command) is not None


def _is_pip_executable(command: str) -> bool:
    return _PIP_EXECUTABLE_PATTERN.fullmatch(command) is not None


def _shell_tokens(command: str) -> list[str]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return []


from awf.service.supply_chain_policy_helpers import (  # noqa: E402
    _command_token_segments,
    _is_lockfile_path,
    _isoformat,
    _load_candidate,
    _load_open_candidate_for_workspace,
    _matches_any,
    _nested_dict,
    _normalized_unique_paths,
    _redact_command_excerpt,
    _severity,
)
