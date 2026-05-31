"""Service-mode auth mount resolution for worker-created workspace stacks."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.node import auth_mounts as auth_mounts_mod
from awf.node.auth_mounts import ServiceAuthMountResolver, resolve_service_auth_mounts
from awf.node.compose_manager import AuthMount


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
    (host_home / ".grok").mkdir()
    (host_home / ".grok" / "auth.json").write_text('{"token":"do-not-mount"}\n')
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
    assert (ollama_home / ".ollama" / "config.json").read_text() == ('{"integrations": {}}\n')
    assert (ollama_home / ".ollama" / "id_ed25519").read_text() == "private-key\n"
    assert not (ollama_home / ".ollama" / "models").exists()
    assert "/home/agent/.grok" not in by_target
    assert not (work_dir / "auth" / "ws_auth" / "grok").exists()


@pytest.mark.unit
def test_service_auth_mounts_skip_dangling_claude_skill_links(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    host_claude = host_home / ".claude"
    skill_dir = host_claude / "skills" / "stale-skill"
    skill_dir.mkdir(parents=True)
    (host_claude / "settings.json").write_text('{"theme": "dark"}\n')
    (skill_dir / "SKILL.md").symlink_to(host_home / "removed" / "SKILL.md")
    work_dir = tmp_path / "work"

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_auth",
        host_env={},
    )

    claude_home = Path({m.target: m for m in mounts}["/home/agent/.claude"].source)
    assert (claude_home / "settings.json").read_text() == '{"theme": "dark"}\n'
    assert not (claude_home / "skills" / "stale-skill" / "SKILL.md").exists()


@pytest.mark.unit
def test_service_auth_mounts_copy_codex_into_workspace_isolated_home(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    host_codex = host_home / ".codex"
    host_codex.mkdir(parents=True)
    (host_codex / "auth.json").write_text('{"token": "redacted"}')
    (host_codex / "config.toml").write_text("model = 'gpt-5.5'\n")
    (host_codex / "installation_id").write_text("installation-123\n")
    (host_codex / "logs_2.db").write_text("do not copy")
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
    assert not (codex_home / "logs_2.db").exists()
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
def test_service_auth_mounts_exclude_claude_usage_history(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    host_claude = host_home / ".claude"
    (host_claude / "projects" / "repo").mkdir(parents=True)
    (host_claude / "projects" / "repo" / "session.jsonl").write_text('{"usage": "historical"}\n')
    (host_claude / "todos").mkdir()
    (host_claude / "todos" / "x.json").write_text("{}\n")
    (host_claude / "shell-snapshots").mkdir()
    (host_claude / "statsig").mkdir()
    (host_claude / "settings.json").write_text('{"theme": "dark"}\n')
    work_dir = tmp_path / "work"

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_auth",
        host_env={},
    )

    claude_home = Path({m.target: m for m in mounts}["/home/agent/.claude"].source)
    # Auth survives the copy, but the historical usage/transcript dirs do not —
    # so ccusage cannot attribute host history to this workspace run.
    assert (claude_home / "settings.json").read_text() == '{"theme": "dark"}\n'
    assert not (claude_home / "projects").exists()
    assert not (claude_home / "todos").exists()
    assert not (claude_home / "shell-snapshots").exists()
    assert not (claude_home / "statsig").exists()


@pytest.mark.unit
def test_service_auth_mounts_exclude_gemini_usage_history(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    host_gemini = host_home / ".gemini"
    (host_gemini / "tmp" / "session").mkdir(parents=True)
    (host_gemini / "tmp" / "session" / "logs.json").write_text('[{"tokens": 999}]\n')
    (host_gemini / "settings.json").write_text('{"selectedAuthType": "oauth"}\n')
    work_dir = tmp_path / "work"

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_auth",
        host_env={},
    )

    gemini_home = Path({m.target: m for m in mounts}["/home/agent/.gemini"].source)
    assert (gemini_home / "settings.json").read_text() == '{"selectedAuthType": "oauth"}\n'
    assert not (gemini_home / "tmp").exists()


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

    assert (opencode_dir / "opencode.json").read_text() == ('{"model": "agent-refreshed"}\n')
    assert (ollama_dir / "config.json").read_text() == '{"token": "agent-refreshed"}\n'


@pytest.mark.unit
def test_service_auth_mounts_chown_isolated_writable_auth_for_agent_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host_home = tmp_path / "host-home"
    host_codex = host_home / ".codex"
    host_claude = host_home / ".claude"
    host_gemini = host_home / ".gemini"
    host_opencode = host_home / ".config" / "opencode"
    host_ollama = host_home / ".ollama"
    host_codex.mkdir(parents=True)
    host_claude.mkdir(parents=True)
    host_gemini.mkdir(parents=True)
    host_opencode.mkdir(parents=True)
    host_ollama.mkdir(parents=True)
    (host_codex / "auth.json").write_text('{"token": "codex"}\n')
    (host_codex / "rules").mkdir()
    (host_codex / "rules" / "default.rules").write_text("rule\n")
    (host_claude / "settings.json").write_text('{"token": "claude"}\n')
    (host_home / ".claude.json").write_text('{"token": "claude-file"}\n')
    (host_gemini / "settings.json").write_text('{"token": "gemini"}\n')
    (host_opencode / "opencode.json").write_text('{"token": "opencode"}\n')
    (host_ollama / "config.json").write_text('{"token": "ollama"}\n')
    (host_ollama / "id_ed25519").write_text("private-key\n")
    (host_home / ".gitconfig").write_text("[user]\n  name = Host\n")
    work_dir = tmp_path / "work"
    chowned: list[tuple[Path, int, int]] = []

    def _record_chown(path: str | bytes, uid: int, gid: int) -> None:
        chowned.append((Path(path), uid, gid))

    monkeypatch.setattr("awf.node.auth_mounts.os.chown", _record_chown)

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_auth",
        host_env={},
        workspace_owner_uid=1000,
        workspace_owner_gid=1000,
    )

    by_target = {m.target: m for m in mounts}
    expected_paths = {
        Path(by_target["/home/agent/.codex"].source),
        Path(by_target["/home/agent/.codex"].source) / "auth.json",
        Path(by_target["/home/agent/.codex"].source) / "rules" / "default.rules",
        Path(by_target["/home/agent/.claude"].source),
        Path(by_target["/home/agent/.claude"].source) / "settings.json",
        Path(by_target["/home/agent/.claude.json"].source),
        Path(by_target["/home/agent/.gemini"].source),
        Path(by_target["/home/agent/.gemini"].source) / "settings.json",
        Path(by_target["/home/agent/.config/opencode"].source),
        Path(by_target["/home/agent/.config/opencode"].source) / "opencode.json",
        Path(by_target["/home/agent/.ollama"].source),
        Path(by_target["/home/agent/.ollama"].source) / "config.json",
        Path(by_target["/home/agent/.ollama"].source) / "id_ed25519",
    }
    assert expected_paths <= {path for path, _, _ in chowned}
    assert all(uid == 1000 and gid == 1000 for _, uid, gid in chowned)
    assert all(path.is_relative_to(work_dir / "auth" / "ws_auth") for path, _, _ in chowned)
    assert host_home / ".gitconfig" not in {path for path, _, _ in chowned}


@pytest.mark.unit
def test_service_auth_mounts_require_complete_owner_ids(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    host_home.mkdir()

    with pytest.raises(ValueError, match="requires both uid and gid"):
        resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=tmp_path / "work",
            workspace_id="ws_auth",
            host_env={},
            workspace_owner_uid=1000,
        )


@pytest.mark.unit
def test_service_auth_mounts_skip_readonly_mounts_for_chown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    (host_home / ".gitconfig").write_text("[user]\n  name = Host\n")
    chowned: list[Path] = []

    def _record_chown(path: str | bytes, uid: int, gid: int) -> None:
        del uid, gid
        chowned.append(Path(path))

    monkeypatch.setattr("awf.node.auth_mounts.os.chown", _record_chown)

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=tmp_path / "work",
        workspace_id="ws_auth",
        host_env={},
        workspace_owner_uid=1000,
        workspace_owner_gid=1000,
    )

    assert [mount.target for mount in mounts] == ["/home/agent/.gitconfig"]
    assert chowned == []


@pytest.mark.unit
def test_chown_tree_uses_lchown_for_symlinks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.write_text("target\n")
    root_link = tmp_path / "root-link"
    root_link.symlink_to(target)
    directory = tmp_path / "directory"
    directory.mkdir()
    child_link = directory / "child-link"
    child_link.symlink_to(target)
    chowned: list[Path] = []
    lchowned: list[Path] = []

    def _record_chown(path: str | bytes, uid: int, gid: int) -> None:
        del uid, gid
        chowned.append(Path(path))

    def _record_lchown(path: str | bytes, uid: int, gid: int) -> None:
        del uid, gid
        lchowned.append(Path(path))

    monkeypatch.setattr("awf.node.auth_mounts.os.chown", _record_chown)
    monkeypatch.setattr("awf.node.auth_mounts.os.lchown", _record_lchown)

    auth_mounts_mod._chown_tree(root_link, 1000, 1000)
    auth_mounts_mod._chown_tree(directory, 1000, 1000)

    assert lchowned == [root_link, child_link]
    assert chowned == [directory]


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


@pytest.mark.unit
def test_chown_workspace_auth_sources_skips_read_only_mounts() -> None:
    # Read-only mounts must be skipped: no chown is attempted on the source,
    # so a non-existent path is safe and the call returns without error.
    auth_mounts_mod._chown_workspace_auth_sources(  # noqa: SLF001
        [AuthMount(source="/nonexistent/ro-source", target="/agent/ro", mode="ro")],
        uid=1000,
        gid=1000,
    )
