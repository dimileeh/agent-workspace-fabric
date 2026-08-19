"""Regression coverage for the replaceable local-service Git-config source."""

from __future__ import annotations

import asyncio
import dataclasses
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

import pytest

from awf.service import worker as worker_mod
from awf.service.gitconfig_source import (
    GitconfigSourceServer,
    request_gitconfig_source_refresh,
)
from tests.unit.service.test_worker import _settings
from tests.unit.service.test_worker_runtime_wiring import _stub_worker_runtime_dependencies


@pytest.mark.unit
def test_gitconfig_source_refresh_observes_atomic_host_replacement(tmp_path: Path) -> None:
    """A request rereads the directory-mounted name instead of a pinned inode."""
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    host_home.mkdir()
    source = host_home / ".gitconfig"
    source.write_text("[user]\n  name = Before\n", encoding="utf-8")
    socket_path = work_dir / "service-auth" / "gitconfig-source.sock"

    async def exercise() -> tuple[Path | None, Path | None]:
        server = GitconfigSourceServer(
            host_home=host_home,
            work_dir=work_dir,
            socket_path=socket_path,
        )
        server_task = asyncio.create_task(server.serve())
        try:
            await server.ready.wait()
            first = await request_gitconfig_source_refresh(socket_path)
            replacement = host_home / ".gitconfig.next"
            replacement.write_text("[user]\n  name = After\n", encoding="utf-8")
            replacement.replace(source)
            second = await request_gitconfig_source_refresh(socket_path)
            return first, second
        finally:
            server_task.cancel()
            with suppress(asyncio.CancelledError):
                await server_task

    first, second = asyncio.run(exercise())

    assert first is not None
    assert second is not None
    assert first != second
    assert first.read_text(encoding="utf-8") == "[user]\n  name = Before\n"
    assert second.read_text(encoding="utf-8") == "[user]\n  name = After\n"
    assert (socket_path.parent / "current").resolve() == second.parent


@pytest.mark.unit
def test_gitconfig_source_resolves_absolute_symlink_through_host_root(
    tmp_path: Path,
) -> None:
    """An external absolute host symlink is read through the helper mount."""
    host_root = tmp_path / "host-root"
    host_home = host_root / "home" / "agent"
    external_config = host_root / "nix" / "store" / "profile" / "gitconfig"
    actual_config = host_root / "nix" / "store" / "actual" / "gitconfig"
    host_home.mkdir(parents=True)
    external_config.parent.mkdir(parents=True)
    actual_config.parent.mkdir(parents=True)
    actual_config.write_text("[include]\n  path = identity.inc\n", encoding="utf-8")
    external_config.symlink_to("../actual/gitconfig")
    (host_home / "identity.inc").write_text(
        "[user]\n  email = agent@example.com\n",
        encoding="utf-8",
    )
    (host_home / ".gitconfig").symlink_to("/nix/store/profile/gitconfig")
    server = GitconfigSourceServer(
        host_home=host_home,
        host_root=host_root,
        logical_host_home=Path("/home/agent"),
        work_dir=tmp_path / "work",
        socket_path=tmp_path / "work" / "service-auth" / "source.sock",
    )

    snapshot = server.refresh()

    assert snapshot is not None
    result = subprocess.run(
        ["git", "config", "--file", str(snapshot), "--includes", "user.email"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "agent@example.com"


@pytest.mark.unit
def test_gitconfig_source_resolves_intermediate_absolute_symlink_through_host_root(
    tmp_path: Path,
) -> None:
    """A relative include below an absolute host directory link is preserved."""
    host_root = tmp_path / "host-root"
    host_home = host_root / "home" / "agent"
    identity = host_root / "nix" / "store" / "profile" / "git" / "identity.inc"
    host_home.mkdir(parents=True)
    identity.parent.mkdir(parents=True)
    (host_home / ".gitconfig").write_text(
        "[include]\n  path = .config/git/identity.inc\n",
        encoding="utf-8",
    )
    identity.write_text("[user]\n  email = agent@example.com\n", encoding="utf-8")
    (host_home / ".config").symlink_to("/nix/store/profile")
    server = GitconfigSourceServer(
        host_home=host_home,
        host_root=host_root,
        logical_host_home=Path("/home/agent"),
        work_dir=tmp_path / "work",
        socket_path=tmp_path / "work" / "service-auth" / "source.sock",
    )

    snapshot = server.refresh()

    assert snapshot is not None
    result = subprocess.run(
        ["git", "config", "--file", str(snapshot), "--includes", "user.email"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "agent@example.com"


@pytest.mark.unit
def test_gitconfig_source_treats_host_root_symlink_cycle_as_absent(tmp_path: Path) -> None:
    """A cyclic host symlink cannot wedge the refresh helper."""
    host_root = tmp_path / "host-root"
    host_home = host_root / "home" / "agent"
    first = host_root / "nix" / "store" / "first"
    second = host_root / "nix" / "store" / "second"
    host_home.mkdir(parents=True)
    first.parent.mkdir(parents=True)
    (host_home / ".gitconfig").symlink_to("/nix/store/first")
    first.symlink_to("/nix/store/second")
    second.symlink_to("/nix/store/first")
    server = GitconfigSourceServer(
        host_home=host_home,
        host_root=host_root,
        work_dir=tmp_path / "work",
        socket_path=tmp_path / "work" / "service-auth" / "source.sock",
    )

    assert server.refresh() is None


@pytest.mark.unit
def test_gitconfig_source_rewrites_relative_gitdirs_to_logical_host_home(
    tmp_path: Path,
) -> None:
    """Helper-only mount paths must not leak into worker-consumed conditions."""
    mounted_home = tmp_path / "mounted-host-home"
    logical_home = tmp_path / "logical-host-home"
    mounted_home.mkdir()
    (mounted_home / ".gitconfig").write_text(
        '[includeIf "gitdir:./repos/"]\n'
        "  path = identities/top.inc\n"
        "[include]\n"
        "  path = configs/conditions.inc\n",
        encoding="utf-8",
    )
    (mounted_home / "configs").mkdir()
    (mounted_home / "configs" / "conditions.inc").write_text(
        '[includeIf "gitdir/i:./Repos/"]\n  path = ../identities/nested.inc\n',
        encoding="utf-8",
    )
    (mounted_home / "identities").mkdir()
    (mounted_home / "identities" / "top.inc").write_text(
        "[user]\n  email = top@example.com\n",
        encoding="utf-8",
    )
    (mounted_home / "identities" / "nested.inc").write_text(
        "[user]\n  email = nested@example.com\n",
        encoding="utf-8",
    )
    top_repo = logical_home / "repos" / "top"
    nested_repo = logical_home / "configs" / "repos" / "nested"
    subprocess.run(["git", "init", "--quiet", str(top_repo)], check=True)
    subprocess.run(["git", "init", "--quiet", str(nested_repo)], check=True)
    server = GitconfigSourceServer(
        host_home=mounted_home,
        logical_host_home=logical_home,
        work_dir=tmp_path / "work",
        socket_path=tmp_path / "work" / "service-auth" / "source.sock",
    )

    snapshot = server.refresh()

    assert snapshot is not None
    assert str(mounted_home) not in snapshot.read_text(encoding="utf-8")
    assert f'[includeIf "gitdir:{logical_home}/repos/"]' in snapshot.read_text(
        encoding="utf-8",
    )
    nested_config = snapshot.parent / "configs" / "conditions.inc"
    assert f'[includeIf "gitdir/i:{logical_home}/configs/Repos/"]' in nested_config.read_text(
        encoding="utf-8",
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
def test_gitconfig_source_refresh_publishes_empty_home_without_config(tmp_path: Path) -> None:
    """Workers get a stable absent-config source instead of a dangling pointer."""
    server = GitconfigSourceServer(
        host_home=tmp_path / "host-home",
        work_dir=tmp_path / "work",
        socket_path=tmp_path / "work" / "service-auth" / "gitconfig-source.sock",
    )
    server.host_home.mkdir()

    assert server.refresh() is None
    assert (server.socket_path.parent / "current").resolve().is_dir()


@pytest.mark.unit
def test_gitconfig_source_server_reports_absent_config(tmp_path: Path) -> None:
    """An on-demand refresh explicitly tells the worker no config is present."""
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    socket_path = tmp_path / "socket" / "source.sock"

    async def exercise() -> Path | None:
        server = GitconfigSourceServer(
            host_home=host_home,
            work_dir=tmp_path / "work",
            socket_path=socket_path,
        )
        server_task = asyncio.create_task(server.serve())
        try:
            await server.ready.wait()
            return await request_gitconfig_source_refresh(socket_path)
        finally:
            server_task.cancel()
            with suppress(asyncio.CancelledError):
                await server_task

    assert asyncio.run(exercise()) is None


@pytest.mark.unit
def test_gitconfig_source_server_rejects_unknown_request(tmp_path: Path) -> None:
    """The private socket accepts only the fixed refresh operation."""
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    socket_path = tmp_path / "socket" / "source.sock"

    async def exercise() -> str:
        server = GitconfigSourceServer(
            host_home=host_home,
            work_dir=tmp_path / "work",
            socket_path=socket_path,
        )
        server_task = asyncio.create_task(server.serve())
        try:
            await server.ready.wait()
            reader, writer = await asyncio.open_unix_connection(str(socket_path))
            writer.write(b"unknown\n")
            await writer.drain()
            response = (await reader.readline()).decode().strip()
            writer.close()
            await writer.wait_closed()
            return response
        finally:
            server_task.cancel()
            with suppress(asyncio.CancelledError):
                await server_task

    assert asyncio.run(exercise()) == "error: invalid request"


@pytest.mark.unit
def test_gitconfig_source_server_refuses_non_socket_path(tmp_path: Path) -> None:
    """A bad override cannot make startup unlink an arbitrary existing file."""
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    socket_path = tmp_path / "socket" / "source.sock"
    socket_path.parent.mkdir()
    socket_path.write_text("keep", encoding="utf-8")
    server = GitconfigSourceServer(
        host_home=host_home,
        work_dir=tmp_path / "work",
        socket_path=socket_path,
    )

    async def exercise() -> None:
        await asyncio.wait_for(server.serve(), timeout=0.1)

    with pytest.raises(FileExistsError, match="not a Unix socket"):
        asyncio.run(exercise())
    assert socket_path.read_text(encoding="utf-8") == "keep"


@pytest.mark.unit
def test_gitconfig_source_starts_and_reports_invalid_host_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A bad host config remains observable without wedging worker startup."""
    from awf.service import gitconfig_source as source_mod

    def _invalid_config(**_kwargs: object) -> None:
        raise RuntimeError("invalid host config")

    monkeypatch.setattr(source_mod, "materialize_service_gitconfig", _invalid_config)
    socket_path = tmp_path / "socket" / "source.sock"

    async def exercise() -> tuple[bool, str]:
        server = GitconfigSourceServer(
            host_home=tmp_path / "host-home",
            work_dir=tmp_path / "work",
            socket_path=socket_path,
        )
        server_task = asyncio.create_task(server.serve())
        try:
            await server.ready.wait()
            try:
                await request_gitconfig_source_refresh(socket_path)
            except RuntimeError as exc:
                return (socket_path.parent / "current").resolve().is_dir(), str(exc)
            raise AssertionError("invalid config refresh should fail")
        finally:
            server_task.cancel()
            with suppress(asyncio.CancelledError):
                await server_task

    has_empty_source, error = asyncio.run(exercise())

    assert has_empty_source is True
    assert error == "error: invalid host config"


@pytest.mark.unit
def test_worker_requests_replaceable_source_and_preserves_it_on_helper_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The provision callback refreshes the helper before materializing again."""
    created: dict[str, object] = {}
    materialized_homes: list[Path] = []
    socket_path = tmp_path / "work" / "service-auth" / "gitconfig-source.sock"
    helper_snapshot = (
        tmp_path / "work" / "service-auth" / "gitconfig-snapshots" / "new" / "home" / ".gitconfig"
    )

    _stub_worker_runtime_dependencies(
        monkeypatch,
        created,
        forge_client=object(),
        build_feature=lambda **_kwargs: object(),
        build_release=lambda **_kwargs: object(),
    )

    class _GitManager:
        def __init__(self, _work_dir: Path, *, env: dict[str, str], **_kwargs: object) -> None:
            pass

        def replace_env(self, _env: dict[str, str]) -> None:
            pass

        def set_task_env(self, _env: dict[str, str]) -> object:
            return object()

        def reset_task_env(self, _token: object) -> None:
            pass

    class _AuthMountResolver:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def replace_gitconfig_source(self, _source: Path | None) -> None:
            pass

        def set_task_gitconfig_source(self, _source: Path | None) -> object:
            return object()

        def reset_task_gitconfig_source(self, _token: object) -> None:
            pass

    class _Provisioner:
        def __init__(
            self,
            *,
            before_provision: object,
            after_provision: object,
            **_kwargs: object,
        ) -> None:
            created["before_provision"] = before_provision
            created["after_provision"] = after_provision

    def _materialize(*, host_home: Path, **_kwargs: object) -> Path:
        materialized_homes.append(host_home)
        snapshot = tmp_path / f"snapshot-{len(materialized_homes)}"
        snapshot.write_text("[user]\n  name = Snapshot\n", encoding="utf-8")
        return snapshot

    refresh_requests: list[Path] = []

    async def _request(path: Path) -> Path:
        refresh_requests.append(path)
        if len(refresh_requests) == 2:
            raise OSError("helper unavailable")
        return helper_snapshot

    monkeypatch.setattr(worker_mod, "GitManager", _GitManager)
    monkeypatch.setattr(worker_mod, "ServiceAuthMountResolver", _AuthMountResolver)
    monkeypatch.setattr(worker_mod, "Provisioner", _Provisioner)
    monkeypatch.setattr(worker_mod, "_materialize_service_gitconfig", _materialize)
    monkeypatch.setattr(worker_mod, "_request_gitconfig_source_refresh", _request)
    settings = dataclasses.replace(_settings(tmp_path), gitconfig_source_socket=str(socket_path))

    worker_mod.build_worker_runtime(settings)

    async def exercise() -> None:
        await created["before_provision"]()  # type: ignore[operator]
        await created["after_provision"]()  # type: ignore[operator]
        await created["before_provision"]()  # type: ignore[operator]
        await created["after_provision"]()  # type: ignore[operator]

    asyncio.run(exercise())

    assert refresh_requests == [socket_path, socket_path]
    assert materialized_homes == [socket_path.parent / "current", helper_snapshot.parent]


@pytest.mark.unit
def test_gitconfig_source_main_wires_compose_arguments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The module entry point accepts the exact arguments used by Compose."""
    from awf.service import gitconfig_source as source_mod

    created: dict[str, Path] = {}

    class _Server:
        def __init__(
            self,
            *,
            host_home: Path,
            host_root: Path,
            logical_host_home: Path,
            work_dir: Path,
            socket_path: Path,
        ) -> None:
            created.update(
                host_home=host_home,
                host_root=host_root,
                logical_host_home=logical_host_home,
                work_dir=work_dir,
                socket_path=socket_path,
            )

        async def serve(self) -> None:
            created["served"] = Path("yes")

    monkeypatch.setattr(source_mod, "GitconfigSourceServer", _Server)
    mounted_root = tmp_path / "mounted-root"
    logical_home = tmp_path / "logical" / "nested" / "home"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gitconfig_source.py",
            "--host-root",
            str(mounted_root),
            "--logical-host-home",
            str(logical_home),
            "--work-dir",
            str(tmp_path / "work"),
            "--socket",
            str(tmp_path / "socket"),
        ],
    )

    source_mod.main()

    assert created == {
        "host_home": mounted_root / logical_home.relative_to("/"),
        "host_root": mounted_root,
        "logical_host_home": logical_home,
        "work_dir": tmp_path / "work",
        "socket_path": tmp_path / "socket",
        "served": Path("yes"),
    }
