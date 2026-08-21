"""Worker Git-config snapshot runtime wiring tests."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from awf.service import worker as worker_mod
from tests.unit.service.test_worker import _settings
from tests.unit.service.test_worker_runtime_wiring import _stub_worker_runtime_dependencies


@pytest.mark.unit
@pytest.mark.parametrize(
    "snapshot_error",
    [
        OSError("snapshot filesystem failure"),
        RuntimeError("snapshot validation failure"),
        subprocess.TimeoutExpired(cmd=["git", "config"], timeout=1),
    ],
    ids=["os-error", "runtime-error", "subprocess-error"],
)
def test_build_worker_runtime_falls_back_to_live_gitconfig_when_snapshot_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    snapshot_error: Exception,
) -> None:
    created: dict[str, Any] = {}
    warnings: list[tuple[str, dict[str, object]]] = []
    _stub_worker_runtime_dependencies(
        monkeypatch,
        created,
        forge_client=object(),
        build_feature=lambda **_kwargs: object(),
        build_release=lambda **_kwargs: object(),
    )

    class _RecordingGitManager:
        def __init__(self, _work_dir: Path, *, env: object, **_kwargs: object) -> None:
            created["git_env"] = env

    class _RecordingAuthMountResolver:
        def __init__(self, *, gitconfig_source: Path | None, **_kwargs: object) -> None:
            created["gitconfig_source"] = gitconfig_source

    def _raise_snapshot_error(**_kwargs: object) -> None:
        raise snapshot_error

    monkeypatch.setattr(worker_mod, "GitManager", _RecordingGitManager)
    monkeypatch.setattr(worker_mod, "ServiceAuthMountResolver", _RecordingAuthMountResolver)
    monkeypatch.setattr(worker_mod, "_materialize_service_gitconfig", _raise_snapshot_error)
    monkeypatch.setattr(
        worker_mod,
        "_log",
        SimpleNamespace(warning=lambda event, **kwargs: warnings.append((event, kwargs))),
    )
    settings = _settings(tmp_path)
    host_home = Path(settings.host_home)
    host_home.mkdir(parents=True)
    live_gitconfig = host_home / ".gitconfig"
    live_gitconfig.write_text("[user]\n  name = Live fallback\n")

    assert worker_mod.build_worker_runtime(settings) is not None
    assert created["git_env"]["GIT_CONFIG_GLOBAL"] == str(live_gitconfig)
    assert created["gitconfig_source"] is None
    assert warnings == [
        (
            "worker.gitconfig_snapshot_failed",
            {"error": str(snapshot_error), "fallback": "host_gitconfig"},
        )
    ]


@pytest.mark.unit
def test_build_worker_runtime_warns_when_snapshot_fails_without_host_gitconfig(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    created: dict[str, Any] = {}
    warnings: list[tuple[str, dict[str, object]]] = []
    _stub_worker_runtime_dependencies(
        monkeypatch,
        created,
        forge_client=object(),
        build_feature=lambda **_kwargs: object(),
        build_release=lambda **_kwargs: object(),
    )

    class _RecordingGitManager:
        def __init__(self, _work_dir: Path, *, env: object, **_kwargs: object) -> None:
            created["git_env"] = env

    class _RecordingAuthMountResolver:
        def __init__(self, *, gitconfig_source: Path | None, **_kwargs: object) -> None:
            created["gitconfig_source"] = gitconfig_source

    def _raise_snapshot_error(**_kwargs: object) -> None:
        raise OSError("snapshot missing")

    monkeypatch.setattr(worker_mod, "GitManager", _RecordingGitManager)
    monkeypatch.setattr(worker_mod, "ServiceAuthMountResolver", _RecordingAuthMountResolver)
    monkeypatch.setattr(worker_mod, "_materialize_service_gitconfig", _raise_snapshot_error)
    monkeypatch.setattr(
        worker_mod,
        "_log",
        SimpleNamespace(warning=lambda event, **kwargs: warnings.append((event, kwargs))),
    )
    settings = _settings(tmp_path)
    Path(settings.host_home).mkdir(parents=True)

    assert worker_mod.build_worker_runtime(settings) is not None
    assert "GIT_CONFIG_GLOBAL" not in created["git_env"]
    assert created["gitconfig_source"] is None
    assert warnings == [
        (
            "worker.gitconfig_snapshot_failed",
            {"error": "snapshot missing", "fallback": "no_global_gitconfig"},
        )
    ]


@pytest.mark.unit
def test_build_worker_runtime_warns_when_snapshot_is_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    created: dict[str, Any] = {}
    warnings: list[tuple[str, dict[str, object]]] = []
    _stub_worker_runtime_dependencies(
        monkeypatch,
        created,
        forge_client=object(),
        build_feature=lambda **_kwargs: object(),
        build_release=lambda **_kwargs: object(),
    )

    class _RecordingGitManager:
        def __init__(self, _work_dir: Path, *, env: object, **_kwargs: object) -> None:
            created["git_env"] = env

    class _RecordingAuthMountResolver:
        def __init__(self, *, gitconfig_source: Path | None, **_kwargs: object) -> None:
            created["gitconfig_source"] = gitconfig_source

    monkeypatch.setattr(worker_mod, "GitManager", _RecordingGitManager)
    monkeypatch.setattr(worker_mod, "ServiceAuthMountResolver", _RecordingAuthMountResolver)
    monkeypatch.setattr(worker_mod, "_materialize_service_gitconfig", lambda **_kwargs: None)
    monkeypatch.setattr(
        worker_mod,
        "_log",
        SimpleNamespace(warning=lambda event, **kwargs: warnings.append((event, kwargs))),
    )
    settings = _settings(tmp_path)
    host_home = Path(settings.host_home)
    host_home.mkdir(parents=True)

    assert worker_mod.build_worker_runtime(settings) is not None
    assert "GIT_CONFIG_GLOBAL" not in created["git_env"]
    assert created["gitconfig_source"] is None
    assert warnings == [
        (
            "worker.gitconfig_snapshot_unavailable",
            {
                "source_home": str(host_home),
                "fallback": "no_global_gitconfig",
            },
        )
    ]


@pytest.mark.unit
def test_build_worker_runtime_preserves_gitconfig_consumers_when_refresh_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    created: dict[str, Any] = {}
    snapshots: Any = iter(
        (tmp_path / "snapshot-old", OSError("refresh failed"), tmp_path / "snapshot-new")
    )
    applied_envs: list[dict[str, str]] = []
    _stub_worker_runtime_dependencies(
        monkeypatch,
        created,
        forge_client=object(),
        build_feature=lambda **_kwargs: object(),
        build_release=lambda **_kwargs: object(),
    )

    class _RecordingGitManager:
        def __init__(self, _work_dir: Path, *, env: dict[str, str], **_kwargs: object) -> None:
            self.envs = [env]
            self.task_envs: dict[asyncio.Task[Any], dict[str, str]] = {}
            created["git"] = self

        def replace_env(self, env: dict[str, str]) -> None:
            self.envs.append(env)

        def set_task_env(self, env: dict[str, str]) -> asyncio.Task[Any]:
            task = asyncio.current_task()
            assert task is not None
            self.task_envs[task] = env
            return task

        def reset_task_env(self, token: asyncio.Task[Any]) -> None:
            self.task_envs.pop(token)

        def current_gitconfig(self) -> str:
            task = asyncio.current_task()
            return self.task_envs.get(task, self.envs[-1])["GIT_CONFIG_GLOBAL"]

    class _RecordingAuthMountResolver:
        def __init__(self, *, gitconfig_source: Path | None, **_kwargs: object) -> None:
            self.sources = [gitconfig_source]
            self.task_sources: dict[asyncio.Task[Any], Path | None] = {}
            created["auth_mount_resolver"] = self

        def replace_gitconfig_source(self, source: Path | None) -> None:
            self.sources.append(source)

        def set_task_gitconfig_source(self, source: Path | None) -> asyncio.Task[Any]:
            task = asyncio.current_task()
            assert task is not None
            self.task_sources[task] = source
            return task

        def reset_task_gitconfig_source(self, token: asyncio.Task[Any]) -> None:
            self.task_sources.pop(token)

        def current_source(self) -> Path | None:
            task = asyncio.current_task()
            return self.task_sources.get(task, self.sources[-1])

    class _RecordingProvisioner:
        def __init__(
            self, *, before_provision: Any, after_provision: Any, **_kwargs: object
        ) -> None:
            created["before_provision"] = before_provision
            created["after_provision"] = after_provision

    def _materialize(**_kwargs: object) -> Path:
        snapshot = next(snapshots)
        if isinstance(snapshot, OSError):
            raise snapshot
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text("[user]\n  name = Snapshot\n")
        return snapshot

    monkeypatch.setattr(worker_mod, "GitManager", _RecordingGitManager)
    monkeypatch.setattr(worker_mod, "ServiceAuthMountResolver", _RecordingAuthMountResolver)
    monkeypatch.setattr(worker_mod, "Provisioner", _RecordingProvisioner)
    monkeypatch.setattr(worker_mod, "_materialize_service_gitconfig", _materialize)
    monkeypatch.setattr(worker_mod, "_apply_service_git_environment", applied_envs.append)
    released_protections: list[frozenset[Path | None]] = []
    released_roots: list[Path] = []

    def _record_released_leases(
        *, snapshots_root: Path, protected_configs: tuple[Path | None, ...]
    ) -> None:
        released_roots.append(snapshots_root)
        released_protections.append(frozenset(protected_configs))

    monkeypatch.setattr(
        worker_mod,
        "_release_superseded_service_gitconfig_leases",
        _record_released_leases,
    )
    settings = _settings(tmp_path)
    live_gitconfig = Path(settings.host_home) / ".gitconfig"
    live_gitconfig.parent.mkdir(parents=True)
    live_gitconfig.write_text("[user]\n  name = Live fallback\n")
    assert worker_mod.build_worker_runtime(settings) is not None

    async def exercise_overlapping_provisions() -> None:
        first_started = asyncio.Event()
        finish_first = asyncio.Event()
        observed: dict[str, tuple[str, Path | None]] = {}

        async def first_provision() -> None:
            await created["before_provision"]()
            first_started.set()
            await finish_first.wait()
            observed["first"] = (
                created["git"].current_gitconfig(),
                created["auth_mount_resolver"].current_source(),
            )
            await created["after_provision"]()

        first = asyncio.create_task(first_provision())
        await first_started.wait()
        await created["before_provision"]()
        observed["second"] = (
            created["git"].current_gitconfig(),
            created["auth_mount_resolver"].current_source(),
        )
        await created["after_provision"]()
        finish_first.set()
        await first
        assert observed == {
            "first": (str(tmp_path / "snapshot-old"), tmp_path / "snapshot-old"),
            "second": (str(tmp_path / "snapshot-new"), tmp_path / "snapshot-new"),
        }
        assert created["git"].task_envs == {}
        assert created["auth_mount_resolver"].task_sources == {}

    asyncio.run(exercise_overlapping_provisions())
    expected_paths = [str(tmp_path / "snapshot-old"), str(tmp_path / "snapshot-new")]
    assert [env["GIT_CONFIG_GLOBAL"] for env in created["git"].envs] == expected_paths
    assert created["auth_mount_resolver"].sources == [
        tmp_path / "snapshot-old",
        tmp_path / "snapshot-new",
    ]
    assert [env["GIT_CONFIG_GLOBAL"] for env in applied_envs] == expected_paths
    assert released_protections == [
        frozenset({tmp_path / "snapshot-old"}),
        frozenset({tmp_path / "snapshot-old", tmp_path / "snapshot-new"}),
        frozenset({tmp_path / "snapshot-old", tmp_path / "snapshot-new"}),
        frozenset({tmp_path / "snapshot-new"}),
    ]
    assert released_roots == [Path(settings.work_dir) / "service-auth" / "gitconfig-snapshots"] * 4
