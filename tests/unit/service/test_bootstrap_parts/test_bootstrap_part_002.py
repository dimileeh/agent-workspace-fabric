"""Local service bootstrap orchestration tests."""

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
    **kwargs: object,
) -> dict[str, object]:
    return {"service": settings.service_name, "status": "ok", "checks": {}}


async def _no_sleep(_delay: float) -> None:
    return None


@pytest.mark.unit
def test_bootstrap_resolves_asset_root_compose_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset_root = tmp_path / "checkout"
    asset_root.mkdir()
    env_file = asset_root / "docker" / "compose" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("AWF_API_TOKEN=from-asset-root\n", encoding="utf-8")
    fallback_env_file = tmp_path / "docker" / "compose" / ".env"
    fallback_env_file.parent.mkdir(parents=True)
    fallback_env_file.write_text("AWF_API_TOKEN=from-fallback\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        bootstrap,
        "LOCAL_SERVICE_COMPOSE_ENV_FILE",
        Path("docker/compose/.env"),
        raising=False,
    )

    assert bootstrap._resolve_compose_env_file(asset_root) == env_file  # noqa: SLF001

    env_file.unlink()
    assert bootstrap._resolve_compose_env_file(asset_root) is None  # noqa: SLF001


@pytest.mark.unit
def test_bootstrap_starts_optional_ollama_bridge_when_profile_enabled(
    tmp_path: Path,
    source_checkout_root: Path,
) -> None:
    calls: list[list[str]] = []
    source_root = source_checkout_root

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    result = asyncio.run(
        run_service_bootstrap(
            _settings(tmp_path),
            options=ServiceBootstrapOptions(
                timeout_seconds=1,
                poll_interval_seconds=0.1,
                skip_agent_runtime_build=True,
            ),
            run_subprocess=_run,
            status_collector=_ok_status_collector,
            sleep=_no_sleep,
            monotonic=lambda: 0.0,
            provider_environ={"COMPOSE_PROFILES": "metrics,ollama-bridge"},
        )
    )

    assert result.service_status["status"] == "ok"
    assert [
        "docker",
        "compose",
        "-f",
        str(source_root / "docker/compose/local-service.yml"),
        "up",
        "-d",
        "--build",
        "ollama-bridge",
    ] in calls


@pytest.mark.unit
def test_bootstrap_starts_optional_ollama_bridge_with_lowercase_compose_profiles(
    tmp_path: Path,
    source_checkout_root: Path,
) -> None:
    calls: list[list[str]] = []
    source_root = source_checkout_root

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    result = asyncio.run(
        run_service_bootstrap(
            _settings(tmp_path),
            options=ServiceBootstrapOptions(
                timeout_seconds=1,
                poll_interval_seconds=0.1,
                skip_agent_runtime_build=True,
            ),
            run_subprocess=_run,
            status_collector=_ok_status_collector,
            sleep=_no_sleep,
            monotonic=lambda: 0.0,
            provider_environ={"compose_profiles": "metrics,ollama-bridge"},
        )
    )

    assert result.service_status["status"] == "ok"
    assert [
        "docker",
        "compose",
        "-f",
        str(source_root / "docker/compose/local-service.yml"),
        "up",
        "-d",
        "--build",
        "ollama-bridge",
    ] in calls


@pytest.mark.unit
def test_bootstrap_success_payload_includes_stage_output(tmp_path: Path) -> None:
    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[-1] == "migrate":
            return subprocess.CompletedProcess(
                args,
                returncode=0,
                stdout="alembic applied pending migrations\n",
                stderr="migration warning\n",
            )
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

    migrate_stage = result.to_dict()["stages"][2]
    assert migrate_stage["stdout"] == "alembic applied pending migrations\n"
    assert migrate_stage["stderr"] == "migration warning\n"


@pytest.mark.unit
def test_bootstrap_reruns_migrate_with_force_recreate(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    asyncio.run(
        run_service_bootstrap(
            _settings(tmp_path),
            options=ServiceBootstrapOptions(timeout_seconds=1, poll_interval_seconds=0.1),
            run_subprocess=_run,
            status_collector=_ok_status_collector,
            sleep=_no_sleep,
            monotonic=lambda: 0.0,
        )
    )

    migrate = calls[2]
    assert migrate[-3:] == ["--build", "--force-recreate", "migrate"]


@pytest.mark.unit
def test_bootstrap_polls_status_until_ok(tmp_path: Path) -> None:
    statuses = [
        {"status": "fail", "checks": {"api": {"ok": False}}},
        {"status": "ok", "checks": {"api": {"ok": True}}},
    ]
    sleeps: list[float] = []

    async def _collect(
        _settings: ServiceSettings,
        **_kwargs: object,
    ) -> dict[str, object]:
        return statuses.pop(0)

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    result = asyncio.run(
        run_service_bootstrap(
            _settings(tmp_path),
            options=ServiceBootstrapOptions(timeout_seconds=5, poll_interval_seconds=0.5),
            run_subprocess=_run,
            status_collector=_collect,
            sleep=_sleep,
            monotonic=lambda: 0.0,
        )
    )

    assert result.service_status["status"] == "ok"
    assert sleeps == [0.5]
    assert statuses == []


@pytest.mark.unit
def test_bootstrap_retries_status_collector_exceptions_until_ok(tmp_path: Path) -> None:
    attempts = 0
    sleeps: list[float] = []

    async def _collect(
        _settings: ServiceSettings,
        **_kwargs: object,
    ) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionRefusedError("api is still starting")
        return {"status": "ok", "checks": {"api": {"ok": True}}}

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    result = asyncio.run(
        run_service_bootstrap(
            _settings(tmp_path),
            options=ServiceBootstrapOptions(timeout_seconds=5, poll_interval_seconds=0.5),
            run_subprocess=_run,
            status_collector=_collect,
            sleep=_sleep,
            monotonic=lambda: 0.0,
        )
    )

    assert result.service_status["status"] == "ok"
    assert attempts == 2
    assert sleeps == [0.5]


@pytest.mark.unit
def test_bootstrap_times_out_after_repeated_status_collector_exceptions(
    tmp_path: Path,
) -> None:
    clock = 0.0
    attempts = 0
    sleeps: list[float] = []

    def _monotonic() -> float:
        return clock

    async def _sleep(delay: float) -> None:
        nonlocal clock
        sleeps.append(delay)
        clock += delay

    async def _collect(
        _settings: ServiceSettings,
        **_kwargs: object,
    ) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        raise ConnectionRefusedError("api is still starting")

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    with pytest.raises(ServiceBootstrapError) as exc_info:
        asyncio.run(
            run_service_bootstrap(
                _settings(tmp_path),
                options=ServiceBootstrapOptions(timeout_seconds=2, poll_interval_seconds=1),
                run_subprocess=_run,
                status_collector=_collect,
                sleep=_sleep,
                monotonic=_monotonic,
            )
        )

    assert attempts == 3
    assert sleeps == [1, 1]
    assert exc_info.value.reason_code == "SERVICE_BOOTSTRAP_TIMEOUT"
    assert exc_info.value.last_status is not None
    collector_check = exc_info.value.last_status["checks"]["status_collector"]
    assert collector_check["reason"] == "STATUS_COLLECTION_FAILED"
    assert collector_check["detail"] == "ConnectionRefusedError: api is still starting"


@pytest.mark.unit
def test_bootstrap_timeout_reports_last_status(tmp_path: Path) -> None:
    clock = 0.0
    last_status = {"status": "fail", "checks": {"api": {"reason": "API_UNREACHABLE"}}}

    def _monotonic() -> float:
        return clock

    async def _sleep(delay: float) -> None:
        nonlocal clock
        clock += delay

    async def _collect(
        _settings: ServiceSettings,
        **_kwargs: object,
    ) -> dict[str, object]:
        return dict(last_status)

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    with pytest.raises(ServiceBootstrapError) as exc_info:
        asyncio.run(
            run_service_bootstrap(
                _settings(tmp_path),
                options=ServiceBootstrapOptions(timeout_seconds=2, poll_interval_seconds=1),
                run_subprocess=_run,
                status_collector=_collect,
                sleep=_sleep,
                monotonic=_monotonic,
            )
        )

    assert exc_info.value.reason_code == "SERVICE_BOOTSTRAP_TIMEOUT"
    assert exc_info.value.last_status == last_status
    assert exc_info.value.to_dict()["last_status"] == last_status


@pytest.mark.unit
def test_bootstrap_compose_failure_stops_with_stage_context(
    tmp_path: Path,
    source_checkout_root: Path,
) -> None:
    calls: list[list[str]] = []
    status_calls = 0

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[-1] == "migrate":
            return subprocess.CompletedProcess(
                args,
                returncode=17,
                stdout="migration stdout",
                stderr="migration failed",
            )
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    async def _collect(_settings: ServiceSettings, **_kwargs: object) -> dict[str, object]:
        nonlocal status_calls
        status_calls += 1
        return {"status": "ok"}

    with pytest.raises(ServiceBootstrapError) as exc_info:
        asyncio.run(
            run_service_bootstrap(
                _settings(tmp_path),
                options=ServiceBootstrapOptions(timeout_seconds=1, poll_interval_seconds=0.1),
                run_subprocess=_run,
                status_collector=_collect,
                sleep=_no_sleep,
                monotonic=lambda: 0.0,
            )
        )

    assert [Path(calls[0][-1]), calls[1][-1], calls[2][-1]] == [
        source_checkout_root,
        "postgres",
        "migrate",
    ]
    assert status_calls == 0
    assert exc_info.value.reason_code == "SERVICE_BOOTSTRAP_STAGE_FAILED"
    assert exc_info.value.stage == "migrate"
    assert exc_info.value.returncode == 17
    assert exc_info.value.stdout == "migration stdout"
    assert exc_info.value.stderr == "migration failed"


@pytest.mark.unit
def test_bootstrap_missing_docker_binary_reports_stage_context(tmp_path: Path) -> None:
    def _run(_args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("docker")

    with pytest.raises(ServiceBootstrapError) as exc_info:
        asyncio.run(
            run_service_bootstrap(
                _settings(tmp_path),
                options=ServiceBootstrapOptions(timeout_seconds=1, poll_interval_seconds=0.1),
                run_subprocess=_run,
                status_collector=_ok_status_collector,
                sleep=_no_sleep,
                monotonic=lambda: 0.0,
            )
        )

    payload = exc_info.value.to_dict()
    assert payload["reason_code"] == "SERVICE_BOOTSTRAP_STAGE_FAILED"
    assert payload["stage"] == "agent_runtime_build"
    assert payload["returncode"] == 127
    assert payload["stderr"] == "docker binary not found on PATH"


@pytest.mark.unit
def test_bootstrap_os_error_reports_stage_context(tmp_path: Path) -> None:
    def _run(_args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("permission denied")

    with pytest.raises(ServiceBootstrapError) as exc_info:
        asyncio.run(
            run_service_bootstrap(
                _settings(tmp_path),
                options=ServiceBootstrapOptions(timeout_seconds=1, poll_interval_seconds=0.1),
                run_subprocess=_run,
                status_collector=_ok_status_collector,
                sleep=_no_sleep,
                monotonic=lambda: 0.0,
            )
        )

    assert exc_info.value.reason_code == "SERVICE_BOOTSTRAP_STAGE_FAILED"
    assert exc_info.value.stage == "agent_runtime_build"
    assert exc_info.value.returncode == 1
    assert exc_info.value.stderr == "OSError: permission denied"


@pytest.mark.unit
def test_bootstrap_error_payload_includes_all_optional_fields() -> None:
    error = ServiceBootstrapError(
        reason_code="SERVICE_BOOTSTRAP_STAGE_FAILED",
        message="stage failed",
        stage="migrate",
        command=("docker", "compose", "up", "migrate"),
        returncode=17,
        stdout="migration stdout",
        stderr="migration stderr",
        last_status={"status": "fail"},
    )

    assert error.to_dict() == {
        "status": "failed",
        "reason_code": "SERVICE_BOOTSTRAP_STAGE_FAILED",
        "message": "stage failed",
        "stage": "migrate",
        "command": ["docker", "compose", "up", "migrate"],
        "returncode": 17,
        "stdout": "migration stdout",
        "stderr": "migration stderr",
        "last_status": {"status": "fail"},
    }


@pytest.mark.unit
def test_run_subprocess_delegates_to_subprocess_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(bootstrap.subprocess, "run", _run)

    result = bootstrap._run_subprocess(  # noqa: SLF001
        ["docker", "version"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert calls == [
        {
            "args": ["docker", "version"],
            "check": False,
            "capture_output": True,
            "text": True,
        }
    ]


@pytest.mark.unit
def test_bootstrap_skip_agent_runtime_build_omits_build_command(
    tmp_path: Path,
    source_checkout_root: Path,
) -> None:
    calls: list[list[str]] = []
    compose_file = str(source_checkout_root / "docker/compose/local-service.yml")

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    result = asyncio.run(
        run_service_bootstrap(
            _settings(tmp_path),
            options=ServiceBootstrapOptions(
                timeout_seconds=1,
                poll_interval_seconds=0.1,
                skip_agent_runtime_build=True,
            ),
            run_subprocess=_run,
            status_collector=_ok_status_collector,
            sleep=_no_sleep,
            monotonic=lambda: 0.0,
        )
    )

    assert result.service_status["status"] == "ok"
    assert calls == [
        [
            "docker",
            "compose",
            "-f",
            compose_file,
            "up",
            "-d",
            "--build",
            "postgres",
        ],
        [
            "docker",
            "compose",
            "-f",
            compose_file,
            "up",
            "--build",
            "--force-recreate",
            "migrate",
        ],
        [
            "docker",
            "compose",
            "-f",
            compose_file,
            "up",
            "-d",
            "--build",
            "api",
            "worker",
        ],
    ]
