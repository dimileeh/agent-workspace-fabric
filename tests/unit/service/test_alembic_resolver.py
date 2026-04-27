"""Alembic merge-head resolver tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from awf.service.alembic_resolver import (
    AlembicMergeResolver,
    AlembicResolveResult,
    AlembicResolveStatus,
    _render_merge_revision,
    _sanitize_revision_id,
)


def _write_alembic_ini(repo: Path) -> None:
    (repo / "migrations" / "versions").mkdir(parents=True)
    (repo / "alembic.ini").write_text(
        "[alembic]\n"
        "script_location = migrations\n"
        "prepend_sys_path = ./src\n"
        "version_path_separator = os\n",
        encoding="utf-8",
    )


def _write_revision(
    repo: Path,
    revision: str,
    down_revision: str | None,
    *,
    name: str | None = None,
) -> None:
    filename = f"{revision}_{name or revision}.py"
    down_revision_literal = "None" if down_revision is None else repr(down_revision)
    (repo / "migrations" / "versions" / filename).write_text(
        f'"""Revision {revision}."""\n\n'
        f'revision = "{revision}"\n'
        f"down_revision = {down_revision_literal}\n"
        "branch_labels = None\n"
        "depends_on = None\n\n"
        "def upgrade() -> None:\n"
        "    pass\n\n"
        "def downgrade() -> None:\n"
        "    pass\n",
        encoding="utf-8",
    )


def _heads(repo: Path) -> list[str]:
    config = Config(str(repo / "alembic.ini"))
    config.set_main_option("script_location", str(repo / "migrations"))
    return sorted(ScriptDirectory.from_config(config).get_heads())


@pytest.mark.unit
def test_resolver_generates_merge_revision_for_multiple_heads(tmp_path: Path) -> None:
    _write_alembic_ini(tmp_path)
    _write_revision(tmp_path, "base001", None)
    _write_revision(tmp_path, "left001", "base001")
    _write_revision(tmp_path, "right001", "base001")

    resolver = AlembicMergeResolver(revision_id_factory=lambda _heads: "merge001")
    result = resolver.resolve(tmp_path)

    assert result.status == AlembicResolveStatus.resolved
    assert result.reason_code == "ALEMBIC_HEADS_MERGED"
    assert result.heads == ("left001", "right001")
    assert result.generated_revision == "merge001"
    assert result.generated_path is not None

    generated = result.generated_path.read_text(encoding="utf-8")
    assert 'revision = "merge001"' in generated
    assert 'down_revision = ("left001", "right001")' in generated
    assert "def upgrade() -> None:" in generated
    assert _heads(tmp_path) == ["merge001"]


@pytest.mark.unit
def test_resolver_is_noop_when_graph_has_single_head(tmp_path: Path) -> None:
    _write_alembic_ini(tmp_path)
    _write_revision(tmp_path, "base001", None)
    _write_revision(tmp_path, "head001", "base001")

    result = AlembicMergeResolver(revision_id_factory=lambda _heads: "unused").resolve(tmp_path)

    assert result.status == AlembicResolveStatus.not_needed
    assert result.reason_code == "ALEMBIC_SINGLE_HEAD"
    assert result.heads == ("head001",)
    assert result.generated_path is None
    assert _heads(tmp_path) == ["head001"]


@pytest.mark.unit
def test_resolver_is_unsupported_without_alembic_config(tmp_path: Path) -> None:
    result = AlembicMergeResolver().resolve(tmp_path)

    assert result.status == AlembicResolveStatus.unsupported
    assert result.reason_code == "ALEMBIC_NOT_CONFIGURED"
    assert result.heads == ()
    assert result.generated_path is None


@pytest.mark.unit
def test_resolver_result_serializes_paths_and_change_status(tmp_path: Path) -> None:
    generated = tmp_path / "migrations" / "versions" / "merge.py"
    resolved = AlembicResolveResult(
        status=AlembicResolveStatus.resolved,
        reason_code="ALEMBIC_HEADS_MERGED",
        heads=("left", "right"),
        generated_revision="merge",
        generated_path=generated,
        message="merged",
    )
    unsupported = AlembicResolveResult(
        status=AlembicResolveStatus.unsupported,
        reason_code="ALEMBIC_NOT_CONFIGURED",
        heads=(),
    )

    assert resolved.changed is True
    assert resolved.to_dict() == {
        "status": "resolved",
        "reason_code": "ALEMBIC_HEADS_MERGED",
        "heads": ["left", "right"],
        "generated_revision": "merge",
        "generated_path": str(generated),
        "message": "merged",
    }
    assert unsupported.changed is False
    assert unsupported.to_dict()["generated_path"] is None


@pytest.mark.unit
def test_resolver_accepts_absolute_script_location_and_default_revision_id(
    tmp_path: Path,
) -> None:
    migrations = tmp_path / "absolute-migrations"
    (migrations / "versions").mkdir(parents=True)
    (tmp_path / "alembic.ini").write_text(
        "[alembic]\n"
        f"script_location = {migrations}\n"
        "prepend_sys_path = ./src\n"
        "version_path_separator = os\n",
        encoding="utf-8",
    )
    for revision, down_revision in [
        ("base001", None),
        ("left001", "base001"),
        ("right001", "base001"),
    ]:
        down_revision_literal = "None" if down_revision is None else repr(down_revision)
        (migrations / "versions" / f"{revision}.py").write_text(
            f'revision = "{revision}"\n'
            f"down_revision = {down_revision_literal}\n"
            "branch_labels = None\n"
            "depends_on = None\n",
            encoding="utf-8",
        )

    result = AlembicMergeResolver().resolve(tmp_path)

    assert result.status == AlembicResolveStatus.resolved
    assert result.generated_revision is not None
    assert result.generated_revision.startswith("awf_")
    assert len(result.generated_revision) <= 32
    assert result.generated_path is not None
    assert result.generated_path.parent == migrations / "versions"


@pytest.mark.unit
def test_resolver_is_unsupported_when_script_location_is_missing(tmp_path: Path) -> None:
    (tmp_path / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")

    result = AlembicMergeResolver().resolve(tmp_path)

    assert result.status == AlembicResolveStatus.unsupported
    assert result.reason_code == "ALEMBIC_NOT_CONFIGURED"
    assert result.message == "No alembic.ini was found at the target branch root."


@pytest.mark.unit
def test_resolver_reports_unreadable_graph_without_escaping(tmp_path: Path) -> None:
    _write_alembic_ini(tmp_path)
    (tmp_path / "migrations" / "versions" / "bad.py").write_text(
        "this is not valid python",
        encoding="utf-8",
    )

    result = AlembicMergeResolver().resolve(tmp_path)

    assert result.status == AlembicResolveStatus.unsupported
    assert result.reason_code == "ALEMBIC_GRAPH_UNREADABLE"
    assert result.heads == ()
    assert "invalid syntax" in str(result.message)


@pytest.mark.unit
def test_render_merge_revision_uses_tuple_syntax_for_one_head() -> None:
    rendered = _render_merge_revision(
        revision="merge001",
        heads=("head001",),
        created_at=datetime(2026, 4, 27, tzinfo=UTC),
    )

    assert 'down_revision = ("head001",)' in rendered
    assert 'revision = "merge001"' in rendered


@pytest.mark.unit
def test_sanitize_revision_id_strips_invalid_characters_and_truncates() -> None:
    sanitized = _sanitize_revision_id("---bad revision/id.with.symbols." + "x" * 50)

    assert sanitized == "bad_revision_id_with_symbols_xxx"
    assert len(sanitized) == 32
