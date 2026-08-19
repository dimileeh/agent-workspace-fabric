"""Service worker git-environment wiring tests.

Split out of :mod:`tests.unit.service.test_worker` to keep that module under the
first-party file-size guardrail. These exercise ``_service_git_environment`` --
the pure HOME/SSH/credential-helper env builder -- in isolation.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import threading
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path

import pytest

from awf.common.git_auth import bitbucket_git_config_entries
from awf.service import gitconfig_snapshot as gitconfig_snapshot_mod
from awf.service import worker as worker_mod


@pytest.fixture(autouse=True)
def _isolate_bundle_leases() -> Iterator[None]:
    registry = gitconfig_snapshot_mod._ACTIVE_BUNDLE_LEASES
    original = dict(registry)
    try:
        yield
    finally:
        for bundle, lease in registry.items():
            if original.get(bundle) is not lease:
                with suppress(OSError):
                    lease.close()
        registry.clear()
        registry.update(original)


@pytest.mark.unit
def test_service_gitconfig_snapshot_survives_host_atomic_replacement(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    host_home.mkdir()
    source = host_home / ".gitconfig"
    source.write_text("[user]\n  name = Original\n")

    snapshot = worker_mod._materialize_service_gitconfig(
        host_home=host_home,
        work_dir=work_dir,
    )

    assert snapshot.parent.parent.parent == work_dir / "service-auth" / "gitconfig-snapshots"
    assert snapshot.name == ".gitconfig"
    assert snapshot.read_text() == "[user]\n  name = Original\n"
    assert snapshot.stat().st_mode & 0o777 == 0o600
    assert snapshot.parent.parent in gitconfig_snapshot_mod._ACTIVE_BUNDLE_LEASES

    replacement = host_home / ".gitconfig.next"
    replacement.write_text("[user]\n  name = Replacement\n")
    replacement.replace(source)
    source.unlink()

    env = worker_mod._service_git_environment(host_home, gitconfig_path=snapshot)

    assert env["GIT_CONFIG_GLOBAL"] == str(snapshot)
    assert snapshot.read_text() == "[user]\n  name = Original\n"


@pytest.mark.unit
def test_service_gitconfig_snapshot_reuses_immutable_content_addressed_inode(
    tmp_path: Path,
) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    host_home.mkdir()
    source = host_home / ".gitconfig"
    source.write_text("[user]\n  name = Original\n")

    first = worker_mod._materialize_service_gitconfig(
        host_home=host_home,
        work_dir=work_dir,
    )
    assert first is not None
    first_inode = first.stat().st_ino

    second = worker_mod._materialize_service_gitconfig(
        host_home=host_home,
        work_dir=work_dir,
    )

    assert second == first
    assert second.stat().st_ino == first_inode

    source.write_text("[user]\n  name = Replacement\n")
    replacement = worker_mod._materialize_service_gitconfig(
        host_home=host_home,
        work_dir=work_dir,
    )

    assert replacement is not None
    assert replacement != first
    assert replacement.read_text() == "[user]\n  name = Replacement\n"
    assert first.read_text() == "[user]\n  name = Original\n"
    assert first.stat().st_ino == first_inode


@pytest.mark.unit
def test_service_gitconfig_snapshot_adds_lease_to_prelease_bundle(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    host_home.mkdir()
    (host_home / ".gitconfig").write_text("[user]\n  name = Original\n")
    snapshot = worker_mod._materialize_service_gitconfig(
        host_home=host_home,
        work_dir=work_dir,
    )
    assert snapshot is not None
    bundle_root = snapshot.parent.parent
    held_lease = gitconfig_snapshot_mod._ACTIVE_BUNDLE_LEASES.pop(bundle_root)
    held_lease.close()
    (bundle_root / "worker.lock").unlink()

    reused = worker_mod._materialize_service_gitconfig(
        host_home=host_home,
        work_dir=work_dir,
    )

    assert reused == snapshot
    assert (bundle_root / "worker.lock").stat().st_mode & 0o777 == 0o600
    assert bundle_root in gitconfig_snapshot_mod._ACTIVE_BUNDLE_LEASES


@pytest.mark.unit
def test_service_gitconfig_snapshot_preserves_relative_include_origin(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    (host_home / ".gitconfig").write_text("[include]\n  path = identity.inc\n")
    (host_home / "identity.inc").write_text("[user]\n  email = agent@example.com\n")

    snapshot = worker_mod._materialize_service_gitconfig(
        host_home=host_home,
        work_dir=tmp_path / "work",
    )

    assert snapshot is not None
    result = subprocess.run(
        ["git", "config", "--file", str(snapshot), "--includes", "user.email"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "agent@example.com"

    agent_config = snapshot.parent.parent / "agent.gitconfig"
    assert agent_config.read_text() == "[include]\n  path = identity.inc\n"
    agent_result = subprocess.run(
        ["git", "config", "--file", str(agent_config), "--includes", "user.email"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert agent_result.returncode == 1
    assert agent_result.stdout == ""


@pytest.mark.unit
def test_service_gitconfig_snapshot_preserves_external_relative_include(
    tmp_path: Path,
) -> None:
    host_home = tmp_path / "host-home"
    shared_config = tmp_path / "shared-config"
    host_home.mkdir()
    shared_config.mkdir()
    (host_home / ".gitconfig").write_text("[include]\n  path = ../shared-config/base.inc\n")
    (shared_config / "base.inc").write_text(
        "[include]\n  path = nested/identity.inc\n",
    )
    (shared_config / "nested").mkdir()
    (shared_config / "nested" / "identity.inc").write_text(
        "[user]\n  email = external@example.com\n",
    )

    snapshot = worker_mod._materialize_service_gitconfig(
        host_home=host_home,
        work_dir=tmp_path / "work",
    )

    assert snapshot is not None
    assert ".external-includes" in snapshot.read_text()
    external_copies = tuple((snapshot.parent / ".external-includes").iterdir())
    assert len(external_copies) == 2
    assert not (snapshot.parent.parent / "shared-config").exists()
    result = subprocess.run(
        ["git", "config", "--file", str(snapshot), "--includes", "user.email"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "external@example.com"

    agent_config = snapshot.parent.parent / "agent.gitconfig"
    agent_result = subprocess.run(
        ["git", "config", "--file", str(agent_config), "--includes", "user.email"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert agent_result.returncode == 1
    assert agent_result.stdout == ""


@pytest.mark.unit
def test_service_gitconfig_snapshot_preserves_symlinked_relative_include_path(
    tmp_path: Path,
) -> None:
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    (host_home / ".gitconfig").write_text("[include]\n  path = parts/identity.inc\n")
    (host_home / "parts").mkdir()
    (host_home / "actual.inc").write_text("[user]\n  email = symlink@example.com\n")
    (host_home / "parts" / "identity.inc").symlink_to(host_home / "actual.inc")

    snapshot = worker_mod._materialize_service_gitconfig(
        host_home=host_home,
        work_dir=tmp_path / "work",
    )

    assert snapshot is not None
    included_snapshot = snapshot.parent / "parts" / "identity.inc"
    assert included_snapshot.read_text() == "[user]\n  email = symlink@example.com\n"
    result = subprocess.run(
        ["git", "config", "--file", str(snapshot), "--includes", "user.email"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "symlink@example.com"


@pytest.mark.unit
def test_service_gitconfig_snapshot_preserves_each_symlinked_include_alias(
    tmp_path: Path,
) -> None:
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    (host_home / ".gitconfig").write_text(
        '[includeIf "gitdir:./repos/active/"]\n'
        "  path = identities/active.inc\n"
        '[includeIf "gitdir:./repos/inactive/"]\n'
        "  path = identities/inactive.inc\n",
    )
    (host_home / "identities").mkdir()
    (host_home / "identity.inc").write_text("[user]\n  email = alias@example.com\n")
    (host_home / "identities" / "active.inc").symlink_to(host_home / "identity.inc")
    (host_home / "identities" / "inactive.inc").symlink_to(host_home / "identity.inc")
    active_repo = host_home / "repos" / "active" / "project"
    subprocess.run(["git", "init", "--quiet", str(active_repo)], check=True)

    snapshot = worker_mod._materialize_service_gitconfig(
        host_home=host_home,
        work_dir=tmp_path / "work",
    )

    assert snapshot is not None
    assert (snapshot.parent / "identities" / "active.inc").is_file()
    assert (snapshot.parent / "identities" / "inactive.inc").is_file()
    result = subprocess.run(
        [
            "git",
            "-C",
            str(active_repo),
            "config",
            "--file",
            str(snapshot),
            "--includes",
            "user.email",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "alias@example.com"


@pytest.mark.unit
def test_service_gitconfig_snapshot_stops_symlinked_include_cycles(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    (host_home / ".gitconfig").write_text("[include]\n  path = again/.gitconfig\n")
    (host_home / "again").symlink_to(host_home, target_is_directory=True)

    snapshot = worker_mod._materialize_service_gitconfig(
        host_home=host_home,
        work_dir=tmp_path / "work",
    )

    assert snapshot is not None
    assert snapshot.read_text() == "[include]\n  path = again/.gitconfig\n"


@pytest.mark.unit
def test_service_gitconfig_snapshot_preserves_relative_gitdir_conditions(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    (host_home / ".gitconfig").write_text(
        '[includeIf "gitdir:./repos/"]\n'
        "  path = identities/top.inc\n"
        "[include]\n"
        "  path = configs/conditions.inc\n",
    )
    (host_home / "configs").mkdir()
    (host_home / "configs" / "conditions.inc").write_text(
        '[includeIf "gitdir/i:./Repos/"]\n  path = ../identities/nested.inc\n',
    )
    (host_home / "identities").mkdir()
    (host_home / "identities" / "top.inc").write_text(
        "[user]\n  email = top@example.com\n",
    )
    (host_home / "identities" / "nested.inc").write_text(
        "[user]\n  email = nested@example.com\n",
    )
    top_repo = host_home / "repos" / "top"
    nested_repo = host_home / "configs" / "repos" / "nested"
    subprocess.run(["git", "init", "--quiet", str(top_repo)], check=True)
    subprocess.run(["git", "init", "--quiet", str(nested_repo)], check=True)

    snapshot = worker_mod._materialize_service_gitconfig(
        host_home=host_home,
        work_dir=tmp_path / "work",
    )

    assert snapshot is not None
    rewritten_top = f'[includeIf "gitdir:{host_home}/repos/"]'
    assert rewritten_top in snapshot.read_text()
    assert rewritten_top in (snapshot.parent.parent / "agent.gitconfig").read_text()
    assert (
        f'[includeIf "gitdir/i:{host_home}/configs/Repos/"]'
        in (snapshot.parent / "configs" / "conditions.inc").read_text()
    )
    for repo, expected_email in (
        (top_repo, "top@example.com"),
        (nested_repo, "nested@example.com"),
    ):
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "config",
                "--file",
                str(snapshot),
                "--includes",
                "user.email",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected_email


@pytest.mark.unit
def test_service_gitconfig_snapshot_is_absent_without_host_config(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    host_home.mkdir()

    assert (
        worker_mod._materialize_service_gitconfig(
            host_home=host_home,
            work_dir=tmp_path / "work",
        )
        is None
    )


@pytest.mark.unit
def test_service_gitconfig_snapshot_reaps_only_unreferenced_stale_bundles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_dir = tmp_path / "work"
    snapshots_root = work_dir / "service-auth" / "gitconfig-snapshots"
    snapshots_root.mkdir(parents=True)
    bundles = [snapshots_root / f"{index:064x}" for index in range(12)]
    for bundle in bundles:
        bundle.mkdir()
        (bundle / "worker.lock").touch()

    current = bundles[-1]
    compose_referenced = bundles[0]
    container_referenced = bundles[1]
    compose_file = work_dir / "compose" / "ws_active" / "compose.yml"
    compose_file.parent.mkdir(parents=True)
    compose_file.write_text(f"source: {compose_referenced}/agent.gitconfig\n")
    monkeypatch.setattr(
        gitconfig_snapshot_mod,
        "_running_container_mount_sources",
        lambda: frozenset({container_referenced / "agent.gitconfig"}),
    )

    gitconfig_snapshot_mod._reap_stale_gitconfig_bundles(
        snapshots_root=snapshots_root,
        work_dir=work_dir,
        protected_root=current,
        now=10**10,
    )

    assert current.is_dir()
    assert compose_referenced.is_dir()
    assert container_referenced.is_dir()
    assert not bundles[2].exists()


@pytest.mark.unit
def test_service_gitconfig_snapshot_skips_cleanup_when_docker_state_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_dir = tmp_path / "work"
    snapshots_root = work_dir / "service-auth" / "gitconfig-snapshots"
    stale = snapshots_root / ("a" * 64)
    current = snapshots_root / ("b" * 64)
    stale.mkdir(parents=True)
    current.mkdir()
    monkeypatch.setattr(
        gitconfig_snapshot_mod,
        "_running_container_mount_sources",
        lambda: None,
    )

    gitconfig_snapshot_mod._reap_stale_gitconfig_bundles(
        snapshots_root=snapshots_root,
        work_dir=work_dir,
        protected_root=current,
        now=10**10,
    )

    assert stale.is_dir()


@pytest.mark.unit
def test_service_gitconfig_snapshot_discovers_container_mount_from_stopped_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    responses = iter(
        (
            subprocess.CompletedProcess(
                args=["docker", "container", "ls", "--all", "--quiet"],
                returncode=0,
                stdout="container-id\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["docker", "container", "inspect", "container-id"],
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "Mounts": [{"Source": "/work/bundle/agent.gitconfig"}],
                            "Config": {"Env": ["OTHER=value"]},
                        }
                    ]
                ),
                stderr="",
            ),
        )
    )

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return next(responses)

    monkeypatch.setattr(
        gitconfig_snapshot_mod.subprocess,
        "run",
        run,
    )

    sources = gitconfig_snapshot_mod._running_container_mount_sources()

    assert commands[0] == ["docker", "container", "ls", "--all", "--quiet"]
    assert sources == frozenset({Path("/work/bundle/agent.gitconfig")})


@pytest.mark.unit
def test_service_gitconfig_snapshot_cleanup_error_does_not_break_worker_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_dir = tmp_path / "work"
    snapshots_root = work_dir / "service-auth" / "gitconfig-snapshots"
    snapshots_root.mkdir(parents=True)
    bundles = [snapshots_root / f"{index:064x}" for index in range(9)]
    for bundle in bundles:
        bundle.mkdir()
        (bundle / "worker.lock").touch()
    monkeypatch.setattr(
        gitconfig_snapshot_mod,
        "_running_container_mount_sources",
        lambda: frozenset(),
    )
    monkeypatch.setattr(
        gitconfig_snapshot_mod.shutil,
        "rmtree",
        lambda _path: (_ for _ in ()).throw(OSError("concurrent cleanup")),
    )

    gitconfig_snapshot_mod._reap_stale_gitconfig_bundles(
        snapshots_root=snapshots_root,
        work_dir=work_dir,
        protected_root=bundles[-1],
        now=10**10,
    )

    assert bundles[0].is_dir()


@pytest.mark.unit
def test_service_gitconfig_snapshot_retains_bundle_with_live_worker_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_dir = tmp_path / "work"
    snapshots_root = work_dir / "service-auth" / "gitconfig-snapshots"
    snapshots_root.mkdir(parents=True)
    bundles = [snapshots_root / f"{index:064x}" for index in range(9)]
    for bundle in bundles:
        bundle.mkdir()
        (bundle / "worker.lock").touch()
    leased = bundles[0]
    monkeypatch.setattr(
        gitconfig_snapshot_mod,
        "_running_container_mount_sources",
        lambda: frozenset(),
    )

    with (leased / "worker.lock").open("r+b") as lease:
        fcntl.flock(lease.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        gitconfig_snapshot_mod._reap_stale_gitconfig_bundles(
            snapshots_root=snapshots_root,
            work_dir=work_dir,
            protected_root=bundles[-1],
            now=10**10,
        )

    assert leased.is_dir()


@pytest.mark.unit
def test_service_gitconfig_snapshot_cleanup_tolerates_disappearing_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_dir = tmp_path / "work"
    snapshots_root = work_dir / "service-auth" / "gitconfig-snapshots"
    snapshots_root.mkdir(parents=True)
    disappearing = snapshots_root / ("a" * 64)
    current = snapshots_root / ("b" * 64)
    disappearing.mkdir()
    current.mkdir()
    monkeypatch.setattr(
        gitconfig_snapshot_mod,
        "_running_container_mount_sources",
        lambda: frozenset(),
    )
    original_stat = Path.stat
    disappearing_stat_calls = 0

    def flaky_stat(path: Path, *args: object, **kwargs: object) -> os.stat_result:
        nonlocal disappearing_stat_calls
        if path == disappearing:
            disappearing_stat_calls += 1
            if disappearing_stat_calls > 1:
                raise FileNotFoundError(path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)

    gitconfig_snapshot_mod._reap_stale_gitconfig_bundles(
        snapshots_root=snapshots_root,
        work_dir=work_dir,
        protected_root=current,
        now=10**10,
    )

    assert disappearing_stat_calls > 1
    assert current.is_dir()


@pytest.mark.unit
def test_service_gitconfig_snapshot_serializes_publication_and_cleanup(tmp_path: Path) -> None:
    snapshots_root = tmp_path / "gitconfig-snapshots"
    snapshots_root.mkdir()
    competing_started = threading.Event()
    competing_acquired = threading.Event()

    def competing_operation() -> None:
        competing_started.set()
        with gitconfig_snapshot_mod._snapshot_coordination_lock(snapshots_root):
            competing_acquired.set()

    with gitconfig_snapshot_mod._snapshot_coordination_lock(snapshots_root):
        competitor = threading.Thread(target=competing_operation)
        competitor.start()
        assert competing_started.wait(timeout=1)
        assert not competing_acquired.wait(timeout=0.1)

    competitor.join(timeout=1)
    assert competing_acquired.is_set()


@pytest.mark.unit
def test_service_gitconfig_snapshot_is_owned_by_agent_runtime_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    (host_home / ".gitconfig").write_text("[user]\n  name = AWF\n")
    ownership: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "awf.service.gitconfig_snapshot.os.fchown",
        lambda _fd, uid, gid: ownership.append((uid, gid)),
    )
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    snapshot = worker_mod._materialize_service_gitconfig(
        host_home=host_home,
        work_dir=tmp_path / "work",
        owner_uid=1000,
        owner_gid=1000,
    )

    assert snapshot is not None
    assert ownership
    assert set(ownership) == {(1000, 1000)}
    assert snapshot.stat().st_mode & 0o777 == 0o600


@pytest.mark.unit
def test_service_gitconfig_snapshot_remains_agent_readable_for_non_root_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    (host_home / ".gitconfig").write_text("[user]\n  name = AWF\n")
    monkeypatch.setattr("awf.service.gitconfig_snapshot.os.geteuid", lambda: 501)
    monkeypatch.setattr(
        "awf.service.gitconfig_snapshot.os.fchown",
        lambda *_args: pytest.fail("non-root worker must not attempt fchown"),
    )

    snapshot = worker_mod._materialize_service_gitconfig(
        host_home=host_home,
        work_dir=tmp_path / "work",
        owner_uid=1000,
        owner_gid=1000,
    )

    assert snapshot is not None
    assert snapshot.stat().st_mode & 0o777 == 0o644
    assert snapshot.parent.stat().st_mode & 0o777 == 0o755


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
def test_service_git_environment_forwards_github_token_for_gh_cli(tmp_path: Path) -> None:
    env = worker_mod._service_git_environment(
        tmp_path / "host-home",
        github_token="ghp_service_token",
    )

    assert env["GH_TOKEN"] == "ghp_service_token"
    assert env["GITHUB_TOKEN"] == "ghp_service_token"


@pytest.mark.unit
def test_service_git_environment_marks_worker_managed_worktrees_safe(tmp_path: Path) -> None:
    env = worker_mod._service_git_environment(tmp_path / "host-home")

    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "safe.directory"
    assert env["GIT_CONFIG_VALUE_0"] == "*"


@pytest.mark.unit
def test_service_git_environment_configures_gh_credential_helper_for_git(
    tmp_path: Path,
) -> None:
    env = worker_mod._service_git_environment(
        tmp_path / "host-home",
        github_token="ghp_service_token",
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
        tmp_path / "host-home",
        github_token="ghp_service_token",
    )

    count = int(env["GIT_CONFIG_COUNT"])
    entries = {
        env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"] for index in range(count)
    }
    # Bitbucket host-scoped helper is wired alongside the (unchanged) GitHub one.
    assert "credential.https://bitbucket.org.helper" in entries
    assert entries["credential.https://github.com.helper"] == "!gh auth git-credential"
    assert entries["url.https://github.com/.insteadOf"] == "git@github.com:"
    # SSH-form bitbucket remotes are rewritten to HTTPS so the token is used
    # (parity with the GitHub insteadOf rewrite above). ``insteadOf`` is
    # multi-valued: the scp-like ``git@bitbucket.org:`` form, the no-port
    # ``ssh://git@bitbucket.org/`` form, and the explicit-default-port
    # ``ssh://git@bitbucket.org:22/`` form (which the preflight accepts as
    # canonical) are all covered.
    bitbucket_insteadof = [
        env[f"GIT_CONFIG_VALUE_{index}"]
        for index in range(count)
        if env[f"GIT_CONFIG_KEY_{index}"] == "url.https://bitbucket.org/.insteadOf"
    ]
    assert "git@bitbucket.org:" in bitbucket_insteadof
    assert "ssh://git@bitbucket.org/" in bitbucket_insteadof
    assert "ssh://git@bitbucket.org:22/" in bitbucket_insteadof
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    # The Atlassian token never lands in any git env value.
    assert all(secret_token not in value for value in env.values())


@pytest.mark.unit
def test_service_git_environment_unchanged_without_bitbucket_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("BITBUCKET_API_TOKEN", raising=False)
    monkeypatch.delenv("BITBUCKET_EMAIL", raising=False)

    env = worker_mod._service_git_environment(
        tmp_path / "host-home",
        github_token="ghp_service_token",
    )

    # Pure regression: no bitbucket helper, no terminal-prompt override, and the
    # GitHub credential helper plus safe.directory entries are untouched.
    assert "GIT_TERMINAL_PROMPT" not in env
    count = int(env["GIT_CONFIG_COUNT"])
    entries = {
        env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"] for index in range(count)
    }
    # Compare against the exact bitbucket-scoped config keys rather than a substring
    # of the host (a bare-host substring check is an incomplete-URL-sanitization
    # pattern flagged by static analysis).
    assert {key for key, _ in bitbucket_git_config_entries()}.isdisjoint(entries)
    assert entries["credential.https://github.com.helper"] == "!gh auth git-credential"


@pytest.mark.unit
def test_service_git_environment_reads_bitbucket_and_ssh_from_source_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``source_env`` (not the caller os.environ) drives the Bitbucket/SSH wiring.

    The real worker reads its Compose-forwarded container env; callers that run
    from a different process context (``awf profile doctor``) pass that env via
    ``source_env`` so the Bitbucket-conditional additions match the worker. Here
    the creds and agent socket live ONLY in ``source_env`` and are absent from the
    caller os.environ, yet the Bitbucket helper, GIT_TERMINAL_PROMPT, and the
    SSH_AUTH_SOCK/GIT_SSH_COMMAND wiring are still emitted.
    """
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
    """An explicit ``source_env`` fully replaces os.environ for Bitbucket detection.

    Bitbucket creds in the caller os.environ but absent from ``source_env`` must
    NOT add the Bitbucket helper: the worker context modelled by ``source_env``
    lacks them, so the doctor must not over-add keys the worker would not inject.
    """
    monkeypatch.setenv("BITBUCKET_API_TOKEN", "caller_token")
    monkeypatch.setenv("BITBUCKET_EMAIL", "caller@example.com")

    env = worker_mod._service_git_environment(
        tmp_path / "host-home",
        github_token="ghp_service_token",
        source_env={},
    )

    assert "GIT_TERMINAL_PROMPT" not in env
    count = int(env["GIT_CONFIG_COUNT"])
    entries = {
        env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"] for index in range(count)
    }
    # Compare against the exact bitbucket-scoped config keys rather than a substring
    # of the host (a bare-host substring check is an incomplete-URL-sanitization
    # pattern flagged by static analysis).
    assert {key for key, _ in bitbucket_git_config_entries()}.isdisjoint(entries)
