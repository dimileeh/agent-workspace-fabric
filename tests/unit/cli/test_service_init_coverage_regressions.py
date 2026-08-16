"""Focused coverage for local-service init path and failure handling."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.cli import service_init_ops


@pytest.mark.unit
def test_legacy_env_migration_requires_canonical_env_and_source_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy migration is limited to a canonical .env under an AWF source root."""

    assert service_init_ops._legacy_service_env_migration_allowed(tmp_path / "custom.env") is False

    monkeypatch.chdir(tmp_path)
    for marker in service_init_ops._AWF_SOURCE_ROOT_ENV_MIGRATION_MARKERS:
        marker_path = tmp_path / marker
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text("marker\n", encoding="utf-8")

    assert service_init_ops._legacy_service_env_migration_allowed(Path(".env")) is True


@pytest.mark.unit
def test_env_migration_payload_ignores_objects_without_serializers() -> None:
    """Unexpected migration objects cannot add opaque values to JSON output."""

    payload: dict[str, object] = {}
    service_init_ops._add_env_migration_payload(payload, object())
    assert payload == {}


@pytest.mark.unit
def test_trusted_compose_env_paths_require_local_compose_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the verified local Compose file may supply a non-default env file."""

    from awf.service import config as service_config

    compose_file = tmp_path / "docker" / "compose" / service_config.LOCAL_SERVICE_COMPOSE_FILE.name
    env_file = compose_file.with_name("service.env")
    compose_file.parent.mkdir(parents=True)
    compose_file.write_text("services: {}\n", encoding="utf-8")
    env_file.write_text("A=1\n", encoding="utf-8")

    assert service_init_ops._trusted_service_compose_env_file_from_verified_paths(
        compose_file, Path(".env")
    ) == Path(".env")
    assert (
        service_init_ops._trusted_service_compose_env_file_from_verified_paths(
            compose_file, tmp_path / "elsewhere" / "service.env"
        )
        is None
    )
    assert (
        service_init_ops._trusted_service_compose_env_file_from_verified_paths(
            compose_file.with_name("other.yml"), env_file
        )
        is None
    )

    monkeypatch.setattr(service_config, "_is_local_service_compose_file_path", lambda _path: True)
    assert service_init_ops._trusted_service_compose_env_file(compose_file, Path(".env")) == Path(
        ".env"
    )
    assert (
        service_init_ops._trusted_service_compose_env_file(
            compose_file, tmp_path / "elsewhere" / "service.env"
        )
        is None
    )


@pytest.mark.unit
def test_service_compose_env_file_rejects_untrusted_existing_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing env file is not passed to Compose unless its path is trusted."""

    active = tmp_path / "active.env"
    trusted = tmp_path / "trusted.env"
    active.write_text("A=1\n", encoding="utf-8")
    trusted.write_text("A=2\n", encoding="utf-8")

    assert (
        service_init_ops._service_compose_env_file(
            active,
            trusted_compose_env_file=trusted,
        )
        is None
    )
    monkeypatch.setattr(service_init_ops, "_is_local_service_compose_env_file", lambda _path: False)
    assert service_init_ops._service_compose_env_file(active) is None


@pytest.mark.unit
def test_init_env_overlay_source_rejects_unrelated_examples(tmp_path: Path) -> None:
    """Root overlays are used only with the root or Compose env template."""

    env_file = tmp_path / "docker" / "compose" / ".env"
    root_env = tmp_path / ".env"
    env_file.parent.mkdir(parents=True)
    root_env.write_text("ROOT_ONLY=1\n", encoding="utf-8")

    assert (
        service_init_ops._init_env_overlay_source(env_file, tmp_path / "unrelated.example") is None
    )


@pytest.mark.unit
def test_split_env_header_stops_at_non_comment_context() -> None:
    """Operator notes are assignment context, not reusable file-header comments."""

    header, context = service_init_ops._split_env_file_header_context(
        ["# first header\n", "operator note\n", "# APP_TOKEN docs\n"],
        "APP_TOKEN",
        seed_has_leading_context=False,
    )

    assert header == ["# first header\n", "operator note\n"]
    assert context == ["# APP_TOKEN docs\n"]


@pytest.mark.unit
def test_split_env_header_without_key_comment_keeps_full_header() -> None:
    """A non-comment tail stops key-comment scanning without inventing assignment context."""

    header, context = service_init_ops._split_env_file_header_context(
        ["# first header\n", "# second header\n", "operator note\n"],
        "APP_TOKEN",
        seed_has_leading_context=False,
    )

    assert header == ["# first header\n", "# second header\n", "operator note\n"]
    assert context == []


@pytest.mark.unit
def test_env_seed_merge_uses_seed_header_and_drops_duplicate_overlay_context() -> None:
    """A seed-owned header prevents a generic adjacent overlay comment from duplicating it."""

    merged, keys = service_init_ops._merge_env_seed_contents_with_overlay_keys(
        b"# seed header\nZZZ=seed\n",
        b"# overlay header\nZZZ=override\n",
    )

    assert merged == b"# seed header\nZZZ=override\n"
    assert keys == ()


@pytest.mark.unit
def test_env_seed_merge_keeps_last_overlay_duplicate_and_trailing_context() -> None:
    """Root-only duplicate keys keep the final value and their trailing operator notes."""

    merged, keys = service_init_ops._merge_env_seed_contents_with_overlay_keys(
        b"A=seed\n",
        b"B=first\nB=last\n# trailing note\n",
    )

    assert merged == b"A=seed\nB=last\n# trailing note\n"
    assert keys == ("B",)


@pytest.mark.unit
def test_seed_env_file_reports_overlay_read_failure(tmp_path: Path) -> None:
    """Unreadable overlay paths return a structured read_overlay failure."""

    env_file = tmp_path / "service.env"
    example = tmp_path / ".env.example"
    overlay = tmp_path / ".env"
    example.write_text("A=1\n", encoding="utf-8")
    overlay.mkdir()

    action, error, keys = service_init_ops._seed_env_file(
        env_file,
        example,
        env_overlay=overlay,
    )

    assert action == "write_failed"
    assert error is not None and error["operation"] == "read_overlay"
    assert keys == ()


class _FailingWriteHandle:
    def __enter__(self) -> _FailingWriteHandle:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def write(self, _contents: bytes) -> None:
        raise OSError("disk full")


@pytest.mark.unit
@pytest.mark.parametrize("unlink_error", [FileNotFoundError(), OSError("unlink failed")])
def test_seed_env_file_tolerates_cleanup_races_after_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unlink_error: OSError,
) -> None:
    """A failed seed write preserves the primary error when cleanup also fails."""

    env_file = tmp_path / "service.env"
    example = tmp_path / ".env.example"
    example.write_text("A=1\n", encoding="utf-8")
    real_open = Path.open
    real_unlink = Path.unlink

    def _open(path: Path, *args: object, **kwargs: object) -> object:
        if path == env_file:
            return _FailingWriteHandle()
        return real_open(path, *args, **kwargs)

    def _unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == env_file:
            raise unlink_error
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _open)
    monkeypatch.setattr(Path, "unlink", _unlink)

    action, error, keys = service_init_ops._seed_env_file(env_file, example)

    assert action == "write_failed"
    assert error is not None and error["operation"] == "write_env"
    assert keys == ()


@pytest.mark.unit
def test_init_display_path_falls_back_when_relpath_cannot_compare_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-drive path display errors fall back to the absolute path."""

    candidate = tmp_path / "service.env"
    monkeypatch.setattr(
        service_init_ops.os.path,
        "relpath",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("different drives")),
    )

    assert service_init_ops._init_display_path(candidate) == str(candidate)


@pytest.mark.unit
def test_init_env_warning_describes_parent_directory_failure() -> None:
    """Parent creation failures identify both the directory and destination env file."""

    warning = service_init_ops._init_env_warning(
        {
            "operation": "create_parent_directory",
            "path": "docker/compose",
            "env_file": "docker/compose/.env",
            "env_example": ".env.example",
            "message": "permission denied",
        }
    )

    assert "docker/compose" in warning
    assert "docker/compose/.env" in warning
    assert "permission denied" in warning


@pytest.mark.unit
def test_docker_diagnostic_lookup_returns_none_without_matching_entry() -> None:
    """Doctor reports without a Docker diagnostic are treated as unknown."""

    report = SimpleNamespace(diagnostics=[SimpleNamespace(id="postgres")])
    assert service_init_ops._docker_diagnostic_from_report(report) is None
