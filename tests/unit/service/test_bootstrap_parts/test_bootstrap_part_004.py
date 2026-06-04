"""Work-dir mount-propagation preflight tests (#376/#388).

The preflight ensures the host work dir is an ``rshared`` mount so a
worker-mounted ``~/.claude`` overlay propagates into the sibling agent
container, or forces the per-workspace copy fallback on non-propagating hosts
(Docker Desktop / virtiofs / grpcfuse). These exercise the standalone helper
with a fake mount table + runner and the bootstrap integration that folds the
result into the stage env.
"""

from __future__ import annotations

import asyncio
import subprocess
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
def test_preflight_private_mount_made_rshared_via_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "23 28 0:21 / /host/work rw,relatime - ext4 /dev/sda rw\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap, "_host_is_linux", lambda: True)
    monkeypatch.setattr(bootstrap, "_mount_binary_available", lambda: True)

    calls: list[list[str]] = []

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    result = ensure_work_dir_mount_propagation(
        "/host/work",
        run_subprocess=_run,
        environ={"PATH": "/usr/bin"},
        mountinfo_path=mountinfo,
    )

    assert result.propagation == "rshared"
    assert result.force_copy is False
    assert result.reason_code == "SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_ENSURED"
    assert calls == [
        ["mount", "--bind", "/host/work", "/host/work"],
        ["mount", "--make-rshared", "/host/work"],
    ]


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
def test_preflight_make_rshared_failure_forces_copy_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "23 28 0:21 / /host/work rw,relatime - ext4 /dev/sda rw\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap, "_host_is_linux", lambda: True)
    monkeypatch.setattr(bootstrap, "_mount_binary_available", lambda: True)

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        # ``mount --make-rshared`` fails (e.g. not root): copy fallback.
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="not permitted")

    result = ensure_work_dir_mount_propagation(
        "/host/work",
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
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "23 28 0:21 / /host/work rw,relatime - ext4 /dev/sda rw\n",
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
        "/host/work",
        run_subprocess=_run,
        environ={},
        mountinfo_path=mountinfo,
    )

    assert result.propagation == "rprivate"
    assert result.force_copy is True
    assert result.reason_code == "SERVICE_BOOTSTRAP_WORK_DIR_PROPAGATION_UNAVAILABLE"
    # The successful bind must be unwound so the host mount table does not leak.
    assert calls == [
        ["mount", "--bind", "/host/work", "/host/work"],
        ["mount", "--make-rshared", "/host/work"],
        ["umount", "/host/work"],
    ]


@pytest.mark.unit
def test_preflight_make_rshared_oserror_forces_copy_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "23 28 0:21 / /host/work rw,relatime - ext4 /dev/sda rw\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap, "_host_is_linux", lambda: True)
    monkeypatch.setattr(bootstrap, "_mount_binary_available", lambda: True)

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("mount missing")

    result = ensure_work_dir_mount_propagation(
        "/host/work",
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
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "23 28 0:21 / /host/work rw,relatime - ext4 /dev/sda rw\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap, "_host_is_linux", lambda: True)
    monkeypatch.setattr(bootstrap, "_mount_binary_available", lambda: True)

    seen_env: list[object] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen_env.append(kwargs.get("env", "missing"))
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    result = ensure_work_dir_mount_propagation(
        "/host/work",
        run_subprocess=_run,
        mountinfo_path=mountinfo,
    )

    assert result.propagation == "rshared"
    # ``env`` is omitted entirely when no environ is supplied.
    assert seen_env == ["missing", "missing"]


@pytest.mark.unit
def test_resolve_bootstrap_host_work_dir_prefers_host_then_work_dir() -> None:
    resolve = bootstrap._resolve_bootstrap_host_work_dir  # noqa: SLF001
    assert resolve({"AWF_HOST_WORK_DIR": "/a"}) == "/a"
    assert resolve({"AWF_WORK_DIR": "/b"}) == "/b"
    # Blank AWF_HOST_WORK_DIR falls through to AWF_WORK_DIR.
    assert resolve({"AWF_HOST_WORK_DIR": "   ", "AWF_WORK_DIR": "/b"}) == "/b"
    # With nothing pinned, fall back to compose's deterministic default so the
    # preflight still runs on the common bootstrap path (#397 review).
    assert resolve({"HOME": "/home/op"}) == "/home/op/.awf/service"
    # A blank HOME falls through to the explicit-pins-only behavior.
    assert resolve({"HOME": "   "}) is None
    assert resolve({}) is None
