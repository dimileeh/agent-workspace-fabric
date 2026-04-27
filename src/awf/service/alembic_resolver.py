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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


class AlembicResolveStatus(StrEnum):
    """Alembic resolver outcome."""

    unsupported = "unsupported"
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
    message: str | None = None

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
            "message": self.message,
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
        try:
            heads = tuple(sorted(script.get_heads()))
        except Exception as exc:
            return AlembicResolveResult(
                status=AlembicResolveStatus.unsupported,
                reason_code="ALEMBIC_GRAPH_UNREADABLE",
                heads=(),
                message=str(exc),
            )

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
    now = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    digest = hashlib.sha1(",".join(sorted(heads)).encode("utf-8")).hexdigest()[:8]
    return _sanitize_revision_id(f"awf_{now}_{digest}")


_REVISION_ID_RE = re.compile(r"[^a-zA-Z0-9_]+")


def _sanitize_revision_id(value: str) -> str:
    return _REVISION_ID_RE.sub("_", value).strip("_")[:32]
