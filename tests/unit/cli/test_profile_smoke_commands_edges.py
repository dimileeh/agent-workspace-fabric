"""Focused coverage for profile/smoke command edge branches."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from awf.cli import profile_smoke_commands
from awf.cli.common import OutputFormat


@pytest.mark.unit
def test_profile_preview_json_emits_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payloads: list[object] = []

    monkeypatch.setattr(
        "awf.profiles.resolver.resolve_workspace_profile",
        lambda **_kwargs: SimpleNamespace(model_dump=lambda **_dump_kwargs: {"profile": "ok"}),
    )
    monkeypatch.setattr(
        profile_smoke_commands, "_emit", lambda payload, _fmt: payloads.append(payload)
    )

    profile_smoke_commands.profile_preview(str(tmp_path), fmt=OutputFormat.json)

    assert payloads == [{"profile": "ok"}]


@pytest.mark.unit
def test_profile_preview_pretty_uses_pretty_renderer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payloads: list[object] = []

    monkeypatch.setattr(
        "awf.profiles.resolver.resolve_workspace_profile",
        lambda **_kwargs: SimpleNamespace(model_dump=lambda **_dump_kwargs: {"profile": "pretty"}),
    )
    monkeypatch.setattr(
        profile_smoke_commands,
        "_emit_profile_preview_pretty",
        lambda payload: payloads.append(payload),
    )

    profile_smoke_commands.profile_preview(str(tmp_path), fmt=OutputFormat.pretty)

    assert payloads == [{"profile": "pretty"}]


@pytest.mark.unit
def test_profile_preview_resolves_forge_from_checkout_remote(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A bitbucket ``origin`` on the checkout surfaces as ``forge: bitbucket`` in preview."""
    payloads: list[dict[str, object]] = []

    monkeypatch.setattr(
        "awf.common.git_remote.detect_repo_url_from_checkout",
        lambda _path: "https://bitbucket.org/o/r.git",
    )
    monkeypatch.setattr(
        profile_smoke_commands,
        "_emit",
        lambda payload, _fmt: payloads.append(payload),
    )

    profile_smoke_commands.profile_preview(
        str(tmp_path),
        profile_ref="auto",
        validation_command=[],
        fmt=OutputFormat.json,
    )

    assert payloads and payloads[0]["profile"]["forge"] == "bitbucket"


@pytest.mark.unit
def test_profile_init_write_adds_written_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payloads: list[object] = []
    written = tmp_path / ".awf" / "workspace.yml"

    monkeypatch.setattr(
        "awf.profiles.onboarding.preview_project_onboarding",
        lambda *_args, **_kwargs: SimpleNamespace(to_dict=lambda: {"profile": "draft"}),
    )
    monkeypatch.setattr(
        "awf.profiles.onboarding.write_workspace_profile",
        lambda *_args, **_kwargs: written,
    )
    monkeypatch.setattr(
        profile_smoke_commands, "_emit", lambda payload, _fmt: payloads.append(payload)
    )

    profile_smoke_commands.profile_init(
        tmp_path,
        template="auto",
        write=True,
        force=True,
        include_smoke_request=True,
        fmt=OutputFormat.json,
    )

    assert payloads == [{"profile": "draft", "written_path": str(written)}]


@pytest.mark.unit
def test_profile_init_file_exists_exits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "awf.profiles.onboarding.preview_project_onboarding",
        lambda *_args, **_kwargs: SimpleNamespace(to_dict=lambda: {"profile": "draft"}),
    )

    def _raise_exists(*_args: object, **_kwargs: object) -> object:
        raise FileExistsError("profile exists")

    monkeypatch.setattr("awf.profiles.onboarding.write_workspace_profile", _raise_exists)

    with pytest.raises(typer.Exit) as excinfo:
        profile_smoke_commands.profile_init(
            tmp_path,
            template="auto",
            write=True,
            force=False,
            include_smoke_request=False,
            fmt=OutputFormat.json,
        )

    assert excinfo.value.exit_code == 1
    assert "profile exists" in capsys.readouterr().err


@pytest.mark.unit
def test_profile_init_value_error_exits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    def _raise_value_error(*_args: object, **_kwargs: object) -> object:
        raise ValueError("bad template")

    monkeypatch.setattr("awf.profiles.onboarding.preview_project_onboarding", _raise_value_error)

    with pytest.raises(typer.Exit) as excinfo:
        profile_smoke_commands.profile_init(
            tmp_path,
            template="auto",
            write=False,
            force=False,
            include_smoke_request=False,
            fmt=OutputFormat.json,
        )

    assert excinfo.value.exit_code == 2
    assert "bad template" in capsys.readouterr().err


@pytest.mark.unit
def test_smoke_run_pretty_success_and_json_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pretty_payloads: list[object] = []
    json_payloads: list[object] = []
    reports = iter(
        [
            {"status": "ok", "project": "demo"},
            {"status": "fail", "project": "demo"},
        ]
    )

    async def _collect_smoke_report(**_kwargs: object) -> dict[str, object]:
        return next(reports)

    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings",
        lambda: SimpleNamespace(settings="ok"),
    )
    monkeypatch.setattr("awf.service.smoke.collect_smoke_report", _collect_smoke_report)
    monkeypatch.setattr(
        profile_smoke_commands,
        "_emit_smoke_pretty",
        lambda payload: pretty_payloads.append(payload),
    )

    profile_smoke_commands.smoke_run(
        project=tmp_path,
        fmt=OutputFormat.pretty,
        mocked_local=True,
        demo_path=tmp_path,
    )

    assert pretty_payloads == [{"status": "ok", "project": "demo"}]

    monkeypatch.setattr(
        profile_smoke_commands, "_emit", lambda payload, _fmt: json_payloads.append(payload)
    )

    with pytest.raises(typer.Exit) as excinfo:
        profile_smoke_commands.smoke_run(
            project=tmp_path,
            fmt=OutputFormat.json,
            mocked_local=False,
            demo_path=None,
        )

    assert excinfo.value.exit_code == 1
    assert json_payloads == [{"status": "fail", "project": "demo"}]
