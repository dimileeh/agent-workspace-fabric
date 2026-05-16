"""Alembic merge-head resolver tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import util as alembic_util
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.script.revision import RevisionError

import awf.service.alembic_resolver as alembic_resolver
from awf.service.alembic_resolver import (
    AlembicGraphFinding,
    AlembicGraphValidationResult,
    AlembicGraphValidationStatus,
    AlembicMergeResolver,
    AlembicResolveResult,
    AlembicResolveStatus,
    _as_revision_tuple,
    _merge_finding_details,
    _relative_path,
    _render_merge_revision,
    _revision_reference_anomalies,
    _safe_heads,
    _sanitize_revision_id,
    validate_alembic_migration_graph,
)


def _write_alembic_ini(repo: Path) -> None:
    (repo / "migrations" / "versions").mkdir(parents=True)
    (repo / "alembic.ini").write_text(
        "[alembic]\n"
        "script_location = migrations\n"
        "prepend_sys_path = ./src\n"
        "path_separator = os\n"
        "version_path_separator = os\n",
        encoding="utf-8",
    )


def _write_revision(
    repo: Path,
    revision: str,
    down_revision: str | tuple[str, ...] | None,
    *,
    name: str | None = None,
    branch_labels: str | tuple[str, ...] | None = None,
    depends_on: str | tuple[str, ...] | None = None,
) -> None:
    filename = f"{revision}_{name or revision}.py"
    down_revision_literal = "None" if down_revision is None else repr(down_revision)
    branch_labels_literal = "None" if branch_labels is None else repr(branch_labels)
    depends_on_literal = "None" if depends_on is None else repr(depends_on)
    (repo / "migrations" / "versions" / filename).write_text(
        f'"""Revision {revision}."""\n\n'
        f'revision = "{revision}"\n'
        f"down_revision = {down_revision_literal}\n"
        f"branch_labels = {branch_labels_literal}\n"
        f"depends_on = {depends_on_literal}\n\n"
        "def upgrade() -> None:\n"
        "    pass\n\n"
        "def downgrade() -> None:\n"
        "    pass\n",
        encoding="utf-8",
    )


def _heads(repo: Path) -> list[str]:
    config = Config(str(repo / "alembic.ini"))
    config.set_main_option("path_separator", "os")
    config.set_main_option("script_location", str(repo / "migrations"))
    return sorted(ScriptDirectory.from_config(config).get_heads())


@pytest.mark.unit
def test_alembic_graph_validation_detects_multiple_heads(tmp_path: Path) -> None:
    _write_alembic_ini(tmp_path)
    _write_revision(tmp_path, "base001", None)
    _write_revision(tmp_path, "left001", "base001")
    _write_revision(tmp_path, "right001", "base001")

    result = validate_alembic_migration_graph(tmp_path)

    assert result.status == AlembicGraphValidationStatus.failed
    assert result.reason_code == "ALEMBIC_MULTIPLE_HEADS"
    assert result.heads == ("left001", "right001")
    assert result.findings[0].reason_code == "ALEMBIC_MULTIPLE_HEADS"
    assert result.to_dict()["findings"][0]["details"]["heads"] == ["left001", "right001"]
    assert not (tmp_path / "migrations" / "versions" / "merge001_merge_alembic_heads.py").exists()


@pytest.mark.unit
def test_alembic_graph_validation_detects_missing_down_revision(tmp_path: Path) -> None:
    _write_alembic_ini(tmp_path)
    _write_revision(tmp_path, "orphan001", "missing001")

    result = validate_alembic_migration_graph(tmp_path)

    assert result.status == AlembicGraphValidationStatus.failed
    assert result.reason_code == "ALEMBIC_MISSING_DOWN_REVISION"
    assert result.heads == ("orphan001",)
    assert result.to_dict()["details"]["missing_down_revisions"] == ["missing001"]
    assert result.findings[0].details["missing_down_revisions"] == ["missing001"]


@pytest.mark.unit
def test_alembic_graph_validation_detects_duplicate_revision_ids(tmp_path: Path) -> None:
    _write_alembic_ini(tmp_path)
    _write_revision(tmp_path, "dup001", None, name="left")
    _write_revision(tmp_path, "dup001", None, name="right")

    result = validate_alembic_migration_graph(tmp_path)

    assert result.status == AlembicGraphValidationStatus.failed
    assert result.reason_code == "ALEMBIC_DUPLICATE_REVISION"
    assert result.heads == ("dup001", "dup001")
    assert result.to_dict()["details"]["duplicate_revisions"] == ["dup001"]
    assert result.findings[0].details["duplicate_revisions"] == ["dup001"]


@pytest.mark.unit
def test_alembic_graph_validation_detects_branch_label_anomaly(tmp_path: Path) -> None:
    _write_alembic_ini(tmp_path)
    _write_revision(tmp_path, "base001", None, branch_labels="shared")
    _write_revision(tmp_path, "other001", None, branch_labels=("shared",))

    result = validate_alembic_migration_graph(tmp_path)

    assert result.status == AlembicGraphValidationStatus.failed
    assert result.reason_code == "ALEMBIC_BRANCH_LABEL_ANOMALY"
    assert result.to_dict()["details"]["duplicate_branch_labels"] == {
        "shared": ["base001", "other001"]
    }
    assert result.findings[0].reason_code == "ALEMBIC_BRANCH_LABEL_ANOMALY"


@pytest.mark.unit
def test_alembic_graph_validation_reports_ambiguous_branch_label_references(
    tmp_path: Path,
) -> None:
    _write_alembic_ini(tmp_path)
    _write_revision(tmp_path, "base001", None, branch_labels="shared")
    _write_revision(tmp_path, "other001", None, branch_labels="shared")
    _write_revision(tmp_path, "head001", "shared", depends_on="shared")

    result = validate_alembic_migration_graph(tmp_path)

    assert result.status == AlembicGraphValidationStatus.failed
    assert result.reason_code == "ALEMBIC_BRANCH_LABEL_ANOMALY"
    assert result.to_dict()["details"]["ambiguous_down_revisions"] == {
        "shared": ["base001", "other001"]
    }
    assert result.to_dict()["details"]["ambiguous_dependencies"] == {
        "shared": ["base001", "other001"]
    }


@pytest.mark.unit
def test_merge_finding_details_preserves_colliding_keys_by_finding() -> None:
    findings = (
        AlembicGraphFinding(
            reason_code="ALEMBIC_FIRST",
            message="first finding",
            details={"shared": ["first"], "first_only": True},
        ),
        AlembicGraphFinding(
            reason_code="ALEMBIC_SECOND",
            message="second finding",
            details={"shared": ["second"], "second_only": True},
        ),
    )

    details = _merge_finding_details(findings)

    assert details["first_only"] is True
    assert details["second_only"] is True
    assert "shared" not in details
    assert details["detail_key_collisions"] == ["shared"]
    assert details["finding_details"] == [
        {
            "reason_code": "ALEMBIC_FIRST",
            "details": {"shared": ["first"], "first_only": True},
        },
        {
            "reason_code": "ALEMBIC_SECOND",
            "details": {"shared": ["second"], "second_only": True},
        },
    ]


@pytest.mark.unit
def test_alembic_graph_validation_accepts_clean_single_head(tmp_path: Path) -> None:
    _write_alembic_ini(tmp_path)
    _write_revision(tmp_path, "base001", None)
    _write_revision(tmp_path, "head001", "base001")

    result = validate_alembic_migration_graph(tmp_path)

    assert result.status == AlembicGraphValidationStatus.passed
    assert result.reason_code == "ALEMBIC_GRAPH_OK"
    assert result.heads == ("head001",)
    assert result.findings == ()
    assert result.to_dict()["details"] == {}
    assert result.ok is True


@pytest.mark.unit
def test_revision_reference_anomalies_accept_single_owner_branch_label() -> None:
    revisions = (
        SimpleNamespace(revision="base001", down_revision=None, dependencies=None),
        SimpleNamespace(revision="head001", down_revision="lineage", dependencies="lineage"),
    )

    missing_down, ambiguous_down = _revision_reference_anomalies(  # type: ignore[arg-type]
        revisions=revisions,
        revision_ids={"base001", "head001"},
        branch_label_owners={"lineage": ["base001"]},
        reference_type="down_revision",
    )
    missing_deps, ambiguous_deps = _revision_reference_anomalies(  # type: ignore[arg-type]
        revisions=revisions,
        revision_ids={"base001", "head001"},
        branch_label_owners={"lineage": ["base001"]},
        reference_type="dependencies",
    )

    assert missing_down == []
    assert ambiguous_down == {}
    assert missing_deps == []
    assert ambiguous_deps == {}


@pytest.mark.unit
def test_alembic_graph_validation_accepts_absolute_config_and_script_override(
    tmp_path: Path,
) -> None:
    migrations = tmp_path / "db" / "migrations"
    (migrations / "versions").mkdir(parents=True)
    config_path = tmp_path / "config" / "alembic.ini"
    config_path.parent.mkdir()
    config_path.write_text(
        "[alembic]\nscript_location = ignored\npath_separator = os\nversion_path_separator = os\n",
        encoding="utf-8",
    )
    (migrations / "versions" / "base001.py").write_text(
        'revision = "base001"\ndown_revision = None\nbranch_labels = None\ndepends_on = None\n',
        encoding="utf-8",
    )

    result = validate_alembic_migration_graph(
        tmp_path,
        config_path=str(config_path),
        script_location="db/migrations",
    )

    assert result.status == AlembicGraphValidationStatus.passed
    assert result.heads == ("base001",)


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
        generated_path_relative="migrations/versions/merge.py",
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
        "generated_path_relative": "migrations/versions/merge.py",
        "message": "merged",
        "details": {},
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
        "path_separator = os\n"
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
def test_resolver_converts_shared_inspector_unsupported_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_alembic_ini(tmp_path)

    monkeypatch.setattr(
        alembic_resolver,
        "_load_script_directory",
        lambda *_args, **_kwargs: (object(), tmp_path / "migrations" / "versions"),
    )
    monkeypatch.setattr(
        alembic_resolver,
        "_validate_script_directory",
        lambda _script: AlembicGraphValidationResult(
            status=AlembicGraphValidationStatus.unsupported,
            reason_code="ALEMBIC_NOT_CONFIGURED",
            heads=(),
            message="not configured",
        ),
    )

    result = AlembicMergeResolver().resolve(tmp_path)

    assert result.status == AlembicResolveStatus.unsupported
    assert result.reason_code == "ALEMBIC_NOT_CONFIGURED"
    assert result.message == "not configured"


@pytest.mark.unit
def test_resolver_reports_unreadable_graph_without_escaping(tmp_path: Path) -> None:
    _write_alembic_ini(tmp_path)
    (tmp_path / "migrations" / "versions" / "bad.py").write_text(
        "this is not valid python",
        encoding="utf-8",
    )

    result = AlembicMergeResolver().resolve(tmp_path)

    assert result.status == AlembicResolveStatus.refused
    assert result.reason_code == "ALEMBIC_GRAPH_MALFORMED"
    assert result.heads == ()
    assert result.generated_path is None
    assert result.to_dict()["details"]["error_type"] == "SyntaxError"


@pytest.mark.unit
def test_resolver_refuses_missing_down_revision_without_mutating(tmp_path: Path) -> None:
    _write_alembic_ini(tmp_path)
    _write_revision(tmp_path, "orphan001", "missing001")

    result = AlembicMergeResolver(revision_id_factory=lambda _heads: "merge001").resolve(tmp_path)

    assert result.status == AlembicResolveStatus.refused
    assert result.reason_code == "ALEMBIC_MISSING_DOWN_REVISION"
    assert result.generated_path is None
    assert not (tmp_path / "migrations" / "versions" / "merge001_merge_alembic_heads.py").exists()
    assert result.to_dict()["details"]["missing_down_revisions"] == ["missing001"]


@pytest.mark.unit
def test_resolver_refuses_missing_dependency_without_mutating(tmp_path: Path) -> None:
    _write_alembic_ini(tmp_path)
    _write_revision(tmp_path, "base001", None)
    (tmp_path / "migrations" / "versions" / "headdep001.py").write_text(
        'revision = "headdep001"\n'
        'down_revision = "base001"\n'
        "branch_labels = None\n"
        'depends_on = "missingdep001"\n',
        encoding="utf-8",
    )

    result = AlembicMergeResolver(revision_id_factory=lambda _heads: "merge001").resolve(tmp_path)

    assert result.status == AlembicResolveStatus.refused
    assert result.reason_code == "ALEMBIC_MISSING_DEPENDENCY"
    assert result.generated_path is None
    assert not (tmp_path / "migrations" / "versions" / "merge001_merge_alembic_heads.py").exists()
    assert result.to_dict()["details"]["missing_dependencies"] == ["missingdep001"]


@pytest.mark.unit
def test_resolver_refuses_duplicate_revision_ids_without_mutating(tmp_path: Path) -> None:
    _write_alembic_ini(tmp_path)
    _write_revision(tmp_path, "dup001", None, name="left")
    _write_revision(tmp_path, "dup001", None, name="right")

    result = AlembicMergeResolver(revision_id_factory=lambda _heads: "merge001").resolve(tmp_path)

    assert result.status == AlembicResolveStatus.refused
    assert result.reason_code == "ALEMBIC_DUPLICATE_REVISION"
    assert result.heads == ("dup001", "dup001")
    assert result.generated_path is None
    assert not (tmp_path / "migrations" / "versions" / "merge001_merge_alembic_heads.py").exists()
    assert result.to_dict()["details"]["duplicate_revisions"] == ["dup001"]


@pytest.mark.unit
def test_resolver_refuses_duplicate_non_head_revision_ids_without_warning_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_alembic_ini(tmp_path)
    _write_revision(tmp_path, "dup001", None, name="left")
    _write_revision(tmp_path, "dup001", None, name="right")
    _write_revision(tmp_path, "head001", "dup001")

    original_warn = alembic_util.warn

    def rewrite_duplicate_warning(
        message: str,
        stacklevel: int = 2,
    ) -> None:
        if "present more than once" in str(message):
            original_warn("duplicate revision id detected by Alembic", stacklevel=stacklevel)
            return
        original_warn(message, stacklevel=stacklevel)

    monkeypatch.setattr(alembic_util, "warn", rewrite_duplicate_warning)

    result = AlembicMergeResolver(revision_id_factory=lambda _heads: "merge001").resolve(tmp_path)

    assert result.status == AlembicResolveStatus.refused
    assert result.reason_code == "ALEMBIC_DUPLICATE_REVISION"
    assert result.heads == ("head001",)
    assert result.generated_path is None
    assert not (tmp_path / "migrations" / "versions" / "merge001_merge_alembic_heads.py").exists()
    assert result.to_dict()["details"]["duplicate_revisions"] == ["dup001"]


@pytest.mark.unit
def test_resolver_refuses_missing_tuple_dependencies_without_mutating(tmp_path: Path) -> None:
    _write_alembic_ini(tmp_path)
    _write_revision(tmp_path, "base001", None)
    (tmp_path / "migrations" / "versions" / "head001.py").write_text(
        'revision = "head001"\n'
        'down_revision = "base001"\n'
        "branch_labels = None\n"
        'depends_on = ("missing_dep", "other_missing_dep")\n',
        encoding="utf-8",
    )

    result = AlembicMergeResolver(revision_id_factory=lambda _heads: "merge001").resolve(tmp_path)

    assert result.status == AlembicResolveStatus.refused
    assert result.reason_code == "ALEMBIC_MISSING_DEPENDENCY"
    assert result.generated_path is None
    assert not (tmp_path / "migrations" / "versions" / "merge001_merge_alembic_heads.py").exists()
    assert result.to_dict()["details"]["missing_dependencies"] == [
        "missing_dep",
        "other_missing_dep",
    ]


@pytest.mark.unit
def test_resolver_refuses_branch_label_anomaly_without_mutating(tmp_path: Path) -> None:
    _write_alembic_ini(tmp_path)
    _write_revision(tmp_path, "base001", None, branch_labels="shared")
    _write_revision(tmp_path, "other001", None, branch_labels="shared")

    result = AlembicMergeResolver(revision_id_factory=lambda _heads: "merge001").resolve(tmp_path)

    assert result.status == AlembicResolveStatus.refused
    assert result.reason_code == "ALEMBIC_BRANCH_LABEL_ANOMALY"
    assert result.generated_path is None
    assert not (tmp_path / "migrations" / "versions" / "merge001_merge_alembic_heads.py").exists()
    assert result.to_dict()["details"]["duplicate_branch_labels"] == {
        "shared": ["base001", "other001"]
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("exc", "reason_code", "message"),
    [
        (
            RevisionError("duplicate revision"),
            "ALEMBIC_GRAPH_UNSAFE",
            "Alembic revision graph is unsafe to merge automatically.",
        ),
        (
            RuntimeError("graph locked"),
            "ALEMBIC_GRAPH_UNREADABLE",
            "Alembic revision graph could not be read safely.",
        ),
    ],
)
def test_safe_heads_refuses_revision_loading_exceptions(
    exc: Exception,
    reason_code: str,
    message: str,
) -> None:
    class BrokenScriptDirectory:
        def _load_revisions(self) -> tuple[object, ...]:
            raise exc

    result = _safe_heads(BrokenScriptDirectory())

    assert isinstance(result, AlembicResolveResult)
    assert result.status == AlembicResolveStatus.refused
    assert result.reason_code == reason_code
    assert result.message == message
    assert result.details == {
        "error_type": type(exc).__name__,
        "error": str(exc),
    }


@pytest.mark.unit
def test_relative_path_returns_none_for_paths_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    inside = root / "migrations" / "versions" / "merge.py"
    outside = tmp_path / "other" / "merge.py"

    assert _relative_path(inside, root) == "migrations/versions/merge.py"
    assert _relative_path(outside, root) is None


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


@pytest.mark.unit
def test_load_script_directory_resolves_relative_versions_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_alembic_ini(tmp_path)
    fake_script = SimpleNamespace(versions="versions")

    def fake_from_config(_config: Config) -> object:
        return fake_script

    monkeypatch.setattr(alembic_resolver.ScriptDirectory, "from_config", fake_from_config)

    script, version_dir = alembic_resolver._load_script_directory(tmp_path)  # noqa: SLF001

    assert script is fake_script
    assert version_dir == tmp_path / "migrations" / "versions"
    assert version_dir.is_dir()


@pytest.mark.unit
def test_safe_heads_reports_revision_and_unexpected_graph_errors() -> None:
    class RevisionErrorScript:
        def _load_revisions(self) -> tuple[object, ...]:
            raise RevisionError("broken graph")

    class UnexpectedErrorScript:
        def _load_revisions(self) -> tuple[object, ...]:
            raise RuntimeError("cannot inspect graph")

    unsafe = _safe_heads(RevisionErrorScript())  # type: ignore[arg-type]
    unreadable = _safe_heads(UnexpectedErrorScript())  # type: ignore[arg-type]

    assert isinstance(unsafe, AlembicResolveResult)
    assert unsafe.reason_code == "ALEMBIC_GRAPH_UNSAFE"
    assert unsafe.to_dict()["details"]["error_type"] == "RevisionError"
    assert isinstance(unreadable, AlembicResolveResult)
    assert unreadable.reason_code == "ALEMBIC_GRAPH_UNREADABLE"
    assert unreadable.to_dict()["details"]["error_type"] == "RuntimeError"


@pytest.mark.unit
def test_safe_heads_returns_heads_for_clean_graph(tmp_path: Path) -> None:
    _write_alembic_ini(tmp_path)
    _write_revision(tmp_path, "base001", None)
    _write_revision(tmp_path, "head001", "base001")
    config = Config(str(tmp_path / "alembic.ini"))
    config.set_main_option("path_separator", "os")
    config.set_main_option("script_location", str(tmp_path / "migrations"))
    script = ScriptDirectory.from_config(config)

    assert _safe_heads(script) == ("head001",)


@pytest.mark.unit
def test_revision_tuple_and_relative_path_helpers_handle_non_string_sequences(
    tmp_path: Path,
) -> None:
    assert _as_revision_tuple(("left", "right")) == ("left", "right")
    assert _relative_path(tmp_path / "outside.py", tmp_path / "root") is None
