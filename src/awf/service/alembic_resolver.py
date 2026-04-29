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
import warnings
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.script.revision import ResolutionError, RevisionError


class AlembicResolveStatus(StrEnum):
    """Alembic resolver outcome."""

    unsupported = "unsupported"
    refused = "refused"
    not_needed = "not_needed"
    resolved = "resolved"


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
        loaded = _load_script_directory(repo_path)
        if loaded is None:
            return AlembicResolveResult(
                status=AlembicResolveStatus.unsupported,
                reason_code="ALEMBIC_NOT_CONFIGURED",
                heads=(),
                message="No alembic.ini was found at the target branch root.",
            )

        script, version_dir = loaded
        graph_result = _safe_heads(script)
        if isinstance(graph_result, AlembicResolveResult):
            return graph_result
        heads = graph_result

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


def _load_script_directory(repo_path: Path) -> tuple[ScriptDirectory, Path] | None:
    config_path = repo_path / "alembic.ini"
    if not config_path.is_file():
        return None

    config = Config(str(config_path))
    script_location = config.get_main_option("script_location")
    if not script_location:
        return None

    script_path = Path(script_location)
    if not script_path.is_absolute():
        script_path = repo_path / script_path
    config.set_main_option("script_location", str(script_path))

    script = ScriptDirectory.from_config(config)
    version_dir = Path(script.versions)
    if not version_dir.is_absolute():
        version_dir = script_path / version_dir
    version_dir.mkdir(parents=True, exist_ok=True)
    return script, version_dir


def _safe_heads(script: ScriptDirectory) -> tuple[str, ...] | AlembicResolveResult:
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            heads = tuple(sorted(script.get_heads()))
            tuple(script.walk_revisions())
    except SyntaxError as exc:
        return _refused_graph_result(
            reason_code="ALEMBIC_GRAPH_MALFORMED",
            message="Alembic revision graph contains malformed Python.",
            exc=exc,
        )
    except (KeyError, ResolutionError, RevisionError) as exc:
        return _refused_graph_result(
            reason_code="ALEMBIC_GRAPH_UNSAFE",
            message="Alembic revision graph is unsafe to merge automatically.",
            exc=exc,
        )
    except Exception as exc:
        return _refused_graph_result(
            reason_code="ALEMBIC_GRAPH_UNREADABLE",
            message="Alembic revision graph could not be read safely.",
            exc=exc,
        )

    duplicate_revisions = sorted(
        revision for revision, count in Counter(heads).items() if count > 1
    )
    if duplicate_revisions:
        return AlembicResolveResult(
            status=AlembicResolveStatus.refused,
            reason_code="ALEMBIC_GRAPH_UNSAFE",
            heads=heads,
            message="Alembic revision graph is unsafe to merge automatically.",
            details={"duplicate_revisions": duplicate_revisions},
        )

    unsafe_warnings = [
        str(warning.message)
        for warning in caught
        if _is_unsafe_alembic_warning(str(warning.message))
    ]
    if unsafe_warnings:
        return AlembicResolveResult(
            status=AlembicResolveStatus.refused,
            reason_code="ALEMBIC_GRAPH_UNSAFE",
            heads=heads,
            message="Alembic revision graph is unsafe to merge automatically.",
            details={"warnings": unsafe_warnings},
        )

    return heads


def _refused_graph_result(
    *,
    reason_code: str,
    message: str,
    exc: Exception,
) -> AlembicResolveResult:
    return AlembicResolveResult(
        status=AlembicResolveStatus.refused,
        reason_code=reason_code,
        heads=(),
        message=message,
        details={
            "error_type": type(exc).__name__,
            "error": str(exc),
        },
    )


def _is_unsafe_alembic_warning(message: str) -> bool:
    return "is present more than once" in message or "is not present" in message


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
