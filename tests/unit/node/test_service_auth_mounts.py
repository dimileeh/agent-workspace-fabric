"""Service-mode auth mount resolution for worker-created workspace stacks."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.node.auth_mounts import resolve_service_auth_mounts


@pytest.mark.unit
def test_service_auth_mounts_include_existing_host_credentials(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    (host_home / ".config" / "gh").mkdir(parents=True)
    (host_home / ".config" / "gcloud").mkdir(parents=True)
    (host_home / ".ssh").mkdir(parents=True)
    (host_home / ".claude").mkdir()
    (host_home / ".gemini").mkdir()
    (host_home / ".gitconfig").write_text("[user]\n  name = Test\n")
    (host_home / ".claude.json").write_text("{}\n")

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=tmp_path / "work",
        workspace_id="ws_auth",
        host_env={},
    )

    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.config/gh"].source == str(host_home / ".config" / "gh")
    assert by_target["/home/agent/.config/gh"].mode == "ro"
    assert by_target["/home/agent/.config/gcloud"].source == str(host_home / ".config" / "gcloud")
    assert by_target["/home/agent/.config/gcloud"].mode == "ro"
    assert by_target["/home/agent/.gitconfig"].source == str(host_home / ".gitconfig")
    assert by_target["/home/agent/.gitconfig"].mode == "ro"
    assert by_target["/home/agent/.ssh"].source == str(host_home / ".ssh")
    assert by_target["/home/agent/.ssh"].mode == "ro"
    assert by_target["/home/agent/.claude"].source == str(host_home / ".claude")
    assert by_target["/home/agent/.claude"].mode == "rw"
    assert by_target["/home/agent/.claude.json"].source == str(host_home / ".claude.json")
    assert by_target["/home/agent/.claude.json"].mode == "rw"
    assert by_target["/home/agent/.gemini"].source == str(host_home / ".gemini")
    assert by_target["/home/agent/.gemini"].mode == "rw"


@pytest.mark.unit
def test_service_auth_mounts_copy_codex_into_workspace_isolated_home(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    host_codex = host_home / ".codex"
    host_codex.mkdir(parents=True)
    (host_codex / "auth.json").write_text('{"token": "redacted"}')
    (host_codex / "config.toml").write_text("model = 'gpt-5.5'\n")
    (host_codex / "installation_id").write_text("installation-123\n")
    (host_codex / "logs_2.sqlite").write_text("do not copy")
    (host_codex / "sessions").mkdir()
    (host_codex / "rules").mkdir()
    (host_codex / "rules" / "default.rules").write_text("rule")
    work_dir = tmp_path / "work"

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_auth",
        host_env={},
    )

    by_target = {m.target: m for m in mounts}
    codex_mount = by_target["/home/agent/.codex"]
    codex_home = Path(codex_mount.source)
    assert codex_mount.mode == "rw"
    assert codex_home == work_dir / "auth" / "ws_auth" / "codex"
    assert (codex_home / "auth.json").read_text() == '{"token": "redacted"}'
    assert (codex_home / "config.toml").read_text() == "model = 'gpt-5.5'\n"
    assert (codex_home / "installation_id").read_text() == "installation-123\n"
    assert (codex_home / "rules" / "default.rules").read_text() == "rule"
    assert not (codex_home / "logs_2.sqlite").exists()
    assert not (codex_home / "sessions").exists()


@pytest.mark.unit
def test_service_auth_mounts_preserve_existing_workspace_codex_home(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    host_codex = host_home / ".codex"
    host_codex.mkdir(parents=True)
    (host_codex / "auth.json").write_text('{"token": "initial"}')
    (host_codex / "config.toml").write_text("model = 'gpt-5.5'\n")
    work_dir = tmp_path / "work"

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_auth",
        host_env={},
    )
    codex_home = Path({m.target: m for m in mounts}["/home/agent/.codex"].source)
    (codex_home / "auth.json").write_text('{"token": "agent-refreshed"}')
    (codex_home / "sessions").mkdir()
    (codex_home / "sessions" / "session.jsonl").write_text("{}\n")
    (host_codex / "auth.json").write_text('{"token": "host-updated"}')

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_auth",
        host_env={},
    )

    assert (codex_home / "auth.json").read_text() == '{"token": "agent-refreshed"}'
    assert (codex_home / "sessions" / "session.jsonl").read_text() == "{}\n"


@pytest.mark.unit
def test_service_auth_mounts_include_google_application_credentials_file(
    tmp_path: Path,
) -> None:
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    credentials = tmp_path / "gcloud-service-account.json"
    credentials.write_text("{}")

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=tmp_path / "work",
        workspace_id="ws_auth",
        host_env={"GOOGLE_APPLICATION_CREDENTIALS": str(credentials)},
    )

    by_target = {m.target: m for m in mounts}
    assert by_target[str(credentials)].source == str(credentials)
    assert by_target[str(credentials)].mode == "ro"


@pytest.mark.unit
def test_service_auth_mounts_skip_missing_paths(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    host_home.mkdir()

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=tmp_path / "work",
        workspace_id="ws_auth",
        host_env={},
    )

    assert mounts == ()
