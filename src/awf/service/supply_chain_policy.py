"""Supply-chain guardrail evaluation for agent-authored workspace output."""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Final, Literal
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from awf.db.models import MergeCandidate, PolicyFinding, TaskAttempt, Workspace
from awf.db.repositories import (
    PolicyFindingCreate,
    PolicyFindingRepository,
    TaskAttemptRepository,
    WorkspaceEventCreate,
    WorkspaceRepository,
    owned_paths_overlap,
    sync_candidate_readiness,
)
from awf.profiles.models import ProfileSupplyChainPolicy

SUPPLY_CHAIN_UNPINNED_DEPENDENCY_INSTALL: Final[str] = (
    "SUPPLY_CHAIN_UNPINNED_DEPENDENCY_INSTALL"
)
SUPPLY_CHAIN_REMOTE_SCRIPT_EXECUTION: Final[str] = "SUPPLY_CHAIN_REMOTE_SCRIPT_EXECUTION"
SUPPLY_CHAIN_UNEXPECTED_REGISTRY_HOST: Final[str] = (
    "SUPPLY_CHAIN_UNEXPECTED_REGISTRY_HOST"
)
SUPPLY_CHAIN_LOCKFILE_OUTSIDE_OWNED_PATHS: Final[str] = (
    "SUPPLY_CHAIN_LOCKFILE_OUTSIDE_OWNED_PATHS"
)
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
    }
)
_REMOTE_SCRIPT_INTERPRETERS: Final[frozenset[str]] = frozenset(
    {"sh", "bash", "zsh", "dash", "fish", "python", "python3", "ruby", "perl", "node"}
)
_URL_CREDENTIAL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(https?://)[^/@\s]+(?::[^/@\s]+)?@"
)


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
        package_command = _package_command(command, tokens)
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
                explanation=(
                    f"Lockfile '{path}' changed outside declared owned_paths."
                ),
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
        owned_paths = tuple(workspace.owned_paths) if workspace.owned_paths else (
            tuple(attempt.owned_paths) if attempt is not None else ()
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


def _remote_script_execution(
    command: str,
    tokens: list[str],
    *,
    policy: ProfileSupplyChainPolicy,
) -> SupplyChainFinding | None:
    for index, token in enumerate(tokens):
        if token not in {"|", "|&"}:
            continue
        if not _is_remote_fetch(tokens[:index]):
            continue
        if not _pipe_target_is_interpreter(tokens[index + 1 :]):
            continue
        severity = _severity(policy.remote_script_execution.mode.value)
        return SupplyChainFinding(
            reason_code=SUPPLY_CHAIN_REMOTE_SCRIPT_EXECUTION,
            severity=severity,
            subject_path=None,
            explanation="Remote script execution piped into a shell was detected.",
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
    return None


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
    unexpected = [
        host for host in package_command.registry_hosts if host not in allowed_hosts
    ]
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


def _package_command(command: str, tokens: list[str]) -> _PackageCommand | None:
    del command
    if not tokens:
        return None
    if tokens[:3] in (["python", "-m", "pip"], ["python3", "-m", "pip"]):
        return _pip_command(tokens[3:], manager="pip")
    if len(tokens) >= 2 and tokens[0] == "uv" and tokens[1] == "pip":
        return _pip_command(tokens[2:], manager="uv pip")
    first = tokens[0]
    if first in {"pip", "pip3"}:
        return _pip_command(tokens[1:], manager="pip")
    if first in {"npm", "pnpm", "yarn", "bun"}:
        return _node_package_command(tokens[1:], manager=first)
    return None


def _pip_command(tokens: list[str], *, manager: str) -> _PackageCommand | None:
    if not tokens or tokens[0] != "install":
        return None
    args = tokens[1:]
    packages = tuple(_package_args(args, manager="pip"))
    registries = tuple(_registry_hosts(args, manager="pip"))
    return _PackageCommand(
        manager=manager,
        operation="install",
        package_specs=packages,
        registry_hosts=registries,
    )


def _node_package_command(tokens: list[str], *, manager: str) -> _PackageCommand | None:
    if not tokens:
        return None
    operation = tokens[0]
    install_ops = {"install", "i", "add"}
    if manager == "npm" and operation == "ci":
        return _PackageCommand(manager=manager, operation=operation, package_specs=(), registry_hosts=())
    if operation not in install_ops:
        return None
    args = tokens[1:]
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
        registry_hosts=tuple(_registry_hosts(args, manager=manager)),
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


def _registry_hosts(args: Sequence[str], *, manager: str) -> list[str]:
    hosts: list[str] = []
    flags = (
        {"--registry"}
        if manager in {"npm", "pnpm", "yarn", "bun"}
        else {"--index-url", "--extra-index-url", "-i"}
    )
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
        if value is None:
            continue
        host = _host_from_url(value)
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
            "-f",
            "--find-links",
            "--trusted-host",
            "-t",
            "--target",
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
    }


def _is_pinned_or_local_spec(manager: str, spec: str) -> bool:
    if _is_local_spec(spec):
        return True
    if manager in {"npm", "pnpm", "yarn", "bun"}:
        if spec.startswith("@"):
            return "@" in spec[1:]
        return "@" in spec or spec.startswith(("file:", "workspace:", "link:"))
    return (
        "==" in spec
        or "===" in spec
        or spec.startswith(("-r", "--requirement"))
        or spec.startswith(("file:", "git+file:"))
    )


def _is_local_spec(spec: str) -> bool:
    return spec in {".", "./"} or spec.startswith(("./", "../", "/", "file:"))


def _looks_like_requirements_file(spec: str) -> bool:
    return spec.endswith((".txt", ".in", ".lock")) or "/" in spec


def _has_any_flag(args: Sequence[str], flags: set[str]) -> bool:
    return any(arg in flags or any(arg.startswith(f"{flag}=") for flag in flags) for arg in args)


def _is_remote_fetch(tokens: Sequence[str]) -> bool:
    if not tokens:
        return False
    command = PurePosixPath(tokens[0]).name
    if command not in {"curl", "wget"}:
        return False
    return any(token.startswith(("http://", "https://")) for token in tokens[1:])


def _pipe_target_is_interpreter(tokens: Sequence[str]) -> bool:
    if not tokens:
        return False
    for token in tokens:
        if token in {"env", "sudo", "command"} or "=" in token:
            continue
        return PurePosixPath(token).name in _REMOTE_SCRIPT_INTERPRETERS
    return False


def _command_lines(command_evidence: str | Sequence[str]) -> list[str]:
    blobs = [command_evidence] if isinstance(command_evidence, str) else list(command_evidence)
    commands: list[str] = []
    for blob in blobs:
        for line in str(blob).splitlines():
            command = _command_from_line(line)
            if command:
                commands.append(command)
    return commands


def _command_from_line(line: str) -> str | None:
    stripped = line.strip()
    if not stripped:
        return None
    for prefix in ("$ ", "> ", "+ "):
        if stripped.startswith(prefix):
            return stripped[len(prefix) :].strip()
    lower = stripped.lower()
    for label in ("command:", "executed:", "shell:"):
        if lower.startswith(label):
            return stripped[len(label) :].strip()
    if lower.startswith("run "):
        return stripped[4:].strip()
    first = stripped.split(maxsplit=1)[0]
    return stripped if PurePosixPath(first).name in _KNOWN_COMMANDS else None


def _shell_tokens(command: str) -> list[str]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return []


def _severity(mode: str) -> PolicySeverity:
    return "blocking" if mode == "block" else "warning"


def _redact_command_excerpt(command: str) -> str:
    return _URL_CREDENTIAL_PATTERN.sub(r"\1[redacted]@", command).strip()[:300]


def _normalized_unique_paths(paths: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(path for raw in paths if (path := _normalize_path(raw))))


def _normalize_path(path: str) -> str:
    segments: list[str] = []
    for segment in path.strip().replace("\\", "/").split("/"):
        if segment in {"", "."}:
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    return "/".join(segments)


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    return any(owned_paths_overlap(pattern, path) for pattern in patterns)


def _is_lockfile_path(path: str) -> bool:
    name = PurePosixPath(path).name
    return name in _LOCKFILE_NAMES or name.endswith(".lock")


def _nested_dict(
    value: dict[str, object] | None,
    first: str,
    second: str | None = None,
) -> dict[str, object] | None:
    if value is None:
        return None
    first_value = value.get(first)
    if second is None:
        return first_value if isinstance(first_value, dict) else None
    if not isinstance(first_value, dict):
        return None
    second_value = first_value.get(second)
    return second_value if isinstance(second_value, dict) else None


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


async def _load_candidate(session: AsyncSession, candidate_id: str) -> MergeCandidate | None:
    stmt = (
        select(MergeCandidate)
        .where(MergeCandidate.id == candidate_id)
        .options(
            selectinload(MergeCandidate.attempt),
            selectinload(MergeCandidate.workspace).selectinload(Workspace.operations),
            selectinload(MergeCandidate.workspace).selectinload(Workspace.validation_runs),
            selectinload(MergeCandidate.workspace).selectinload(Workspace.policy_findings),
            selectinload(MergeCandidate.task),
        )
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _load_open_candidate_for_workspace(
    session: AsyncSession,
    workspace_id: str,
) -> MergeCandidate | None:
    stmt = (
        select(MergeCandidate)
        .where(
            MergeCandidate.workspace_id == workspace_id,
            MergeCandidate.status == "open",
        )
        .order_by(MergeCandidate.updated_at.desc(), MergeCandidate.id.desc())
        .options(
            selectinload(MergeCandidate.attempt),
            selectinload(MergeCandidate.workspace).selectinload(Workspace.operations),
            selectinload(MergeCandidate.workspace).selectinload(Workspace.validation_runs),
            selectinload(MergeCandidate.workspace).selectinload(Workspace.policy_findings),
            selectinload(MergeCandidate.task),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()
