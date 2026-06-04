"""Bootstrap source and packaged asset resolution tests."""

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
    (root / "docker" / "agent-runtime.Dockerfile").write_text(
        "FROM scratch\n",
        encoding="utf-8",
    )
    (root / "docker" / "control-plane.Dockerfile").write_text(
        "FROM scratch\n",
        encoding="utf-8",
    )
    (root / "docker" / "compose" / "local-service.yml").write_text(
        "services: {}\n",
        encoding="utf-8",
    )
    (root / "compose.yaml").write_text(
        "include:\n  - ./docker/compose/local-service.yml\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text("[project]\nname = 'awf'\n", encoding="utf-8")
    (root / "src" / "awf").mkdir(parents=True)
    (root / "src" / "awf" / "__init__.py").write_text("", encoding="utf-8")
    return root


@pytest.fixture
def source_checkout_root(tmp_path: Path) -> Path:
    return _write_source_checkout(tmp_path / "default-source-checkout")


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
    monkeypatch.setattr(
        bootstrap,
        "_resolve_bootstrap_host_work_dir",
        lambda *_args, **_kwargs: None,
    )


@pytest.fixture(autouse=True)
def _isolate_bootstrap_asset_root(
    monkeypatch: pytest.MonkeyPatch,
    source_checkout_root: Path,
) -> None:
    monkeypatch.setattr(
        bootstrap,
        "_bootstrap_asset_root_candidates",
        lambda: (source_checkout_root,),
    )


async def _ok_status_collector(
    settings: ServiceSettings,
    **_kwargs: object,
) -> dict[str, object]:
    return {"service": settings.service_name, "status": "ok", "checks": {}}


async def _no_sleep(_delay: float) -> None:
    return None


@pytest.mark.unit
def test_bootstrap_resolves_source_assets_when_called_outside_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_checkout_root: Path,
) -> None:
    calls: list[list[str]] = []
    source_root = source_checkout_root
    outside_cwd = tmp_path / "outside"
    outside_cwd.mkdir()
    monkeypatch.chdir(outside_cwd)

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    result = asyncio.run(
        run_service_bootstrap(
            _settings(tmp_path),
            options=ServiceBootstrapOptions(timeout_seconds=1, poll_interval_seconds=0.1),
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
        "-t",
        "awf-agent-runtime:latest",
        "-f",
        str(source_root / "docker/agent-runtime.Dockerfile"),
        str(source_root),
    ]
    assert calls[1][:4] == [
        "docker",
        "compose",
        "-f",
        str(source_root / "compose.yaml"),
    ]


@pytest.mark.unit
def test_bootstrap_fails_clearly_when_source_assets_are_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    isolated_root = tmp_path / "installed-package"
    isolated_root.mkdir()
    monkeypatch.setattr(
        bootstrap,
        "_bootstrap_asset_root_candidates",
        lambda: (isolated_root,),
    )

    with pytest.raises(ServiceBootstrapError) as exc_info:
        asyncio.run(
            run_service_bootstrap(
                _settings(tmp_path),
                options=ServiceBootstrapOptions(timeout_seconds=1, poll_interval_seconds=0.1),
                run_subprocess=lambda *_args, **_kwargs: pytest.fail(
                    "bootstrap should fail before invoking Docker"
                ),
                status_collector=_ok_status_collector,
                sleep=_no_sleep,
                monotonic=lambda: 0.0,
            )
        )

    assert exc_info.value.reason_code == "SERVICE_BOOTSTRAP_ASSETS_NOT_FOUND"
    assert "source checkout" in exc_info.value.message
    assert "docker/agent-runtime.Dockerfile" in exc_info.value.message


@pytest.mark.unit
def test_bootstrap_resolves_packaged_assets_when_source_checkout_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Installed wheels should carry a bootstrap build context."""
    packaged_root = _write_source_checkout(tmp_path / "packaged-assets")
    monkeypatch.setattr(bootstrap, "_bootstrap_asset_root_candidates", lambda: ())
    monkeypatch.setattr(bootstrap, "_packaged_bootstrap_asset_root", lambda: packaged_root)

    assert bootstrap.get_bootstrap_asset_root() == packaged_root


@pytest.mark.unit
def test_packaged_bootstrap_assets_use_local_env_seed_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Package installs should not write or read env files under site-packages."""
    packaged_root = _write_source_checkout(tmp_path / "packaged-assets")
    (packaged_root / ".env.example").write_text("AWF_API_TOKEN=example\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "_bootstrap_asset_root_candidates", lambda: ())
    monkeypatch.setattr(bootstrap, "_packaged_bootstrap_asset_root", lambda: packaged_root)
    monkeypatch.setattr(bootstrap, "LOCAL_SERVICE_COMPOSE_ENV_FILE", Path(".env"), raising=False)

    assets = bootstrap._resolve_bootstrap_assets(  # noqa: SLF001
        bootstrap.LOCAL_SERVICE_COMPOSE_FILE,
        require_agent_runtime=False,
    )

    assert assets.root == packaged_root
    assert assets.compose_file == packaged_root / bootstrap.LOCAL_SERVICE_COMPOSE_FILE
    assert assets.compose_env_file is None
    assert bootstrap._bootstrap_environment_file(assets) == Path(".env")  # noqa: SLF001


@pytest.mark.unit
def test_bootstrap_treats_absolute_asset_root_compose_path_as_default(
    monkeypatch: pytest.MonkeyPatch,
    source_checkout_root: Path,
) -> None:
    compose_file = source_checkout_root / bootstrap.LOCAL_SERVICE_COMPOSE_FILE

    def _fail_user_path_resolution(_path: Path) -> Path:
        pytest.fail("absolute local-service compose path should use default asset resolution")

    monkeypatch.setattr(bootstrap, "_resolve_user_path", _fail_user_path_resolution)

    assets = bootstrap._resolve_bootstrap_assets(  # noqa: SLF001
        compose_file,
        require_agent_runtime=False,
    )

    assert assets.root == source_checkout_root
    assert assets.compose_file == compose_file


@pytest.mark.unit
def test_bootstrap_resolves_custom_compose_paths_without_asset_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    relative_compose = tmp_path / "service.yml"
    relative_compose.write_text("services: {}\n", encoding="utf-8")
    fallback_env = tmp_path / "local.env"
    fallback_env.write_text("AWF_API_TOKEN=from-fallback\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap, "_bootstrap_asset_root_candidates", lambda: ())
    monkeypatch.setattr(
        bootstrap,
        "LOCAL_SERVICE_COMPOSE_ENV_FILE",
        Path("local.env"),
        raising=False,
    )

    assets = bootstrap._resolve_bootstrap_assets(  # noqa: SLF001
        Path("service.yml"),
        require_agent_runtime=False,
    )

    assert assets.root is None
    assert assets.agent_runtime_dockerfile is None
    assert assets.compose_file == relative_compose.resolve()
    assert assets.compose_env_file == fallback_env.resolve()


@pytest.mark.unit
def test_bootstrap_requires_source_assets_for_custom_runtime_build(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    custom_compose = tmp_path / "service.yml"
    custom_compose.write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "_bootstrap_asset_root_candidates", lambda: ())

    with pytest.raises(ServiceBootstrapError) as exc_info:
        bootstrap._resolve_bootstrap_assets(  # noqa: SLF001
            custom_compose,
            require_agent_runtime=True,
        )

    assert exc_info.value.reason_code == "SERVICE_BOOTSTRAP_ASSETS_NOT_FOUND"
