"""Service-mode auth mount resolution for worker-created workspace stacks."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.node.auth_mounts import ServiceAuthMountResolver, resolve_service_auth_mounts


@pytest.mark.unit
def test_service_auth_mounts_include_existing_host_credentials(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    (host_home / ".config" / "gh").mkdir(parents=True)
    (host_home / ".config" / "gcloud").mkdir(parents=True)
    (host_home / ".ssh").mkdir(parents=True)
    (host_home / ".claude").mkdir()
    (host_home / ".claude" / "settings.json").write_text('{"theme": "dark"}\n')
    (host_home / ".gemini").mkdir()
    (host_home / ".gemini" / "settings.json").write_text('{"selectedAuthType": "oauth"}\n')
    (host_home / ".config" / "opencode").mkdir(parents=True)
    (host_home / ".config" / "opencode" / "opencode.json").write_text('{"model": "ollama/x"}\n')
    (host_home / ".ollama").mkdir()
    (host_home / ".ollama" / "config.json").write_text('{"integrations": {}}\n')
    (host_home / ".ollama" / "id_ed25519").write_text("private-key\n")
    (host_home / ".ollama" / "models").mkdir()
    (host_home / ".ollama" / "models" / "large-blob").write_text("do not copy\n")
    (host_home / ".gitconfig").write_text("[user]\n  name = Test\n")
    (host_home / ".claude.json").write_text("{}\n")

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_auth",
        host_env={},
    )

    by_target = {m.target: m for m in mounts}
    claude_home = work_dir / "auth" / "ws_auth" / "claude"
    gemini_home = work_dir / "auth" / "ws_auth" / "gemini"
    opencode_home = work_dir / "auth" / "ws_auth" / "opencode"
    ollama_home = work_dir / "auth" / "ws_auth" / "ollama"
    assert by_target["/home/agent/.config/gh"].source == str(host_home / ".config" / "gh")
    assert by_target["/home/agent/.config/gh"].mode == "ro"
    assert by_target["/home/agent/.config/gcloud"].source == str(host_home / ".config" / "gcloud")
    assert by_target["/home/agent/.config/gcloud"].mode == "ro"
    assert by_target["/home/agent/.gitconfig"].source == str(host_home / ".gitconfig")
    assert by_target["/home/agent/.gitconfig"].mode == "ro"
    assert by_target["/home/agent/.ssh"].source == str(host_home / ".ssh")
    assert by_target["/home/agent/.ssh"].mode == "ro"
    assert by_target["/home/agent/.claude"].source == str(claude_home / ".claude")
    assert by_target["/home/agent/.claude"].mode == "rw"
    assert by_target["/home/agent/.claude.json"].source == str(claude_home / ".claude.json")
    assert by_target["/home/agent/.claude.json"].mode == "rw"
    assert (claude_home / ".claude" / "settings.json").read_text() == '{"theme": "dark"}\n'
    assert (claude_home / ".claude.json").read_text() == "{}\n"
    assert by_target["/home/agent/.gemini"].source == str(gemini_home / ".gemini")
    assert by_target["/home/agent/.gemini"].mode == "rw"
    assert (gemini_home / ".gemini" / "settings.json").read_text() == (
        '{"selectedAuthType": "oauth"}\n'
    )
    assert by_target["/home/agent/.config/opencode"].source == str(
        opencode_home / ".config" / "opencode"
    )
    assert by_target["/home/agent/.config/opencode"].mode == "rw"
    assert (
        opencode_home / ".config" / "opencode" / "opencode.json"
    ).read_text() == '{"model": "ollama/x"}\n'
    assert by_target["/home/agent/.ollama"].source == str(ollama_home / ".ollama")
    assert by_target["/home/agent/.ollama"].mode == "rw"
    assert (ollama_home / ".ollama" / "config.json").read_text() == (
        '{"integrations": {}}\n'
    )
    assert (ollama_home / ".ollama" / "id_ed25519").read_text() == "private-key\n"
    assert not (ollama_home / ".ollama" / "models").exists()


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
def test_service_auth_mounts_suppressed_targets_skip_mounts_and_workspace_copying(
    tmp_path: Path,
) -> None:
    host_home = tmp_path / "host-home"
    host_codex = host_home / ".codex"
    host_codex.mkdir(parents=True)
    (host_codex / "auth.json").write_text('{"token": "do-not-copy"}')
    (host_home / ".config" / "gh").mkdir(parents=True)
    (host_home / ".claude").mkdir()
    (host_home / ".claude" / "settings.json").write_text('{"token": "do-not-copy"}')
    (host_home / ".claude.json").write_text('{"token": "do-not-copy"}')
    (host_home / ".gemini").mkdir()
    (host_home / ".config" / "opencode").mkdir(parents=True)
    (host_home / ".ollama").mkdir()
    work_dir = tmp_path / "work"

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_auth",
        host_env={},
        suppressed_targets=frozenset(
            {
                "/home/agent/.codex",
                "/home/agent/.config/gh",
                "/home/agent/.claude",
                "/home/agent/.claude.json",
                "/home/agent/.gemini",
                "/home/agent/.config/opencode",
                "/home/agent/.ollama",
            }
        ),
    )

    assert mounts == ()
    assert not (work_dir / "auth" / "ws_auth").exists()


@pytest.mark.unit
def test_service_auth_mounts_suppressed_provider_skips_provider_mount_but_keeps_compatibility(
    tmp_path: Path,
) -> None:
    host_home = tmp_path / "host-home"
    (host_home / ".config" / "gh").mkdir(parents=True)
    host_codex = host_home / ".codex"
    host_codex.mkdir()
    (host_codex / "auth.json").write_text('{"token": "compat"}')
    work_dir = tmp_path / "work"

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_auth",
        host_env={},
        suppressed_providers=frozenset({"github"}),
    )

    by_target = {mount.target: mount for mount in mounts}
    assert "/home/agent/.config/gh" not in by_target
    assert Path(by_target["/home/agent/.codex"].source) == work_dir / "auth" / "ws_auth" / "codex"


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
def test_service_auth_mounts_preserve_existing_workspace_claude_auth(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    host_claude = host_home / ".claude"
    host_claude.mkdir(parents=True)
    (host_claude / "settings.json").write_text('{"theme": "initial"}\n')
    (host_home / ".claude.json").write_text('{"token": "initial"}\n')
    work_dir = tmp_path / "work"

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_auth",
        host_env={},
    )
    by_target = {m.target: m for m in mounts}
    claude_dir = Path(by_target["/home/agent/.claude"].source)
    claude_config = Path(by_target["/home/agent/.claude.json"].source)
    (claude_dir / "settings.json").write_text('{"theme": "agent-refreshed"}\n')
    claude_config.write_text('{"token": "agent-refreshed"}\n')
    (host_claude / "settings.json").write_text('{"theme": "host-updated"}\n')
    (host_home / ".claude.json").write_text('{"token": "host-updated"}\n')

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_auth",
        host_env={},
    )

    assert (claude_dir / "settings.json").read_text() == '{"theme": "agent-refreshed"}\n'
    assert claude_config.read_text() == '{"token": "agent-refreshed"}\n'


@pytest.mark.unit
def test_service_auth_mounts_preserve_existing_workspace_gemini_auth(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    host_gemini = host_home / ".gemini"
    host_gemini.mkdir(parents=True)
    (host_gemini / "settings.json").write_text('{"auth": "initial"}\n')
    work_dir = tmp_path / "work"

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_auth",
        host_env={},
    )
    gemini_dir = Path({m.target: m for m in mounts}["/home/agent/.gemini"].source)
    (gemini_dir / "settings.json").write_text('{"auth": "agent-refreshed"}\n')
    (host_gemini / "settings.json").write_text('{"auth": "host-updated"}\n')

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_auth",
        host_env={},
    )

    assert (gemini_dir / "settings.json").read_text() == '{"auth": "agent-refreshed"}\n'


@pytest.mark.unit
def test_service_auth_mounts_preserve_existing_workspace_opencode_and_ollama_auth(
    tmp_path: Path,
) -> None:
    host_home = tmp_path / "host-home"
    host_opencode = host_home / ".config" / "opencode"
    host_ollama = host_home / ".ollama"
    host_opencode.mkdir(parents=True)
    host_ollama.mkdir(parents=True)
    (host_opencode / "opencode.json").write_text('{"model": "initial"}\n')
    (host_ollama / "config.json").write_text('{"token": "initial"}\n')
    work_dir = tmp_path / "work"

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_auth",
        host_env={},
    )
    by_target = {m.target: m for m in mounts}
    opencode_dir = Path(by_target["/home/agent/.config/opencode"].source)
    ollama_dir = Path(by_target["/home/agent/.ollama"].source)
    (opencode_dir / "opencode.json").write_text('{"model": "agent-refreshed"}\n')
    (ollama_dir / "config.json").write_text('{"token": "agent-refreshed"}\n')
    (host_opencode / "opencode.json").write_text('{"model": "host-updated"}\n')
    (host_ollama / "config.json").write_text('{"token": "host-updated"}\n')

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_auth",
        host_env={},
    )

    assert (opencode_dir / "opencode.json").read_text() == (
        '{"model": "agent-refreshed"}\n'
    )
    assert (ollama_dir / "config.json").read_text() == '{"token": "agent-refreshed"}\n'


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
def test_service_auth_mounts_skip_missing_google_application_credentials_file(
    tmp_path: Path,
) -> None:
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    missing_credentials = tmp_path / "missing-service-account.json"

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=tmp_path / "work",
        workspace_id="ws_auth",
        host_env={"GOOGLE_APPLICATION_CREDENTIALS": str(missing_credentials)},
    )

    assert all(m.target != str(missing_credentials) for m in mounts)


@pytest.mark.unit
def test_service_auth_mounts_create_empty_ollama_auth_without_copying_models(
    tmp_path: Path,
) -> None:
    host_home = tmp_path / "host-home"
    host_ollama = host_home / ".ollama"
    host_ollama.mkdir(parents=True)
    (host_ollama / "models").mkdir()
    (host_ollama / "models" / "large-blob").write_text("do not copy\n")
    work_dir = tmp_path / "work"

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_auth",
        host_env={},
    )

    by_target = {m.target: m for m in mounts}
    ollama_dir = Path(by_target["/home/agent/.ollama"].source)
    assert by_target["/home/agent/.ollama"].mode == "rw"
    assert ollama_dir == work_dir / "auth" / "ws_auth" / "ollama" / ".ollama"
    assert ollama_dir.is_dir()
    assert not (ollama_dir / "models").exists()
    assert list(ollama_dir.iterdir()) == []


@pytest.mark.unit
def test_service_auth_mount_resolver_delegates_to_service_mount_resolution(
    tmp_path: Path,
) -> None:
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    (host_home / ".gitconfig").write_text("[user]\n  name = Test\n")
    resolver = ServiceAuthMountResolver(
        host_home=host_home,
        work_dir=tmp_path / "work",
        host_env={},
    )

    mounts = resolver.resolve(workspace_id="ws_auth")

    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.gitconfig"].source == str(host_home / ".gitconfig")
    assert by_target["/home/agent/.gitconfig"].mode == "ro"


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
