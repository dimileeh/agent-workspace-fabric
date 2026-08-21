"""Worker Git environment forwarding tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from awf.common.git_auth import bitbucket_git_config_entries
from awf.service import worker as worker_mod


@pytest.mark.unit
def test_service_git_environment_uses_mounted_host_home(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    ssh_dir = host_home / ".ssh"
    ssh_dir.mkdir(parents=True)
    (host_home / ".gitconfig").write_text("[user]\n  name = AWF\n")
    ssh_config = ssh_dir / "config"
    ssh_config.write_text("Host github.com\n  UseKeychain yes\n")
    known_hosts = ssh_dir / "known_hosts"
    known_hosts.write_text("github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA...\n")

    env = worker_mod._service_git_environment(host_home)

    assert env["HOME"] == str(host_home)
    assert env["GIT_CONFIG_GLOBAL"] == str(host_home / ".gitconfig")
    assert "IgnoreUnknown=UseKeychain" in env["GIT_SSH_COMMAND"]
    assert str(ssh_config) in env["GIT_SSH_COMMAND"]
    assert str(known_hosts) in env["GIT_SSH_COMMAND"]
    assert "StrictHostKeyChecking=accept-new" in env["GIT_SSH_COMMAND"]


@pytest.mark.unit
def test_apply_service_git_environment_drops_removed_global_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/stale/snapshot/.gitconfig")
    monkeypatch.setenv("HOME", os.environ.get("HOME", "/root"))
    worker_mod._apply_service_git_environment({"HOME": "/host-home"})
    assert "GIT_CONFIG_GLOBAL" not in os.environ


@pytest.mark.unit
def test_service_git_environment_forwards_github_token_for_gh_cli(tmp_path: Path) -> None:
    env = worker_mod._service_git_environment(
        tmp_path / "host-home", github_token="ghp_service_token"
    )
    assert env["GH_TOKEN"] == "ghp_service_token"
    assert env["GITHUB_TOKEN"] == "ghp_service_token"


@pytest.mark.unit
def test_service_git_environment_marks_worker_managed_worktrees_safe(tmp_path: Path) -> None:
    env = worker_mod._service_git_environment(tmp_path / "host-home", source_env={})
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "safe.directory"
    assert env["GIT_CONFIG_VALUE_0"] == "*"


@pytest.mark.unit
def test_service_git_environment_configures_gh_credential_helper_for_git(
    tmp_path: Path,
) -> None:
    env = worker_mod._service_git_environment(
        tmp_path / "host-home", github_token="ghp_service_token"
    )
    count = int(env["GIT_CONFIG_COUNT"])
    entries = {
        env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"] for index in range(count)
    }
    assert entries["safe.directory"] == "*"
    assert entries["credential.https://github.com.helper"] == "!gh auth git-credential"
    assert entries["url.https://github.com/.insteadOf"] == "git@github.com:"
    assert all("ghp_service_token" not in value for value in entries.values())


@pytest.mark.unit
def test_service_git_environment_forwards_ssh_agent_socket(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/host-services/ssh-auth.sock")
    env = worker_mod._service_git_environment(tmp_path / "host-home")
    assert env["SSH_AUTH_SOCK"] == "/run/host-services/ssh-auth.sock"
    assert "IdentityAgent=/run/host-services/ssh-auth.sock" in env["GIT_SSH_COMMAND"]


@pytest.mark.unit
def test_service_git_environment_wires_bitbucket_helper_without_leaking_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret_token = "ATATT-service-token-do-not-render"
    monkeypatch.setenv("BITBUCKET_API_TOKEN", secret_token)
    monkeypatch.setenv("BITBUCKET_EMAIL", "agent@example.com")
    env = worker_mod._service_git_environment(
        tmp_path / "host-home", github_token="ghp_service_token"
    )
    count = int(env["GIT_CONFIG_COUNT"])
    entries = {
        env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"] for index in range(count)
    }
    assert "credential.https://bitbucket.org.helper" in entries
    assert entries["credential.https://github.com.helper"] == "!gh auth git-credential"
    assert entries["url.https://github.com/.insteadOf"] == "git@github.com:"
    bitbucket_insteadof = [
        env[f"GIT_CONFIG_VALUE_{index}"]
        for index in range(count)
        if env[f"GIT_CONFIG_KEY_{index}"] == "url.https://bitbucket.org/.insteadOf"
    ]
    assert "git@bitbucket.org:" in bitbucket_insteadof
    assert "ssh://git@bitbucket.org/" in bitbucket_insteadof
    assert "ssh://git@bitbucket.org:22/" in bitbucket_insteadof
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert all(secret_token not in value for value in env.values())


@pytest.mark.unit
def test_service_git_environment_unchanged_without_bitbucket_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("BITBUCKET_API_TOKEN", raising=False)
    monkeypatch.delenv("BITBUCKET_EMAIL", raising=False)
    env = worker_mod._service_git_environment(
        tmp_path / "host-home", github_token="ghp_service_token"
    )
    assert "GIT_TERMINAL_PROMPT" not in env
    count = int(env["GIT_CONFIG_COUNT"])
    entries = {
        env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"] for index in range(count)
    }
    assert {key for key, _ in bitbucket_git_config_entries()}.isdisjoint(entries)
    assert entries["credential.https://github.com.helper"] == "!gh auth git-credential"


@pytest.mark.unit
def test_service_git_environment_reads_bitbucket_and_ssh_from_source_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("BITBUCKET_API_TOKEN", raising=False)
    monkeypatch.delenv("BITBUCKET_EMAIL", raising=False)
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    env = worker_mod._service_git_environment(
        tmp_path / "host-home",
        github_token="ghp_service_token",
        source_env={
            "BITBUCKET_API_TOKEN": "bb_token",
            "BITBUCKET_EMAIL": "dev@example.com",
            "SSH_AUTH_SOCK": "/run/host-services/ssh-auth.sock",
        },
    )
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    count = int(env["GIT_CONFIG_COUNT"])
    entries = {
        env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"] for index in range(count)
    }
    assert "credential.https://bitbucket.org.helper" in entries
    assert env["SSH_AUTH_SOCK"] == "/run/host-services/ssh-auth.sock"
    assert "IdentityAgent=/run/host-services/ssh-auth.sock" in env["GIT_SSH_COMMAND"]


@pytest.mark.unit
def test_service_git_environment_source_env_overrides_caller_environ(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("BITBUCKET_API_TOKEN", "caller_token")
    monkeypatch.setenv("BITBUCKET_EMAIL", "caller@example.com")
    env = worker_mod._service_git_environment(
        tmp_path / "host-home", github_token="ghp_service_token", source_env={}
    )
    assert "GIT_TERMINAL_PROMPT" not in env
    count = int(env["GIT_CONFIG_COUNT"])
    entries = {
        env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"] for index in range(count)
    }
    assert {key for key, _ in bitbucket_git_config_entries()}.isdisjoint(entries)
