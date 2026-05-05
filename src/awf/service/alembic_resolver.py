"""Python workload resolver for merged Alembic revision heads.

When several AWF workspaces land migration PRs against the same target branch,
each PR can be valid in isolation while the integrated branch ends up with
multiple Alembic heads. That is not a normal source conflict, so Git cannot
catch it. This module provides the dedicated Python resolver the target-branch
monitor can run after merges: inspect the Alembic graph and, when needed,
write an empty merge revision whose ``down_revision`` points at every head.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.script.base import Script
from alembic.script.revision import ResolutionError, RevisionError, RevisionMap


class AlembicResolveStatus(StrEnum):
    """Alembic resolver outcome."""

    unsupported = "unsupported"
    refused = "refused"
    not_needed = "not_needed"
    resolved = "resolved"


class AlembicGraphValidationStatus(StrEnum):
    """Non-mutating Alembic revision graph validation outcome."""

    unsupported = "unsupported"
    failed = "failed"
    passed = "passed"


@dataclass(frozen=True)
class AlembicGraphFinding:
    """One structured Alembic graph validation finding."""

    reason_code: str
    message: str
    details: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "reason_code": self.reason_code,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class AlembicGraphValidationResult:
    """Structured result from a non-mutating Alembic graph inspection."""

    status: AlembicGraphValidationStatus
    reason_code: str
    heads: tuple[str, ...]
    message: str | None = None
    details: Mapping[str, object] | None = None
    findings: tuple[AlembicGraphFinding, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == AlembicGraphValidationStatus.passed

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "reason_code": self.reason_code,
            "heads": list(self.heads),
            "message": self.message,
            "details": dict(self.details or {}),
            "findings": [finding.to_dict() for finding in self.findings],
        }


@dataclass(frozen=True)
class AlembicResolveResult:
    """Structured result from one Alembic graph reconciliation pass."""

    status: AlembicResolveStatus
    reason_code: str
    heads: tuple[str, ...]
    generated_revision: str | None = None
    generated_path: Path | None = None
    generated_path_relative: str | None = None
    message: str | None = None
    details: Mapping[str, object] | None = None

    @property
    def changed(self) -> bool:
        return self.status == AlembicResolveStatus.resolved and self.generated_path is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "reason_code": self.reason_code,
            "heads": list(self.heads),
            "generated_revision": self.generated_revision,
            "generated_path": str(self.generated_path) if self.generated_path is not None else None,
            "generated_path_relative": self.generated_path_relative,
            "message": self.message,
            "details": dict(self.details or {}),
        }


RevisionIdFactory = Callable[[Sequence[str]], str]
RevisionReferenceType = Literal["down_revision", "dependencies"]
_DETAIL_KEY_COLLISIONS_KEY = "detail_key_collisions"
_FINDING_DETAILS_KEY = "finding_details"


class AlembicMergeResolver:
    """Resolve an Alembic multi-head graph by generating a merge revision."""

    def __init__(
        self,
        *,
        revision_id_factory: RevisionIdFactory | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._revision_id_factory = revision_id_factory or _default_revision_id
        self._now = now or (lambda: datetime.now(UTC))

    def resolve(self, repo_path: Path) -> AlembicResolveResult:
        """Inspect ``repo_path`` and write a merge revision when it has >1 heads."""

        repo_path = repo_path.expanduser().resolve()
        loaded = _load_script_directory(repo_path, ensure_version_dir=True)
        if loaded is None:
            return AlembicResolveResult(
                status=AlembicResolveStatus.unsupported,
                reason_code="ALEMBIC_NOT_CONFIGURED",
                heads=(),
                message="No alembic.ini was found at the target branch root.",
            )

        script, version_dir = loaded
        graph_result = _validate_script_directory(script)
        if (
            graph_result.status == AlembicGraphValidationStatus.failed
            and graph_result.reason_code != "ALEMBIC_MULTIPLE_HEADS"
        ):
            return _resolve_result_from_graph_validation(graph_result)
        if graph_result.status == AlembicGraphValidationStatus.unsupported:
            return _resolve_result_from_graph_validation(graph_result)
        heads = graph_result.heads

        if len(heads) <= 1:
            return AlembicResolveResult(
                status=AlembicResolveStatus.not_needed,
                reason_code="ALEMBIC_SINGLE_HEAD",
                heads=heads,
                message="Alembic revision graph already has a single head.",
            )

        revision = self._revision_id_factory(heads)
        generated_path = version_dir / f"{revision}_merge_alembic_heads.py"
        generated_path.write_text(
            _render_merge_revision(
                revision=revision,
                heads=heads,
                created_at=self._now(),
            ),
            encoding="utf-8",
        )
        return AlembicResolveResult(
            status=AlembicResolveStatus.resolved,
            reason_code="ALEMBIC_HEADS_MERGED",
            heads=heads,
            generated_revision=revision,
            generated_path=generated_path,
            generated_path_relative=_relative_path(generated_path, repo_path),
            message=f"Generated Alembic merge revision for {len(heads)} heads.",
        )


def validate_alembic_migration_graph(
    repo_path: Path,
    *,
    config_path: str = "alembic.ini",
    script_location: str | None = None,
) -> AlembicGraphValidationResult:
    """Inspect an Alembic revision graph without mutating the checkout."""

    repo_path = repo_path.expanduser().resolve()
    loaded = _load_script_directory(
        repo_path,
        config_path=config_path,
        script_location=script_location,
        ensure_version_dir=False,
    )
    if loaded is None:
        return AlembicGraphValidationResult(
            status=AlembicGraphValidationStatus.unsupported,
            reason_code="ALEMBIC_NOT_CONFIGURED",
            heads=(),
            message="No Alembic configuration was found at the repository root.",
        )
    script, _version_dir = loaded
    return _validate_script_directory(script)


def _load_script_directory(
    repo_path: Path,
    *,
    config_path: str = "alembic.ini",
    script_location: str | None = None,
    ensure_version_dir: bool = True,
) -> tuple[ScriptDirectory, Path] | None:
    config_file = Path(config_path)
    if not config_file.is_absolute():
        config_file = repo_path / config_file
    if not config_file.is_file():
        return None

    config = Config(str(config_file))
    _ensure_alembic_path_separator(config)
    resolved_script_location = script_location or config.get_main_option("script_location")
    if not resolved_script_location:
        return None

    script_path = Path(resolved_script_location)
    if not script_path.is_absolute():
        script_path = repo_path / script_path
    config.set_main_option("script_location", str(script_path))

    script = ScriptDirectory.from_config(config)
    version_dir = Path(script.versions)
    if not version_dir.is_absolute():
        version_dir = script_path / version_dir
    if ensure_version_dir:
        version_dir.mkdir(parents=True, exist_ok=True)
    return script, version_dir


def _ensure_alembic_path_separator(config: Config) -> None:
    if config.get_main_option("path_separator") is None:
        config.set_main_option("path_separator", "os")


def _safe_heads(script: ScriptDirectory) -> tuple[str, ...] | AlembicResolveResult:
    result = _validate_script_directory(script)
    if result.status == AlembicGraphValidationStatus.passed:
        return result.heads
    return _resolve_result_from_graph_validation(result)


def _validate_script_directory(script: ScriptDirectory) -> AlembicGraphValidationResult:
    try:
        # Preflight the loaded scripts directly so duplicate/missing graph
        # detection does not depend on Alembic warning message text.
        revisions = tuple(script._load_revisions())
        graph_result = _unsafe_loaded_revision_graph(revisions)
        if graph_result is not None:
            return graph_result

        revision_map = RevisionMap(lambda: revisions)
        heads = tuple(sorted(revision_map.heads))
        tuple(
            revision_map.iterate_revisions(
                "heads",
                "base",
                inclusive=True,
                assert_relative_length=False,
            )
        )
    except SyntaxError as exc:
        return _failed_graph_exception_result(
            reason_code="ALEMBIC_GRAPH_MALFORMED",
            message="Alembic revision graph contains malformed Python.",
            exc=exc,
        )
    except (KeyError, ResolutionError, RevisionError) as exc:
        return _failed_graph_exception_result(
            reason_code="ALEMBIC_GRAPH_UNSAFE",
            message="Alembic revision graph is unsafe to merge automatically.",
            exc=exc,
        )
    except Exception as exc:
        return _failed_graph_exception_result(
            reason_code="ALEMBIC_GRAPH_UNREADABLE",
            message="Alembic revision graph could not be read safely.",
            exc=exc,
        )

    if len(heads) > 1:
        details: dict[str, object] = {"heads": list(heads)}
        return _failed_graph_result(
            reason_code="ALEMBIC_MULTIPLE_HEADS",
            message="Alembic revision graph contains multiple heads.",
            heads=heads,
            details=details,
        )

    return AlembicGraphValidationResult(
        status=AlembicGraphValidationStatus.passed,
        reason_code="ALEMBIC_GRAPH_OK",
        heads=heads,
        message="Alembic revision graph has a single head.",
    )


def _unsafe_loaded_revision_graph(
    revisions: Sequence[Script],
) -> AlembicGraphValidationResult | None:
    revision_ids = tuple(revision.revision for revision in revisions)
    revision_id_set = set(revision_ids)
    duplicate_revisions = sorted(
        revision for revision, count in Counter(revision_ids).items() if count > 1
    )
    branch_label_owners = _branch_label_owners(revisions)
    duplicate_branch_labels = {
        label: owners for label, owners in sorted(branch_label_owners.items()) if len(owners) > 1
    }
    missing_down_revisions, ambiguous_down_revisions = _revision_reference_anomalies(
        revisions=revisions,
        revision_ids=revision_id_set,
        branch_label_owners=branch_label_owners,
        reference_type="down_revision",
    )
    missing_dependencies, ambiguous_dependencies = _revision_reference_anomalies(
        revisions=revisions,
        revision_ids=revision_id_set,
        branch_label_owners=branch_label_owners,
        reference_type="dependencies",
    )
    branch_label_details: dict[str, object] = {}
    if duplicate_branch_labels:
        branch_label_details["duplicate_branch_labels"] = duplicate_branch_labels
    if ambiguous_down_revisions:
        branch_label_details["ambiguous_down_revisions"] = ambiguous_down_revisions
    if ambiguous_dependencies:
        branch_label_details["ambiguous_dependencies"] = ambiguous_dependencies

    findings: list[AlembicGraphFinding] = []
    if duplicate_revisions:
        findings.append(
            AlembicGraphFinding(
                reason_code="ALEMBIC_DUPLICATE_REVISION",
                message="Alembic revision graph contains duplicate revision ids.",
                details={"duplicate_revisions": duplicate_revisions},
            )
        )
    if branch_label_details:
        findings.append(
            AlembicGraphFinding(
                reason_code="ALEMBIC_BRANCH_LABEL_ANOMALY",
                message="Alembic revision graph contains ambiguous branch labels.",
                details=branch_label_details,
            )
        )
    if missing_down_revisions:
        findings.append(
            AlembicGraphFinding(
                reason_code="ALEMBIC_MISSING_DOWN_REVISION",
                message="Alembic revision graph references missing down revisions.",
                details={"missing_down_revisions": missing_down_revisions},
            )
        )
    if missing_dependencies:
        findings.append(
            AlembicGraphFinding(
                reason_code="ALEMBIC_MISSING_DEPENDENCY",
                message="Alembic revision graph references missing dependencies.",
                details={"missing_dependencies": missing_dependencies},
            )
        )

    if findings:
        details = _merge_finding_details(findings)
        first = findings[0]
        return AlembicGraphValidationResult(
            status=AlembicGraphValidationStatus.failed,
            reason_code=first.reason_code,
            heads=_loaded_revision_heads(revisions),
            message=first.message,
            details=details,
            findings=tuple(findings),
        )

    return None


def _merge_finding_details(findings: Sequence[AlembicGraphFinding]) -> dict[str, object]:
    details: dict[str, object] = {}
    detail_owners: dict[str, list[int]] = {}
    for index, finding in enumerate(findings):
        for key, value in finding.details.items():
            detail_owners.setdefault(key, []).append(index)
            details.setdefault(key, value)

    colliding_keys = {key for key, owners in detail_owners.items() if len(owners) > 1}
    if not colliding_keys:
        return details

    generated_keys = {_DETAIL_KEY_COLLISIONS_KEY, _FINDING_DETAILS_KEY}
    ambiguous_keys = colliding_keys | (generated_keys & detail_owners.keys())
    for key in ambiguous_keys:
        details.pop(key, None)
    details[_DETAIL_KEY_COLLISIONS_KEY] = sorted(ambiguous_keys)
    details[_FINDING_DETAILS_KEY] = [
        {
            "reason_code": finding.reason_code,
            "details": dict(finding.details),
        }
        for finding in findings
    ]
    return details


def _branch_label_owners(revisions: Sequence[Script]) -> dict[str, list[str]]:
    owners: dict[str, list[str]] = {}
    for revision in revisions:
        for branch_label in _as_revision_tuple(revision.branch_labels):
            owners.setdefault(branch_label, []).append(revision.revision)
    return {label: sorted(revision_ids) for label, revision_ids in owners.items()}


def _revision_reference_anomalies(
    *,
    revisions: Sequence[Script],
    revision_ids: set[str],
    branch_label_owners: Mapping[str, Sequence[str]],
    reference_type: RevisionReferenceType,
) -> tuple[list[str], dict[str, list[str]]]:
    missing: set[str] = set()
    ambiguous: dict[str, list[str]] = {}
    for revision in revisions:
        for reference in _revision_references(revision, reference_type=reference_type):
            if reference in revision_ids:
                continue
            label_owners = branch_label_owners.get(reference)
            if label_owners is None:
                missing.add(reference)
                continue
            if len(label_owners) > 1:
                ambiguous[reference] = sorted(label_owners)

    return sorted(missing), dict(sorted(ambiguous.items()))


def _loaded_revision_heads(revisions: Sequence[Script]) -> tuple[str, ...]:
    branch_label_targets = {
        branch_label: owners[0]
        for branch_label, owners in _branch_label_owners(revisions).items()
        if len(owners) == 1
    }
    referenced = {
        branch_label_targets.get(reference, reference)
        for revision in revisions
        for reference in _revision_references(revision, reference_type="down_revision")
    }
    return tuple(
        sorted(revision.revision for revision in revisions if revision.revision not in referenced)
    )


def _revision_references(
    revision: Script,
    *,
    reference_type: RevisionReferenceType,
) -> tuple[str, ...]:
    if reference_type == "down_revision":
        return _as_revision_tuple(revision.down_revision)
    return _as_revision_tuple(revision.dependencies)


def _as_revision_tuple(value: str | Iterable[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def _failed_graph_result(
    *,
    reason_code: str,
    message: str,
    heads: tuple[str, ...],
    details: Mapping[str, object] | None = None,
) -> AlembicGraphValidationResult:
    finding = AlembicGraphFinding(
        reason_code=reason_code,
        message=message,
        details=dict(details or {}),
    )
    return AlembicGraphValidationResult(
        status=AlembicGraphValidationStatus.failed,
        reason_code=reason_code,
        heads=heads,
        message=message,
        details=dict(details or {}),
        findings=(finding,),
    )


def _failed_graph_exception_result(
    *,
    reason_code: str,
    message: str,
    exc: Exception,
) -> AlembicGraphValidationResult:
    return _failed_graph_result(
        reason_code=reason_code,
        heads=(),
        message=message,
        details={
            "error_type": type(exc).__name__,
            "error": str(exc),
        },
    )


def _resolve_result_from_graph_validation(
    result: AlembicGraphValidationResult,
) -> AlembicResolveResult:
    status = (
        AlembicResolveStatus.unsupported
        if result.status == AlembicGraphValidationStatus.unsupported
        else AlembicResolveStatus.refused
    )
    return AlembicResolveResult(
        status=status,
        reason_code=result.reason_code,
        heads=result.heads,
        message=result.message,
        details=result.details,
    )


def _relative_path(path: Path, root: Path) -> str | None:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return None


def _render_merge_revision(
    *,
    revision: str,
    heads: Sequence[str],
    created_at: datetime,
) -> str:
    heads_literal = "(" + ", ".join(json.dumps(head) for head in heads) + ")"
    if len(heads) == 1:
        heads_literal = f"({json.dumps(heads[0])},)"
    timestamp = created_at.astimezone(UTC).isoformat()
    return (
        '"""Merge Alembic heads after AWF target-branch integration.\n\n'
        f"Generated by AWF at {timestamp}.\n"
        '"""\n\n'
        f'revision = "{revision}"\n'
        f"down_revision = {heads_literal}\n"
        "branch_labels = None\n"
        "depends_on = None\n\n\n"
        "def upgrade() -> None:\n"
        "    pass\n\n\n"
        "def downgrade() -> None:\n"
        "    pass\n"
    )


def _default_revision_id(heads: Sequence[str]) -> str:
    digest = hashlib.sha1(",".join(sorted(heads)).encode("utf-8")).hexdigest()[:12]
    return _sanitize_revision_id(f"awf_merge_{digest}")


_REVISION_ID_RE = re.compile(r"[^a-zA-Z0-9_]+")


def _sanitize_revision_id(value: str) -> str:
    return _REVISION_ID_RE.sub("_", value).strip("_")[:32]
