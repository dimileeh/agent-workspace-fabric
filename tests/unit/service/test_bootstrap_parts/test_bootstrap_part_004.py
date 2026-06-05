"""Work-dir mount-propagation preflight tests (#376/#388/#398/#400).

The preflight ensures the host work dir is an ``rshared`` mount so a
worker-mounted ``~/.claude`` overlay propagates into the sibling agent
container, or forces the per-workspace copy fallback on non-propagating hosts
(Docker Desktop / virtiofs / grpcfuse). These exercise the standalone helper
with a fake mount table + runner and the bootstrap integration that folds the
result into the stage env.

Extended for #398: persisting the posture into the compose env-file and #400:
surfacing it in status/doctor.
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

import awf.service.bootstrap as bootstrap
from awf.service.bootstrap import (
    ServiceBootstrapOptions,
    WorkDirPropagationResult,
    ensure_work_dir_mount_propagation,
    run_service_bootstrap,
)
from awf.service.config import ServiceSettings


def _settings(tmp_path: Path) -> ServiceSettings:
    return ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url="http://localhost:8000",
        database_url="postgresql+asyncpg://awf:pw@localhost:5433/awf",
        docker_host=f"unix://{tmp_path / 'docker.sock'}",
        agent_runtime_image="awf-agent-runtime:latest",
        work_dir=str(tmp_path / "work"),
        api_token=None,
        github_token=None,
        worker_poll_interval_seconds=0.1,
        worker_max_concurrent_provisions=1,
        host_home=str(tmp_path / "home"),
    )


def _write_source_checkout(root: Path) -> Path:
    (root / "docker" / "compose").mkdir(parents=True)
    (root / "docker" / "agent-runtime.Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (root / "docker" / "control-plane.Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (root / "docker" / "compose" / "local-service.yml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    (root / "compose.yaml").write_text(
        "include:\n  - ./docker/compose/local-service.yml\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text("[project]\nname = 'awf'\n", encoding="utf-8")
    (root / "src" / "awf").mkdir(parents=True)
    (root / "src" / "awf" / "__init__.py").write_text("", encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _isolate_local_compose_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bootstrap, "local_service_environ", lambda **_kwargs: {})
    monkeypatch.setattr(
        bootstrap,
        "LOCAL_SERVICE_COMPOSE_ENV_FILE",
        tmp_path / "missing.env",
        raising=False,
    )
    for key in (
        "AWF_DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "COMPOSE_PROFILES",
        "COMPOSE_PROJECT_NAME",
        "AWF_HOST_WORK_DIR",
        "AWF_WORK_DIR",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _no_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "_bootstrap_asset_root_candidates", lambda: ())
    monkeypatch.setattr(bootstrap, "_packaged_bootstrap_asset_root", lambda: None)


async def _ok_status_collector(settings: ServiceSettings, **_kwargs: object) -> dict[str, object]:
    return {"service": settings.service_name, "status": "ok", "checks": {}}


async def _no_sleep(_delay: float) -> None:
    return None


def _unexpected_runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    raise AssertionError(f"no subprocess expected, got {args}")


@pytest.mark.unit
def test_preflight_shared_mount_is_ensured_without_subprocess(tmp_path: Path) -> None:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "23 28 0:21 / /host/work rw,relatime shared:1 - ext4 /dev/sda rw\n",
        encoding="utf-8",
    )

    result = ensure_work_dir_mount_propagation(
        "/host/work",
        run_subprocess=_unexpected_runner,
        environ={},
        mountinfo_path=mountinfo,
    )

    assert result == WorkDirPropagationResult(
        propagation="rshared",
        force_copy=False,
        reason_code="SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_ENSURED",
        detail=result.detail,
    )
    assert not result.force_copy


@pytest.mark.unit
def test_preflight_resolves_symlinked_work_dir_to_canonical_mount(tmp_path: Path) -> None:
    # The kernel records mount points in mountinfo as symlink-resolved canonical
    # paths. A work dir reached through a symlink must resolve to that canonical
    # path so it still prefix-matches its shared mount, rather than silently
    # forcing the copy fallback.
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"23 28 0:21 / {real} rw,relatime shared:1 - ext4 /dev/sda rw\n",
        encoding="utf-8",
    )

    result = ensure_work_dir_mount_propagation(
        str(link),
        run_subprocess=_unexpected_runner,
        environ={},
        mountinfo_path=mountinfo,
    )

    assert result.propagation == "rshared"
    assert not result.force_copy


@pytest.mark.unit
def test_preflight_private_mount_made_rshared_via_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "23 28 0:21 / / rw,relatime - ext4 /dev/sda rw\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap, "_host_is_linux", lambda: True)
    monkeypatch.setattr(bootstrap, "_mount_binary_available", lambda: True)

    calls: list[list[str]] = []

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    result = ensure_work_dir_mount_propagation(
        str(work_dir),
        run_subprocess=_run,
        environ={"PATH": "/usr/bin"},
        mountinfo_path=mountinfo,
    )

    assert result.propagation == "rshared"
    assert result.force_copy is False
    assert result.reason_code == "SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_ENSURED"
    assert calls == [
        ["mount", "--bind", str(work_dir), str(work_dir)],
        ["mount", "--make-rshared", str(work_dir)],
    ]


@pytest.mark.unit
def test_preflight_creates_missing_work_dir_before_bind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # First bootstrap: the host work dir does not exist yet and the backing fs
    # is a private Linux mount. ``mount --bind <target> <target>`` requires the
    # target directory to exist, so the preflight must create it first;
    # otherwise the bind fails and forces the copy fallback even on a host where
    # ``--make-rshared`` would have enabled the overlay path (#397).
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "23 28 0:21 / / rw,relatime - ext4 /dev/sda rw\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap, "_host_is_linux", lambda: True)
    monkeypatch.setattr(bootstrap, "_mount_binary_available", lambda: True)

    work_dir = tmp_path / "work" / "service"
    assert not work_dir.exists()

    calls: list[list[str]] = []

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        # Mirror real ``mount --bind``: a missing target makes it fail.
        if args[:2] == ["mount", "--bind"] and not Path(args[2]).is_dir():
            return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="")
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    result = ensure_work_dir_mount_propagation(
        str(work_dir),
        run_subprocess=_run,
        environ={"PATH": "/usr/bin"},
        mountinfo_path=mountinfo,
    )

    assert work_dir.is_dir()
    assert result.propagation == "rshared"
    assert result.force_copy is False
    assert result.reason_code == "SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_ENSURED"
    assert calls == [
        ["mount", "--bind", str(work_dir), str(work_dir)],
        ["mount", "--make-rshared", str(work_dir)],
    ]


@pytest.mark.unit
def test_preflight_work_dir_creation_failure_is_non_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If the bind source cannot be created (here the parent is a regular file,
    # so ``mkdir`` raises ``OSError``), the preflight must not crash — it just
    # proceeds and lets the bind decide the posture (#397).
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    work_dir = blocker / "service"
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "23 28 0:21 / / rw,relatime - ext4 /dev/sda rw\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap, "_host_is_linux", lambda: True)
    monkeypatch.setattr(bootstrap, "_mount_binary_available", lambda: True)

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        # Cannot create the bind source → real ``mount --bind`` would fail.
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="")

    result = ensure_work_dir_mount_propagation(
        str(work_dir),
        run_subprocess=_run,
        environ={"PATH": "/usr/bin"},
        mountinfo_path=mountinfo,
    )

    assert not work_dir.exists()
    assert result.propagation == "rprivate"
    assert result.force_copy is True
    assert result.reason_code == "SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_UNAVAILABLE"


@pytest.mark.unit
def test_preflight_missing_mountinfo_forces_copy_fallback(tmp_path: Path) -> None:
    result = ensure_work_dir_mount_propagation(
        "/host/work",
        run_subprocess=_unexpected_runner,
        environ={},
        mountinfo_path=tmp_path / "does-not-exist",
    )

    assert result.propagation == "rprivate"
    assert result.force_copy is True
    assert result.reason_code == "SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_UNAVAILABLE"


@pytest.mark.unit
def test_preflight_non_propagating_fs_forces_copy_fallback(tmp_path: Path) -> None:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "23 28 0:21 / /host/work rw,relatime - grpcfuse grpcfuse rw\n",
        encoding="utf-8",
    )

    result = ensure_work_dir_mount_propagation(
        "/host/work",
        run_subprocess=_unexpected_runner,  # fs check precedes any mount attempt
        environ={},
        mountinfo_path=mountinfo,
    )

    assert result.propagation == "rprivate"
    assert result.force_copy is True
    assert result.reason_code == "SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_UNAVAILABLE"


@pytest.mark.unit
def test_preflight_shared_non_propagating_fs_forces_copy_fallback(tmp_path: Path) -> None:
    # Docker Desktop marks its virtiofs mounts ``shared:N`` in mountinfo, but an
    # overlay still never propagates into the sibling agent. The non-propagating
    # fs check must win over the shared flag, otherwise the worker provisions an
    # empty ``~/.claude`` on rshared instead of using the copy fallback.
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "23 28 0:21 / /host/work rw,relatime shared:1 - virtiofs virtiofs rw\n",
        encoding="utf-8",
    )

    result = ensure_work_dir_mount_propagation(
        "/host/work",
        run_subprocess=_unexpected_runner,  # fs check precedes any mount attempt
        environ={},
        mountinfo_path=mountinfo,
    )

    assert result.propagation == "rprivate"
    assert result.force_copy is True
    assert result.reason_code == "SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_UNAVAILABLE"


@pytest.mark.unit
def test_preflight_make_rshared_failure_forces_copy_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "23 28 0:21 / / rw,relatime - ext4 /dev/sda rw\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap, "_host_is_linux", lambda: True)
    monkeypatch.setattr(bootstrap, "_mount_binary_available", lambda: True)

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        # ``mount --make-rshared`` fails (e.g. not root): copy fallback.
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="not permitted")

    result = ensure_work_dir_mount_propagation(
        str(work_dir),
        run_subprocess=_run,
        environ={},
        mountinfo_path=mountinfo,
    )

    assert result.propagation == "rprivate"
    assert result.force_copy is True
    assert result.reason_code == "SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_UNAVAILABLE"


@pytest.mark.unit
def test_preflight_make_rshared_failure_unwinds_leaked_bind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "23 28 0:21 / / rw,relatime - ext4 /dev/sda rw\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap, "_host_is_linux", lambda: True)
    monkeypatch.setattr(bootstrap, "_mount_binary_available", lambda: True)

    calls: list[list[str]] = []

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        # ``--bind`` succeeds, ``--make-rshared`` fails (namespace disallows it).
        returncode = 1 if args[:2] == ["mount", "--make-rshared"] else 0
        return subprocess.CompletedProcess(args, returncode=returncode, stdout="", stderr="")

    result = ensure_work_dir_mount_propagation(
        str(work_dir),
        run_subprocess=_run,
        environ={},
        mountinfo_path=mountinfo,
    )

    assert result.propagation == "rprivate"
    assert result.force_copy is True
    assert result.reason_code == "SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_UNAVAILABLE"
    # The successful bind must be unwound so the host mount table does not leak.
    assert calls == [
        ["mount", "--bind", str(work_dir), str(work_dir)],
        ["mount", "--make-rshared", str(work_dir)],
        ["umount", str(work_dir)],
    ]


@pytest.mark.unit
def test_preflight_make_rshared_oserror_forces_copy_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "23 28 0:21 / / rw,relatime - ext4 /dev/sda rw\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap, "_host_is_linux", lambda: True)
    monkeypatch.setattr(bootstrap, "_mount_binary_available", lambda: True)

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("mount missing")

    result = ensure_work_dir_mount_propagation(
        str(work_dir),
        run_subprocess=_run,
        environ={},
        mountinfo_path=mountinfo,
    )

    assert result.force_copy is True
    assert result.reason_code == "SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_UNAVAILABLE"


@pytest.mark.unit
def test_preflight_non_linux_host_forces_copy_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "23 28 0:21 / /host/work rw,relatime - ext4 /dev/sda rw\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap, "_host_is_linux", lambda: False)

    result = ensure_work_dir_mount_propagation(
        "/host/work",
        run_subprocess=_unexpected_runner,
        environ={},
        mountinfo_path=mountinfo,
    )

    assert result.force_copy is True
    assert result.reason_code == "SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_UNAVAILABLE"


@pytest.mark.unit
def test_bootstrap_skips_preflight_without_host_work_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No host work dir and no ``HOME`` → no preflight stage, no propagation env."""
    monkeypatch.delenv("AWF_HOST_WORK_DIR", raising=False)
    monkeypatch.delenv("AWF_WORK_DIR", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    root = _write_source_checkout(tmp_path / "checkout")
    captured_env: list[dict[str, str] | None] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured_env.append(kwargs.get("env"))
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    result = asyncio.run(
        run_service_bootstrap(
            _settings(tmp_path),
            options=ServiceBootstrapOptions(timeout_seconds=1, poll_interval_seconds=0.1),
            asset_root=root,
            run_subprocess=_run,
            status_collector=_ok_status_collector,
            sleep=_no_sleep,
            monotonic=lambda: 0.0,
        )
    )

    assert [stage.stage for stage in result.stages] == [
        "agent_runtime_build",
        "postgres",
        "migrate",
        "api_worker",
    ]
    for env in captured_env:
        assert env is None or "AWF_WORK_DIR_BIND_PROPAGATION" not in env


@pytest.mark.unit
def test_bootstrap_records_preflight_stage_and_injects_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured host work dir runs the preflight, records its stage, and
    folds the propagation + force-copy vars into the docker stage env."""
    root = _write_source_checkout(tmp_path / "checkout")
    monkeypatch.setenv("AWF_HOST_WORK_DIR", "/host/work")

    monkeypatch.setattr(
        bootstrap,
        "ensure_work_dir_mount_propagation",
        lambda *_a, **_k: WorkDirPropagationResult(
            propagation="rprivate",
            force_copy=True,
            reason_code="SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_UNAVAILABLE",
            detail="docker desktop bridge",
        ),
    )

    stage_envs: dict[str, dict[str, str] | None] = {}

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        # Key the captured env by the compose subcommand / build for assertions.
        key = args[-1]
        stage_envs[key] = kwargs.get("env")
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    result = asyncio.run(
        run_service_bootstrap(
            _settings(tmp_path),
            options=ServiceBootstrapOptions(timeout_seconds=1, poll_interval_seconds=0.1),
            asset_root=root,
            run_subprocess=_run,
            status_collector=_ok_status_collector,
            sleep=_no_sleep,
            monotonic=lambda: 0.0,
        )
    )

    stages_by_name = {stage.stage: stage for stage in result.stages}
    assert "work_dir_propagation" in stages_by_name
    preflight_stage = stages_by_name["work_dir_propagation"]
    assert preflight_stage.stdout == "SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_UNAVAILABLE"
    # The preflight runs before the api/worker containers start.
    assert result.stages[0].stage == "work_dir_propagation"

    worker_env = stage_envs["worker"]
    assert worker_env is not None
    assert worker_env["AWF_WORK_DIR_BIND_PROPAGATION"] == "rprivate"
    assert worker_env["AWF_CLAUDE_AUTH_FORCE_COPY"] == "true"


@pytest.mark.unit
def test_bootstrap_preflights_default_work_dir_when_unpinned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no host work dir pinned, the preflight runs on compose's default
    ``${HOME}/.awf/service`` path so Docker Desktop/virtiofs hosts are detected."""
    root = _write_source_checkout(tmp_path / "checkout")
    monkeypatch.delenv("AWF_HOST_WORK_DIR", raising=False)
    monkeypatch.delenv("AWF_WORK_DIR", raising=False)
    monkeypatch.setenv("HOME", "/home/op")

    seen_paths: list[str] = []

    def _preflight(host_work_dir: str, **_kwargs: object) -> WorkDirPropagationResult:
        seen_paths.append(host_work_dir)
        return WorkDirPropagationResult(
            propagation="rprivate",
            force_copy=True,
            reason_code="SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_UNAVAILABLE",
            detail="docker desktop bridge",
        )

    monkeypatch.setattr(bootstrap, "ensure_work_dir_mount_propagation", _preflight)

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    result = asyncio.run(
        run_service_bootstrap(
            _settings(tmp_path),
            options=ServiceBootstrapOptions(timeout_seconds=1, poll_interval_seconds=0.1),
            asset_root=root,
            run_subprocess=_run,
            status_collector=_ok_status_collector,
            sleep=_no_sleep,
            monotonic=lambda: 0.0,
        )
    )

    assert seen_paths == ["/home/op/.awf/service"]
    assert "work_dir_propagation" in {stage.stage for stage in result.stages}


@pytest.mark.unit
def test_host_is_linux_and_mount_binary_available_return_bool() -> None:
    # Exercise the real host-detection helpers (monkeypatched elsewhere).
    assert isinstance(bootstrap._host_is_linux(), bool)  # noqa: SLF001
    assert isinstance(bootstrap._mount_binary_available(), bool)  # noqa: SLF001


@pytest.mark.unit
def test_preflight_parses_mountinfo_edges_and_picks_longest_prefix(tmp_path: Path) -> None:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "\n".join(
            [
                "garbage",  # too few fields
                "1 2 0:1 / /x rw a b",  # no '-' separator
                "1 2 0:1 / /y rw shared:1 -",  # nothing after separator
                "1 2 0:1 / /other rw - ext4 root rw",  # unrelated mount (not a prefix)
                "1 2 0:1 / /host/work rw shared:5 - ext4 root rw",  # exact, shared (longest)
                "1 2 0:1 / /host rw - ext4 root rw",  # shorter prefix, seen after best
                "1 2 0:1 / / rw shared:9 - ext4 root rw",  # root, shorter, seen after best
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = ensure_work_dir_mount_propagation(
        "/host/work",
        run_subprocess=_unexpected_runner,
        environ={},
        mountinfo_path=mountinfo,
    )

    assert result.propagation == "rshared"
    assert result.force_copy is False
    assert result.reason_code == "SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_ENSURED"


@pytest.mark.unit
def test_preflight_no_mount_binary_forces_copy_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "23 28 0:21 / /host/work rw,relatime - ext4 /dev/sda rw\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap, "_host_is_linux", lambda: True)
    monkeypatch.setattr(bootstrap, "_mount_binary_available", lambda: False)

    result = ensure_work_dir_mount_propagation(
        "/host/work",
        run_subprocess=_unexpected_runner,
        environ={},
        mountinfo_path=mountinfo,
    )

    assert result.force_copy is True
    assert result.reason_code == "SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_UNAVAILABLE"


@pytest.mark.unit
def test_preflight_make_rshared_with_default_environ(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Default ``environ=None`` exercises the no-env branch of the mount runner.
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "23 28 0:21 / / rw,relatime - ext4 /dev/sda rw\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap, "_host_is_linux", lambda: True)
    monkeypatch.setattr(bootstrap, "_mount_binary_available", lambda: True)

    seen_env: list[object] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen_env.append(kwargs.get("env", "missing"))
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    result = ensure_work_dir_mount_propagation(
        str(work_dir),
        run_subprocess=_run,
        mountinfo_path=mountinfo,
    )

    assert result.propagation == "rshared"
    # ``env`` is omitted entirely when no environ is supplied.
    assert seen_env == ["missing", "missing"]


@pytest.mark.unit
def test_resolve_bootstrap_host_work_dir_mirrors_compose_bind() -> None:
    resolve = bootstrap._resolve_bootstrap_host_work_dir  # noqa: SLF001
    # Only AWF_HOST_WORK_DIR pins the host bind, matching compose's
    # ``${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}`` expression.
    assert resolve({"AWF_HOST_WORK_DIR": "/a"}) == "/a"
    assert resolve({"AWF_HOST_WORK_DIR": "  /a  "}) == "/a"
    # AWF_WORK_DIR must NOT be consulted: it is the in-container CLI/API state
    # root (default ``.awf``), which compose sets from the host bind path rather
    # than reads. Preflighting it would inspect the wrong path and leave the
    # actual ${HOME}/.awf/service bind on its default rshared posture (#397
    # review PRRT_kwDOSJAM6s6HB0Bj). Falls through to the compose default.
    assert resolve({"AWF_WORK_DIR": "/b", "HOME": "/home/op"}) == "/home/op/.awf/service"
    assert resolve({"AWF_HOST_WORK_DIR": "   ", "AWF_WORK_DIR": "/b", "HOME": "/home/op"}) == (
        "/home/op/.awf/service"
    )
    # With AWF_WORK_DIR set but no HOME, nothing is knowable.
    assert resolve({"AWF_WORK_DIR": "/b"}) is None
    # With nothing pinned, fall back to compose's deterministic default so the
    # preflight still runs on the common bootstrap path (#397 review).
    assert resolve({"HOME": "/home/op"}) == "/home/op/.awf/service"
    # A blank HOME falls through to the explicit-pins-only behavior.
    assert resolve({"HOME": "   "}) is None
    assert resolve({}) is None


@pytest.mark.unit
def test_unescape_mountinfo_field_decodes_escapes_order_independently() -> None:
    # Plain octal escapes the kernel emits for space/tab/newline/backslash.
    assert bootstrap._unescape_mountinfo_field("/a\\040b") == "/a b"  # noqa: SLF001
    assert bootstrap._unescape_mountinfo_field("/a\\011b") == "/a\tb"  # noqa: SLF001
    assert bootstrap._unescape_mountinfo_field("/a\\012b") == "/a\nb"  # noqa: SLF001
    assert bootstrap._unescape_mountinfo_field("/a\\134b") == "/a\\b"  # noqa: SLF001
    # A literal backslash followed by octal-escape-like digits is encoded as
    # ``\134040``; a single regex pass must decode it to ``\040`` rather than
    # mangling it into a space the way sequential replacements would (#397 review
    # issue:4620841664).
    assert bootstrap._unescape_mountinfo_field("/a\\134040b") == "/a\\040b"  # noqa: SLF001


@pytest.mark.unit
def test_apply_propagation_env_raises_force_copy_but_never_lowers_it() -> None:
    apply = bootstrap._apply_work_dir_propagation_env  # noqa: SLF001
    forced = WorkDirPropagationResult(
        propagation="rprivate",
        force_copy=True,
        reason_code="SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_UNAVAILABLE",
        detail="docker desktop bridge",
    )
    shared = WorkDirPropagationResult(
        propagation="rshared",
        force_copy=False,
        reason_code="SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_ENSURED",
        detail="made rshared",
    )

    # The preflight raises the posture when it forces copy.
    assert apply({}, forced)["AWF_CLAUDE_AUTH_FORCE_COPY"] == "true"
    # And clears it when nothing requested copy and the preflight is satisfied.
    assert apply({}, shared)["AWF_CLAUDE_AUTH_FORCE_COPY"] == "false"

    # But an operator's explicit force-copy override (env or compose env file)
    # must survive a satisfied preflight — auth_mounts treats this variable as an
    # operator force-copy request, so the preflight may only raise the posture,
    # never lower it (#397 review PRRT_kwDOSJAM6s6HDkq0).
    for truthy in ("true", "1", "yes", "on", "  True  "):
        env = apply({"AWF_CLAUDE_AUTH_FORCE_COPY": truthy}, shared)
        assert env["AWF_CLAUDE_AUTH_FORCE_COPY"] == "true"
    # A falsey existing value does not pin the posture on a satisfied preflight.
    assert (
        apply({"AWF_CLAUDE_AUTH_FORCE_COPY": "false"}, shared)["AWF_CLAUDE_AUTH_FORCE_COPY"]
        == "false"
    )


# ---------------------------------------------------------------------------
# Persist work-dir propagation posture to compose env-file (#398)
# ---------------------------------------------------------------------------


def _read_env_file_values(path: Path) -> dict[str, str]:
    from awf.service.environment import compose_env_file_values

    return compose_env_file_values(path)


@pytest.mark.unit
def test_persist_writes_propagation_to_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    result = WorkDirPropagationResult(
        propagation="rshared",
        force_copy=False,
        reason_code="SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_ENSURED",
        detail="made rshared",
    )
    bootstrap._persist_work_dir_propagation_result(env_file, result)  # noqa: SLF001

    values = _read_env_file_values(env_file)
    assert values["AWF_WORK_DIR_BIND_PROPAGATION"] == "rshared"
    assert values["AWF_CLAUDE_AUTH_FORCE_COPY"] == "false"
    assert "AWF_WORK_DIR_PROPAGATION_TIMESTAMP" in values


@pytest.mark.unit
def test_persist_writes_force_copy_to_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    result = WorkDirPropagationResult(
        propagation="rprivate",
        force_copy=True,
        reason_code="SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_UNAVAILABLE",
        detail="docker desktop bridge",
    )
    bootstrap._persist_work_dir_propagation_result(env_file, result)  # noqa: SLF001

    values = _read_env_file_values(env_file)
    assert values["AWF_WORK_DIR_BIND_PROPAGATION"] == "rprivate"
    assert values["AWF_CLAUDE_AUTH_FORCE_COPY"] == "true"
    assert "AWF_WORK_DIR_PROPAGATION_TIMESTAMP" in values


@pytest.mark.unit
def test_persist_preserves_existing_env_file_entries(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SOME_OTHER_VAR=foo\n", encoding="utf-8")

    result = WorkDirPropagationResult(
        propagation="rshared",
        force_copy=False,
        reason_code="SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_ENSURED",
        detail="made rshared",
    )
    bootstrap._persist_work_dir_propagation_result(env_file, result)  # noqa: SLF001

    values = _read_env_file_values(env_file)
    assert values["SOME_OTHER_VAR"] == "foo"
    assert values["AWF_WORK_DIR_BIND_PROPAGATION"] == "rshared"
    assert values["AWF_CLAUDE_AUTH_FORCE_COPY"] == "false"


@pytest.mark.unit
def test_persist_strips_export_prefix_to_avoid_duplicates(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "export AWF_WORK_DIR_BIND_PROPAGATION=rshared\nSOME_OTHER_VAR=bar\n",
        encoding="utf-8",
    )

    result = WorkDirPropagationResult(
        propagation="rprivate",
        force_copy=True,
        reason_code="SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_UNAVAILABLE",
        detail="docker desktop bridge",
    )
    bootstrap._persist_work_dir_propagation_result(env_file, result)  # noqa: SLF001

    values = _read_env_file_values(env_file)
    assert values["AWF_WORK_DIR_BIND_PROPAGATION"] == "rprivate"
    assert values["SOME_OTHER_VAR"] == "bar"
    awf_keys = [k for k in values if k.startswith("AWF_")]
    assert len(awf_keys) == 3


@pytest.mark.unit
def test_persist_is_best_effort_non_fatal(tmp_path: Path) -> None:
    unwritable = tmp_path / "noperm" / ".env"
    unwritable.parent.mkdir()
    unwritable.parent.chmod(0o444)

    result = WorkDirPropagationResult(
        propagation="rshared",
        force_copy=False,
        reason_code="SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_ENSURED",
        detail="made rshared",
    )
    try:
        bootstrap._persist_work_dir_propagation_result(unwritable, result)  # noqa: SLF001
    finally:
        unwritable.parent.chmod(0o755)


@pytest.mark.unit
def test_persist_is_best_effort_on_unicode_decode_error(tmp_path: Path) -> None:
    """Non-OSError failures from read_text must not abort bootstrap (#413)."""
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"\xff\xfe")

    result = WorkDirPropagationResult(
        propagation="rshared",
        force_copy=False,
        reason_code="SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_ENSURED",
        detail="made rshared",
    )
    bootstrap._persist_work_dir_propagation_result(env_file, result)  # noqa: SLF001


@pytest.mark.unit
def test_persist_overwrites_stale_posture(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AWF_WORK_DIR_BIND_PROPAGATION=rshared\n"
        "AWF_CLAUDE_AUTH_FORCE_COPY=false\n"
        "AWF_WORK_DIR_PROPAGATION_TIMESTAMP=2020-01-01T00:00:00+00:00\n",
        encoding="utf-8",
    )

    result = WorkDirPropagationResult(
        propagation="rprivate",
        force_copy=True,
        reason_code="SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_UNAVAILABLE",
        detail="docker desktop bridge",
    )
    bootstrap._persist_work_dir_propagation_result(env_file, result)  # noqa: SLF001

    values = _read_env_file_values(env_file)
    assert values["AWF_WORK_DIR_BIND_PROPAGATION"] == "rprivate"
    assert values["AWF_CLAUDE_AUTH_FORCE_COPY"] == "true"
    ts = values["AWF_WORK_DIR_PROPAGATION_TIMESTAMP"]
    parsed_ts = datetime.fromisoformat(ts)
    assert parsed_ts.year >= 2025


@pytest.mark.unit
def test_persist_creates_env_file_if_absent(tmp_path: Path) -> None:
    env_file = tmp_path / "subdir" / ".env"
    assert not env_file.exists()

    result = WorkDirPropagationResult(
        propagation="rshared",
        force_copy=False,
        reason_code="SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_ENSURED",
        detail="made rshared",
    )
    bootstrap._persist_work_dir_propagation_result(env_file, result)  # noqa: SLF001

    assert env_file.exists()
    values = _read_env_file_values(env_file)
    assert values["AWF_WORK_DIR_BIND_PROPAGATION"] == "rshared"


@pytest.mark.unit
def test_persist_env_values_match_compose_interpolation(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    result = WorkDirPropagationResult(
        propagation="rprivate",
        force_copy=True,
        reason_code="SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_UNAVAILABLE",
        detail="docker desktop bridge",
    )
    bootstrap._persist_work_dir_propagation_result(env_file, result)  # noqa: SLF001

    from awf.service.environment import compose_env_file_values

    merged = compose_env_file_values(env_file)
    assert merged["AWF_WORK_DIR_BIND_PROPAGATION"] == "rprivate"
    assert merged["AWF_CLAUDE_AUTH_FORCE_COPY"] == "true"
    assert "AWF_WORK_DIR_PROPAGATION_TIMESTAMP" in merged


@pytest.mark.unit
def test_persist_preserves_operator_force_copy_override(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("AWF_CLAUDE_AUTH_FORCE_COPY=true\n", encoding="utf-8")

    result = WorkDirPropagationResult(
        propagation="rshared",
        force_copy=False,
        reason_code="SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_ENSURED",
        detail="made rshared",
    )
    bootstrap._persist_work_dir_propagation_result(env_file, result)  # noqa: SLF001

    values = _read_env_file_values(env_file)
    assert values["AWF_CLAUDE_AUTH_FORCE_COPY"] == "true"


@pytest.mark.unit
def test_persist_clears_stale_generated_force_copy(tmp_path: Path) -> None:
    """A previous bootstrap wrote AWF_CLAUDE_AUTH_FORCE_COPY=true alongside a
    timestamp.  When a fresh preflight concludes force_copy is no longer needed
    (e.g. work dir moved to a shared mount), the stale generated value must not
    be treated as an operator override — otherwise bootstrap can never return
    to overlay mode (#413)."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AWF_WORK_DIR_BIND_PROPAGATION=rprivate\n"
        "AWF_CLAUDE_AUTH_FORCE_COPY=true\n"
        "AWF_WORK_DIR_PROPAGATION_TIMESTAMP=2020-01-01T00:00:00+00:00\n",
        encoding="utf-8",
    )

    result = WorkDirPropagationResult(
        propagation="rshared",
        force_copy=False,
        reason_code="SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_ENSURED",
        detail="made rshared",
    )
    bootstrap._persist_work_dir_propagation_result(env_file, result)  # noqa: SLF001

    values = _read_env_file_values(env_file)
    assert values["AWF_CLAUDE_AUTH_FORCE_COPY"] == "false"


@pytest.mark.unit
def test_persist_preserves_operator_force_copy_from_environ(tmp_path: Path) -> None:
    """When the operator sets AWF_CLAUDE_AUTH_FORCE_COPY in the process
    environment (not the env-file) and preflight decides rshared / no
    force-copy, _persist_work_dir_propagation_result must still write
    ``true`` so a later non-bootstrap compose recreate sees the override
    (#398 regression)."""
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")

    result = WorkDirPropagationResult(
        propagation="rshared",
        force_copy=False,
        reason_code="SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_ENSURED",
        detail="made rshared",
    )
    environ = {"AWF_CLAUDE_AUTH_FORCE_COPY": "true"}
    bootstrap._persist_work_dir_propagation_result(env_file, result, environ=environ)  # noqa: SLF001

    values = _read_env_file_values(env_file)
    assert values["AWF_CLAUDE_AUTH_FORCE_COPY"] == "true"


@pytest.mark.unit
def test_persist_ignores_stale_force_copy_in_environ_when_env_file_stale(
    tmp_path: Path,
) -> None:
    """When the env-file has a stale bootstrap-generated
    AWF_CLAUDE_AUTH_FORCE_COPY=true plus AWF_WORK_DIR_PROPAGATION_TIMESTAMP,
    the in-process environ likely inherited that stale value from the
    env-file (via local_service_environ).  Passing such an environ to
    _persist_work_dir_propagation_result must not treat the stale value
    as an operator override — otherwise a fresh preflight returning
    force_copy=False writes true back and bootstrap cannot return to
    overlay mode (#413)."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AWF_WORK_DIR_BIND_PROPAGATION=rprivate\n"
        "AWF_CLAUDE_AUTH_FORCE_COPY=true\n"
        "AWF_WORK_DIR_PROPAGATION_TIMESTAMP=2020-01-01T00:00:00+00:00\n",
        encoding="utf-8",
    )

    result = WorkDirPropagationResult(
        propagation="rshared",
        force_copy=False,
        reason_code="SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_ENSURED",
        detail="made rshared",
    )
    environ_with_stale_force_copy = {"AWF_CLAUDE_AUTH_FORCE_COPY": "true"}
    bootstrap._persist_work_dir_propagation_result(
        env_file, result, environ=environ_with_stale_force_copy
    )  # noqa: SLF001

    values = _read_env_file_values(env_file)
    assert values["AWF_CLAUDE_AUTH_FORCE_COPY"] == "false"


@pytest.mark.unit
def test_bootstrap_persist_called_after_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After run_service_bootstrap, the compose env-file has the persisted
    posture values — not just in-process env injection, but actually written
    to the env-file so a subsequent docker compose up --force-recreate
    preserves the preflight-chosen posture (#398)."""
    root = _write_source_checkout(tmp_path / "checkout")
    env_file = tmp_path / "persist-test.env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("AWF_HOST_WORK_DIR", "/host/work")

    monkeypatch.setattr(
        bootstrap,
        "ensure_work_dir_mount_propagation",
        lambda *_a, **_k: WorkDirPropagationResult(
            propagation="rprivate",
            force_copy=True,
            reason_code="SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_UNAVAILABLE",
            detail="docker desktop bridge",
        ),
    )

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    asyncio.run(
        run_service_bootstrap(
            _settings(tmp_path),
            options=ServiceBootstrapOptions(timeout_seconds=1, poll_interval_seconds=0.1),
            asset_root=root,
            env_file=env_file,
            run_subprocess=_run,
            status_collector=_ok_status_collector,
            sleep=_no_sleep,
            monotonic=lambda: 0.0,
        )
    )

    values = _read_env_file_values(env_file)
    assert values["AWF_WORK_DIR_BIND_PROPAGATION"] == "rprivate"
    assert values["AWF_CLAUDE_AUTH_FORCE_COPY"] == "true"
    assert "AWF_WORK_DIR_PROPAGATION_TIMESTAMP" in values


@pytest.mark.unit
def test_compose_recreate_uses_persisted_posture(tmp_path: Path) -> None:
    """A non-bootstrap ``docker compose up --force-recreate`` must use the
    persisted env-file posture rather than the compose-file defaults.  When
    Docker Compose interpolates ``${AWF_WORK_DIR_BIND_PROPAGATION:-rshared}``
    in the YAML, it reads the env-file first — so persisted values override
    the YAML defaults (``rshared`` / ``force_copy=false``).  This test verifies
    the env-file carries the posture and that those values differ from the
    compose-file defaults, proving a recreate would use the persisted posture."""
    from awf.service.environment import compose_env_file_values, compose_interpolation_environ

    env_file = tmp_path / ".env"
    compose_file = tmp_path / "compose" / "local-service.yml"
    compose_file.parent.mkdir(parents=True, exist_ok=True)
    compose_file.write_text(
        "services:\n"
        "  worker:\n"
        "    image: test\n"
        "    environment:\n"
        "      - AWF_WORK_DIR_BIND_PROPAGATION=${AWF_WORK_DIR_BIND_PROPAGATION:-rshared}\n"
        "      - AWF_CLAUDE_AUTH_FORCE_COPY=${AWF_CLAUDE_AUTH_FORCE_COPY:-false}\n",
        encoding="utf-8",
    )

    result = WorkDirPropagationResult(
        propagation="rprivate",
        force_copy=True,
        reason_code="SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_UNAVAILABLE",
        detail="docker desktop bridge",
    )
    bootstrap._persist_work_dir_propagation_result(env_file, result)  # noqa: SLF001

    env_file_values = compose_env_file_values(env_file)
    assert env_file_values["AWF_WORK_DIR_BIND_PROPAGATION"] == "rprivate"
    assert env_file_values["AWF_CLAUDE_AUTH_FORCE_COPY"] == "true"

    service_env = {
        "AWF_WORK_DIR_BIND_PROPAGATION": "rprivate",
        "AWF_CLAUDE_AUTH_FORCE_COPY": "true",
    }
    interpolation = compose_interpolation_environ(
        service_env,
        compose_file=compose_file,
        compose_env_file=env_file,
    )
    assert interpolation.get("AWF_WORK_DIR_BIND_PROPAGATION") != "rshared"
    assert interpolation.get("AWF_CLAUDE_AUTH_FORCE_COPY") != "false"
