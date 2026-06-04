"""Bootstrap asset-root pinning and force-rebuild hook tests (T05)."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

import awf.service.bootstrap as bootstrap
from awf.service.bootstrap import (
    ServiceBootstrapError,
    ServiceBootstrapOptions,
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
    """Write the minimal source tree required by bootstrap asset discovery."""

    (root / "docker" / "compose").mkdir(parents=True)
    (root / "docker" / "agent-runtime.Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (root / "docker" / "control-plane.Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (root / "docker" / "compose" / "local-service.yml").write_text(
        "services: {}\n",
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
    ):
        monkeypatch.delenv(key, raising=False)
    # Skip the work-dir mount-propagation preflight: force the host-work-dir
    # resolver to the real "no host work dir" path so no preflight stage or
    # propagation env is folded in. These command-sequence/env tests assert the
    # docker stage plumbing in isolation; the preflight is covered end-to-end in
    # test_bootstrap_part_004.
    monkeypatch.setattr(
        bootstrap,
        "_resolve_bootstrap_host_work_dir",
        lambda *_args, **_kwargs: None,
    )


@pytest.fixture(autouse=True)
def _no_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force discovery to find nothing so pinning is the only asset source."""
    monkeypatch.setattr(bootstrap, "_bootstrap_asset_root_candidates", lambda: ())
    monkeypatch.setattr(bootstrap, "_packaged_bootstrap_asset_root", lambda: None)


async def _ok_status_collector(settings: ServiceSettings, **_kwargs: object) -> dict[str, object]:
    return {"service": settings.service_name, "status": "ok", "checks": {}}


async def _no_sleep(_delay: float) -> None:
    return None


@pytest.mark.unit
def test_resolve_bootstrap_assets_pins_to_explicit_asset_root(tmp_path: Path) -> None:
    """An explicit asset_root pins resolution even when discovery finds nothing."""
    root = _write_source_checkout(tmp_path / "pinned-checkout")

    assets = bootstrap._resolve_bootstrap_assets(  # noqa: SLF001
        bootstrap.LOCAL_SERVICE_COMPOSE_FILE,
        require_agent_runtime=True,
        asset_root=root,
    )

    assert assets.root == root
    assert assets.compose_file == root / bootstrap.LOCAL_SERVICE_COMPOSE_FILE
    assert assets.agent_runtime_dockerfile == root / bootstrap.AGENT_RUNTIME_DOCKERFILE
    assert assets.compose_env_file is None


@pytest.mark.unit
def test_resolve_bootstrap_assets_pins_verified_compose_path(tmp_path: Path) -> None:
    """A verified checkout's absolute compose path resolves to the pinned root."""
    root = _write_source_checkout(tmp_path / "pinned-checkout")
    compose_file = root / bootstrap.LOCAL_SERVICE_COMPOSE_FILE

    assets = bootstrap._resolve_bootstrap_assets(  # noqa: SLF001
        compose_file,
        require_agent_runtime=False,
        asset_root=root,
    )

    assert assets.root == root
    assert assets.compose_file == compose_file
    assert assets.agent_runtime_dockerfile is None


@pytest.mark.unit
def test_resolve_bootstrap_assets_invalid_asset_root_raises(tmp_path: Path) -> None:
    """A pinned root missing source markers raises the not-found bootstrap error."""
    invalid_root = tmp_path / "not-a-checkout"
    invalid_root.mkdir()

    with pytest.raises(ServiceBootstrapError) as exc_info:
        bootstrap._resolve_bootstrap_assets(  # noqa: SLF001
            bootstrap.LOCAL_SERVICE_COMPOSE_FILE,
            require_agent_runtime=True,
            asset_root=invalid_root,
        )

    assert exc_info.value.reason_code == "SERVICE_BOOTSTRAP_ASSETS_NOT_FOUND"


@pytest.mark.unit
def test_resolve_bootstrap_assets_default_none_uses_discovery(tmp_path: Path) -> None:
    """asset_root=None keeps discovery behavior (which finds nothing here)."""
    with pytest.raises(ServiceBootstrapError) as exc_info:
        bootstrap._resolve_bootstrap_assets(  # noqa: SLF001
            bootstrap.LOCAL_SERVICE_COMPOSE_FILE,
            require_agent_runtime=True,
            asset_root=None,
        )

    assert exc_info.value.reason_code == "SERVICE_BOOTSTRAP_ASSETS_NOT_FOUND"


@pytest.mark.unit
def test_force_rebuild_adds_no_cache_to_agent_runtime_build(tmp_path: Path) -> None:
    """force_rebuild=True injects --no-cache into the agent runtime build stage."""
    root = _write_source_checkout(tmp_path / "pinned-checkout")
    assets = bootstrap._resolve_bootstrap_assets(  # noqa: SLF001
        bootstrap.LOCAL_SERVICE_COMPOSE_FILE,
        require_agent_runtime=True,
        asset_root=root,
    )

    stages = bootstrap._bootstrap_stages(  # noqa: SLF001
        _settings(tmp_path),
        options=ServiceBootstrapOptions(force_rebuild=True),
        compose_file=bootstrap.LOCAL_SERVICE_COMPOSE_FILE,
        assets=assets,
    )

    build_stage = stages[0]
    assert build_stage.name == "agent_runtime_build"
    assert build_stage.command == (
        "docker",
        "build",
        "--no-cache",
        "-t",
        "awf-agent-runtime:latest",
        "-f",
        str(root / bootstrap.AGENT_RUNTIME_DOCKERFILE),
        str(root),
    )


@pytest.mark.unit
def test_force_rebuild_false_keeps_cached_build_command(tmp_path: Path) -> None:
    """force_rebuild=False keeps the cached docker build command unchanged."""
    root = _write_source_checkout(tmp_path / "pinned-checkout")
    assets = bootstrap._resolve_bootstrap_assets(  # noqa: SLF001
        bootstrap.LOCAL_SERVICE_COMPOSE_FILE,
        require_agent_runtime=True,
        asset_root=root,
    )

    stages = bootstrap._bootstrap_stages(  # noqa: SLF001
        _settings(tmp_path),
        options=ServiceBootstrapOptions(force_rebuild=False),
        compose_file=bootstrap.LOCAL_SERVICE_COMPOSE_FILE,
        assets=assets,
    )

    build_stage = stages[0]
    assert build_stage.name == "agent_runtime_build"
    assert "--no-cache" not in build_stage.command


@pytest.mark.unit
def test_force_rebuild_skipped_when_agent_runtime_build_skipped(tmp_path: Path) -> None:
    """Skipping the runtime build leaves no build stage even with force_rebuild."""
    root = _write_source_checkout(tmp_path / "pinned-checkout")
    assets = bootstrap._resolve_bootstrap_assets(  # noqa: SLF001
        bootstrap.LOCAL_SERVICE_COMPOSE_FILE,
        require_agent_runtime=False,
        asset_root=root,
    )

    stages = bootstrap._bootstrap_stages(  # noqa: SLF001
        _settings(tmp_path),
        options=ServiceBootstrapOptions(force_rebuild=True, skip_agent_runtime_build=True),
        compose_file=bootstrap.LOCAL_SERVICE_COMPOSE_FILE,
        assets=assets,
    )

    assert all(stage.name != "agent_runtime_build" for stage in stages)


@pytest.mark.unit
def test_run_service_bootstrap_pins_root_and_forces_rebuild(tmp_path: Path) -> None:
    """End-to-end: pinned root + force_rebuild issues the expected docker commands."""
    root = _write_source_checkout(tmp_path / "pinned-checkout")
    calls: list[list[str]] = []

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    result = asyncio.run(
        run_service_bootstrap(
            _settings(tmp_path),
            options=ServiceBootstrapOptions(
                timeout_seconds=1,
                poll_interval_seconds=0.1,
                force_rebuild=True,
            ),
            asset_root=root,
            run_subprocess=_run,
            status_collector=_ok_status_collector,
            sleep=_no_sleep,
            monotonic=lambda: 0.0,
        )
    )

    assert result.service_status["status"] == "ok"
    assert calls[0] == [
        "docker",
        "build",
        "--no-cache",
        "-t",
        "awf-agent-runtime:latest",
        "-f",
        str(root / "docker/agent-runtime.Dockerfile"),
        str(root),
    ]
    assert calls[1][:4] == [
        "docker",
        "compose",
        "-f",
        str(root / "docker/compose/local-service.yml"),
    ]
