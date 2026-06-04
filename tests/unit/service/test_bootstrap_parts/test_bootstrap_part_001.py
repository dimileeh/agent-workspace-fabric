"""Local service bootstrap orchestration tests."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import threading
from collections.abc import Iterable, Mapping
from dataclasses import replace
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
def test_bootstrap_stage_runner_does_not_block_event_loop(tmp_path: Path) -> None:
    stage_started = threading.Event()
    release_stage = threading.Event()
    observed_event_loop_progress = False
    calls = 0

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            stage_started.set()
            if not release_stage.wait(timeout=0.5):
                return subprocess.CompletedProcess(
                    args,
                    returncode=99,
                    stdout="",
                    stderr="event loop stalled while stage was running",
                )
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    async def _release_stage_after_loop_progress() -> None:
        nonlocal observed_event_loop_progress
        while not stage_started.is_set():
            await asyncio.sleep(0)
        observed_event_loop_progress = True
        release_stage.set()

    async def _exercise() -> None:
        releaser = asyncio.create_task(_release_stage_after_loop_progress())
        try:
            await run_service_bootstrap(
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
        finally:
            if not releaser.done():
                releaser.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await releaser

    asyncio.run(_exercise())

    assert observed_event_loop_progress is True


@pytest.mark.unit
def test_bootstrap_runs_expected_command_sequence(
    tmp_path: Path,
    source_checkout_root: Path,
) -> None:
    calls: list[list[str]] = []
    source_root = source_checkout_root
    expected_env = None

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        expected_kwargs = {
            "check": False,
            "capture_output": True,
            "text": True,
        }
        if expected_env is not None:
            expected_kwargs["env"] = expected_env
        assert kwargs == expected_kwargs
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
    assert calls == [
        [
            "docker",
            "build",
            "-t",
            "awf-agent-runtime:latest",
            "-f",
            str(source_root / "docker/agent-runtime.Dockerfile"),
            str(source_root),
        ],
        [
            "docker",
            "compose",
            "-f",
            str(source_root / "docker/compose/local-service.yml"),
            "up",
            "-d",
            "--build",
            "postgres",
        ],
        [
            "docker",
            "compose",
            "-f",
            str(source_root / "docker/compose/local-service.yml"),
            "up",
            "--build",
            "--force-recreate",
            "migrate",
        ],
        [
            "docker",
            "compose",
            "-f",
            str(source_root / "docker/compose/local-service.yml"),
            "up",
            "-d",
            "--build",
            "api",
            "worker",
        ],
    ]


@pytest.mark.unit
def test_bootstrap_passes_merged_service_environment_to_docker_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service_env = {
        "AWF_DATABASE_URL": "postgresql+asyncpg://awf:compose-secret@localhost:5433/awf",
        "AWF_POSTGRES_PASSWORD": "compose-secret",
    }
    monkeypatch.setattr(bootstrap, "local_service_environ", lambda **_kwargs: dict(service_env))
    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
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
    assert calls
    expected_env = dict(os.environ, **service_env)
    assert all(call["env"] == expected_env for call in calls)


@pytest.mark.unit
def test_bootstrap_uses_explicit_service_environment_without_reloading_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service_env = {
        "AWF_DATABASE_URL": "postgresql+asyncpg://awf:preflight@localhost:5433/awf",
        "AWF_POSTGRES_PASSWORD": "preflight",
    }
    monkeypatch.setattr(
        bootstrap,
        "local_service_environ",
        lambda **_kwargs: pytest.fail("bootstrap should reuse the preflight env"),
    )
    calls: list[dict[str, object]] = []
    collected_provider_environ: dict[str, str] | None = None

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    async def _collect(
        settings: ServiceSettings,
        *,
        strict_providers: Iterable[str] | None = None,
        provider_environ: Mapping[str, str] | None = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        nonlocal collected_provider_environ
        _ = settings, strict_providers
        collected_provider_environ = dict(provider_environ or {})
        return {"service": settings.service_name, "status": "ok", "checks": {}}

    result = asyncio.run(
        run_service_bootstrap(
            _settings(tmp_path),
            options=ServiceBootstrapOptions(
                timeout_seconds=1,
                poll_interval_seconds=0.1,
                skip_agent_runtime_build=True,
            ),
            run_subprocess=_run,
            status_collector=_collect,
            sleep=_no_sleep,
            monotonic=lambda: 0.0,
            service_environ=service_env,
        )
    )

    assert result.service_status["status"] == "ok"
    assert collected_provider_environ == service_env
    assert calls
    expected_subprocess_env = dict(os.environ, **service_env)
    assert all(call["env"] == expected_subprocess_env for call in calls)


@pytest.mark.unit
def test_bootstrap_mirrors_awf_docker_host_to_docker_cli_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    docker_host = f"unix://{tmp_path / 'compose-docker.sock'}"
    service_env = {"AWF_DOCKER_HOST": docker_host}
    expected_env = {"DOCKER_HOST": docker_host}
    monkeypatch.setattr(bootstrap, "local_service_environ", lambda **_kwargs: dict(service_env))
    calls: list[dict[str, object]] = []
    collected_provider_environ: dict[str, str] | None = None

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    async def _collect(
        settings: ServiceSettings,
        *,
        strict_providers: Iterable[str] | None = None,
        provider_environ: Mapping[str, str] | None = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        nonlocal collected_provider_environ
        _ = strict_providers
        collected_provider_environ = dict(provider_environ or {})
        return {"service": settings.service_name, "status": "ok", "checks": {}}

    result = asyncio.run(
        run_service_bootstrap(
            replace(_settings(tmp_path), docker_host=docker_host),
            options=ServiceBootstrapOptions(
                timeout_seconds=1,
                poll_interval_seconds=0.1,
                skip_agent_runtime_build=True,
            ),
            run_subprocess=_run,
            status_collector=_collect,
            sleep=_no_sleep,
            monotonic=lambda: 0.0,
        )
    )

    assert result.service_status["status"] == "ok"
    assert collected_provider_environ == expected_env
    assert calls
    expected_subprocess_env = dict(os.environ, **expected_env)
    assert all(call["env"] == expected_subprocess_env for call in calls)
    assert all("AWF_DOCKER_HOST" not in call["env"] for call in calls)


@pytest.mark.unit
def test_bootstrap_mirrors_mixed_case_awf_docker_host_to_docker_cli_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    docker_host = f"unix://{tmp_path / 'compose-docker.sock'}"
    service_env = {
        "AwF_DoCkEr_HoSt": docker_host,
        "DOCKER_HOST": "unix:///stale-docker.sock",
    }
    expected_env = {"DOCKER_HOST": docker_host}
    monkeypatch.setattr(bootstrap, "local_service_environ", lambda **_kwargs: dict(service_env))
    calls: list[dict[str, object]] = []
    collected_provider_environ: dict[str, str] | None = None

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    async def _collect(
        settings: ServiceSettings,
        *,
        strict_providers: Iterable[str] | None = None,
        provider_environ: Mapping[str, str] | None = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        nonlocal collected_provider_environ
        _ = settings, strict_providers
        collected_provider_environ = dict(provider_environ or {})
        return {"service": settings.service_name, "status": "ok", "checks": {}}

    result = asyncio.run(
        run_service_bootstrap(
            _settings(tmp_path),
            options=ServiceBootstrapOptions(
                timeout_seconds=1,
                poll_interval_seconds=0.1,
                skip_agent_runtime_build=True,
            ),
            run_subprocess=_run,
            status_collector=_collect,
            sleep=_no_sleep,
            monotonic=lambda: 0.0,
        )
    )

    assert result.service_status["status"] == "ok"
    assert collected_provider_environ == expected_env
    assert calls
    expected_subprocess_env = dict(os.environ, **expected_env)
    assert all(call["env"] == expected_subprocess_env for call in calls)
    assert all(not any(key.upper() == "AWF_DOCKER_HOST" for key in call["env"]) for call in calls)


@pytest.mark.unit
def test_bootstrap_mirrors_runtime_awf_docker_host_when_settings_differ(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_docker_host = f"unix://{tmp_path / 'runtime-docker.sock'}"
    settings_docker_host = f"unix://{tmp_path / 'settings-docker.sock'}"
    service_env = {"AWF_DOCKER_HOST": runtime_docker_host}
    expected_env = {"DOCKER_HOST": runtime_docker_host}
    monkeypatch.setattr(bootstrap, "local_service_environ", lambda **_kwargs: dict(service_env))
    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    result = asyncio.run(
        run_service_bootstrap(
            replace(_settings(tmp_path), docker_host=settings_docker_host),
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
    assert calls
    expected_subprocess_env = dict(os.environ, **expected_env)
    assert all(call["env"] == expected_subprocess_env for call in calls)
    assert all("AWF_DOCKER_HOST" not in call["env"] for call in calls)


@pytest.mark.unit
def test_bootstrap_overrides_stale_docker_host_with_runtime_awf_docker_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_docker_host = f"unix://{tmp_path / 'runtime-docker.sock'}"
    stale_docker_host = f"unix://{tmp_path / 'stale-docker.sock'}"
    service_env = {
        "AWF_DOCKER_HOST": runtime_docker_host,
        "DOCKER_HOST": stale_docker_host,
    }
    expected_env = {"DOCKER_HOST": runtime_docker_host}
    monkeypatch.setattr(bootstrap, "local_service_environ", lambda **_kwargs: dict(service_env))
    calls: list[dict[str, object]] = []
    collected_provider_environ: dict[str, str] | None = None

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    async def _collect(
        settings: ServiceSettings,
        *,
        strict_providers: Iterable[str] | None = None,
        provider_environ: Mapping[str, str] | None = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        nonlocal collected_provider_environ
        _ = settings, strict_providers
        collected_provider_environ = dict(provider_environ or {})
        return {"service": settings.service_name, "status": "ok", "checks": {}}

    result = asyncio.run(
        run_service_bootstrap(
            replace(_settings(tmp_path), docker_host=runtime_docker_host),
            options=ServiceBootstrapOptions(
                timeout_seconds=1,
                poll_interval_seconds=0.1,
                skip_agent_runtime_build=True,
            ),
            run_subprocess=_run,
            status_collector=_collect,
            sleep=_no_sleep,
            monotonic=lambda: 0.0,
        )
    )

    assert result.service_status["status"] == "ok"
    assert collected_provider_environ == expected_env
    assert calls
    expected_subprocess_env = dict(os.environ, **expected_env)
    assert all(call["env"] == expected_subprocess_env for call in calls)
    assert all("AWF_DOCKER_HOST" not in call["env"] for call in calls)


@pytest.mark.unit
def test_bootstrap_removes_stale_caller_docker_host_variants_when_awf_host_is_forced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_docker_host = f"unix://{tmp_path / 'runtime-docker.sock'}"
    service_env = {"AWF_DOCKER_HOST": runtime_docker_host}
    expected_provider_env = {"DOCKER_HOST": runtime_docker_host}
    monkeypatch.setenv("DoCkEr_HoSt", "unix:///caller-stale-docker.sock")
    monkeypatch.setattr(bootstrap, "local_service_environ", lambda **_kwargs: dict(service_env))
    calls: list[dict[str, object]] = []
    collected_provider_environ: dict[str, str] | None = None

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    async def _collect(
        settings: ServiceSettings,
        *,
        strict_providers: Iterable[str] | None = None,
        provider_environ: Mapping[str, str] | None = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        nonlocal collected_provider_environ
        _ = settings, strict_providers
        collected_provider_environ = dict(provider_environ or {})
        return {"service": settings.service_name, "status": "ok", "checks": {}}

    result = asyncio.run(
        run_service_bootstrap(
            replace(_settings(tmp_path), docker_host=runtime_docker_host),
            options=ServiceBootstrapOptions(
                timeout_seconds=1,
                poll_interval_seconds=0.1,
                skip_agent_runtime_build=True,
            ),
            run_subprocess=_run,
            status_collector=_collect,
            sleep=_no_sleep,
            monotonic=lambda: 0.0,
        )
    )

    assert result.service_status["status"] == "ok"
    assert collected_provider_environ == expected_provider_env
    assert calls
    for call in calls:
        env = call["env"]
        assert isinstance(env, dict)
        assert env["DOCKER_HOST"] == runtime_docker_host
        assert [key for key in env if key.upper() == "DOCKER_HOST"] == ["DOCKER_HOST"]
        assert not any(key.upper() == "AWF_DOCKER_HOST" for key in env)


@pytest.mark.unit
def test_bootstrap_clears_docker_context_when_awf_docker_host_is_forced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_docker_host = f"unix://{tmp_path / 'runtime-docker.sock'}"
    service_env = {
        "AWF_DOCKER_HOST": runtime_docker_host,
        "DOCKER_CONTEXT": "stale-desktop-context",
    }
    expected_env = {"DOCKER_HOST": runtime_docker_host}
    monkeypatch.setattr(bootstrap, "local_service_environ", lambda **_kwargs: dict(service_env))
    calls: list[dict[str, object]] = []
    collected_provider_environ: dict[str, str] | None = None

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    async def _collect(
        settings: ServiceSettings,
        *,
        strict_providers: Iterable[str] | None = None,
        provider_environ: Mapping[str, str] | None = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        nonlocal collected_provider_environ
        _ = settings, strict_providers
        collected_provider_environ = dict(provider_environ or {})
        return {"service": settings.service_name, "status": "ok", "checks": {}}

    result = asyncio.run(
        run_service_bootstrap(
            replace(_settings(tmp_path), docker_host=runtime_docker_host),
            options=ServiceBootstrapOptions(
                timeout_seconds=1,
                poll_interval_seconds=0.1,
                skip_agent_runtime_build=True,
            ),
            run_subprocess=_run,
            status_collector=_collect,
            sleep=_no_sleep,
            monotonic=lambda: 0.0,
        )
    )

    assert result.service_status["status"] == "ok"
    assert collected_provider_environ == expected_env
    assert calls
    expected_subprocess_env = dict(os.environ, **expected_env)
    expected_subprocess_env.pop("DOCKER_CONTEXT", None)
    assert all(call["env"] == expected_subprocess_env for call in calls)
    assert all("AWF_DOCKER_HOST" not in call["env"] for call in calls)
    assert all("DOCKER_CONTEXT" not in call["env"] for call in calls)


@pytest.mark.unit
def test_bootstrap_clears_docker_context_when_docker_host_is_resolved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_docker_host = f"unix://{tmp_path / 'runtime-docker.sock'}"
    service_env = {
        "DOCKER_HOST": runtime_docker_host,
        "DOCKER_CONTEXT": "service-stale-context",
    }
    expected_env = {"DOCKER_HOST": runtime_docker_host}
    monkeypatch.setenv("DOCKER_HOST", "unix:///caller-stale-docker.sock")
    monkeypatch.setenv("DOCKER_CONTEXT", "caller-stale-context")
    monkeypatch.setattr(bootstrap, "local_service_environ", lambda **_kwargs: dict(service_env))
    calls: list[dict[str, object]] = []
    collected_provider_environ: dict[str, str] | None = None

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    async def _collect(
        settings: ServiceSettings,
        *,
        strict_providers: Iterable[str] | None = None,
        provider_environ: Mapping[str, str] | None = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        nonlocal collected_provider_environ
        _ = settings, strict_providers
        collected_provider_environ = dict(provider_environ or {})
        return {"service": settings.service_name, "status": "ok", "checks": {}}

    result = asyncio.run(
        run_service_bootstrap(
            replace(_settings(tmp_path), docker_host=runtime_docker_host),
            options=ServiceBootstrapOptions(
                timeout_seconds=1,
                poll_interval_seconds=0.1,
                skip_agent_runtime_build=True,
            ),
            run_subprocess=_run,
            status_collector=_collect,
            sleep=_no_sleep,
            monotonic=lambda: 0.0,
        )
    )

    assert result.service_status["status"] == "ok"
    assert collected_provider_environ == expected_env
    assert calls
    expected_subprocess_env = dict(os.environ, **expected_env)
    expected_subprocess_env.pop("DOCKER_CONTEXT", None)
    assert all(call["env"] == expected_subprocess_env for call in calls)
    assert all("AWF_DOCKER_HOST" not in call["env"] for call in calls)
    assert all("DOCKER_CONTEXT" not in call["env"] for call in calls)


@pytest.mark.unit
def test_bootstrap_blank_docker_host_clears_stale_caller_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service_env = {"DOCKER_HOST": ""}
    monkeypatch.setenv("DOCKER_HOST", "unix:///caller-stale-docker.sock")
    monkeypatch.setenv("DOCKER_CONTEXT", "caller-stale-context")
    monkeypatch.setattr(bootstrap, "local_service_environ", lambda **_kwargs: dict(service_env))
    calls: list[dict[str, object]] = []
    collected_provider_environ: dict[str, str] | None = None

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    async def _collect(
        settings: ServiceSettings,
        *,
        strict_providers: Iterable[str] | None = None,
        provider_environ: Mapping[str, str] | None = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        nonlocal collected_provider_environ
        _ = settings, strict_providers
        collected_provider_environ = dict(provider_environ or {})
        return {"service": settings.service_name, "status": "ok", "checks": {}}

    result = asyncio.run(
        run_service_bootstrap(
            _settings(tmp_path),
            options=ServiceBootstrapOptions(
                timeout_seconds=1,
                poll_interval_seconds=0.1,
                skip_agent_runtime_build=True,
            ),
            run_subprocess=_run,
            status_collector=_collect,
            sleep=_no_sleep,
            monotonic=lambda: 0.0,
        )
    )

    assert result.service_status["status"] == "ok"
    assert collected_provider_environ == {}
    assert calls
    assert all("DOCKER_HOST" not in call["env"] for call in calls)
    assert all("DOCKER_CONTEXT" not in call["env"] for call in calls)


@pytest.mark.unit
def test_bootstrap_scrubs_explicitly_cleared_docker_cli_client_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service_env = {"DOCKER_CONFIG": ""}
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "caller-docker-config"))
    monkeypatch.setattr(bootstrap, "local_service_environ", lambda **_kwargs: dict(service_env))
    calls: list[dict[str, object]] = []
    collected_provider_environ: dict[str, str] | None = None

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    async def _collect(
        settings: ServiceSettings,
        *,
        strict_providers: Iterable[str] | None = None,
        provider_environ: Mapping[str, str] | None = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        nonlocal collected_provider_environ
        _ = settings, strict_providers
        collected_provider_environ = dict(provider_environ or {})
        return {"service": settings.service_name, "status": "ok", "checks": {}}

    result = asyncio.run(
        run_service_bootstrap(
            _settings(tmp_path),
            options=ServiceBootstrapOptions(
                timeout_seconds=1,
                poll_interval_seconds=0.1,
                skip_agent_runtime_build=True,
            ),
            run_subprocess=_run,
            status_collector=_collect,
            sleep=_no_sleep,
            monotonic=lambda: 0.0,
        )
    )

    assert result.service_status["status"] == "ok"
    assert collected_provider_environ == {}
    assert calls
    assert all("DOCKER_CONFIG" not in call["env"] for call in calls)


@pytest.mark.unit
def test_bootstrap_passes_explicit_empty_service_environment_to_docker_commands(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
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
    assert calls
    expected_env = None
    assert [call.get("env") for call in calls] == [expected_env] * len(calls)


@pytest.mark.unit
def test_bootstrap_partial_provider_environment_preserves_local_service_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    local_env = {
        "PATH": "/usr/local/bin:/usr/bin",
        "HOME": str(tmp_path / "home"),
        "DOCKER_HOST": "unix:///var/run/docker.sock",
    }
    provider_env = {"COMPOSE_PROFILES": "metrics,ollama-bridge"}
    expected_provider_environ = {**local_env, **provider_env}
    expected_subprocess_environ = {**os.environ, **expected_provider_environ}
    monkeypatch.setattr(bootstrap, "local_service_environ", lambda **_kwargs: dict(local_env))
    calls: list[dict[str, object]] = []
    collected_provider_environ: dict[str, str] | None = None

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    async def _collect(
        settings: ServiceSettings,
        *,
        strict_providers: Iterable[str] | None = None,
        provider_environ: Mapping[str, str] | None = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        nonlocal collected_provider_environ
        _ = settings, strict_providers
        collected_provider_environ = dict(provider_environ or {})
        return {"service": settings.service_name, "status": "ok", "checks": {}}

    result = asyncio.run(
        run_service_bootstrap(
            _settings(tmp_path),
            options=ServiceBootstrapOptions(
                timeout_seconds=1,
                poll_interval_seconds=0.1,
                skip_agent_runtime_build=True,
            ),
            run_subprocess=_run,
            status_collector=_collect,
            sleep=_no_sleep,
            monotonic=lambda: 0.0,
            provider_environ=provider_env,
        )
    )

    assert result.service_status["status"] == "ok"
    assert collected_provider_environ == expected_provider_environ
    assert calls
    assert all(call["env"] == expected_subprocess_environ for call in calls)
    assert any(call["args"][-1] == "ollama-bridge" for call in calls)


@pytest.mark.unit
def test_bootstrap_uses_ambient_compose_profiles_for_ollama_bridge_stage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("COMPOSE_PROFILES", "metrics,ollama-bridge")
    monkeypatch.setattr(bootstrap, "local_service_environ", lambda **_kwargs: {})
    calls: list[dict[str, object]] = []
    collected_provider_environ: dict[str, str] | None = None

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    async def _collect(
        settings: ServiceSettings,
        *,
        strict_providers: Iterable[str] | None = None,
        provider_environ: Mapping[str, str] | None = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        nonlocal collected_provider_environ
        _ = settings, strict_providers
        collected_provider_environ = dict(provider_environ or {})
        return {"service": settings.service_name, "status": "ok", "checks": {}}

    result = asyncio.run(
        run_service_bootstrap(
            _settings(tmp_path),
            options=ServiceBootstrapOptions(
                timeout_seconds=1,
                poll_interval_seconds=0.1,
                skip_agent_runtime_build=True,
            ),
            run_subprocess=_run,
            status_collector=_collect,
            sleep=_no_sleep,
            monotonic=lambda: 0.0,
        )
    )

    assert result.service_status["status"] == "ok"
    assert collected_provider_environ == {}
    assert any(call["args"][-1] == "ollama-bridge" for call in calls)


@pytest.mark.unit
def test_bootstrap_does_not_overlay_provider_environment_on_cwd_compose_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service.config import local_service_environ

    asset_root = tmp_path / "checkout"
    (asset_root / "docker" / "compose").mkdir(parents=True)
    (asset_root / "docker" / "agent-runtime.Dockerfile").write_text(
        "FROM scratch\n",
        encoding="utf-8",
    )
    (asset_root / "docker" / "control-plane.Dockerfile").write_text(
        "FROM scratch\n",
        encoding="utf-8",
    )
    (asset_root / "docker" / "compose" / "local-service.yml").write_text(
        "services: {}\n",
        encoding="utf-8",
    )
    (asset_root / "pyproject.toml").write_text("[project]\nname = 'awf'\n", encoding="utf-8")
    (asset_root / "src" / "awf").mkdir(parents=True)
    (asset_root / "src" / "awf" / "__init__.py").write_text("", encoding="utf-8")

    intended_database_url = "postgresql+asyncpg://awf:intended@asset:5432/awf"
    (asset_root / "docker" / "compose" / ".env").write_text(
        f"AWF_DATABASE_URL={intended_database_url}\n",
        encoding="utf-8",
    )

    unrelated_cwd = tmp_path / "unrelated-project"
    (unrelated_cwd / "docker" / "compose").mkdir(parents=True)
    (unrelated_cwd / "docker" / "compose" / ".env").write_text(
        "\n".join(
            [
                "AWF_DATABASE_URL=postgresql+asyncpg://awf:stale@cwd:5432/awf",
                "COMPOSE_PROFILES=ollama-bridge",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(unrelated_cwd)
    monkeypatch.setattr(bootstrap, "_bootstrap_asset_root_candidates", lambda: (asset_root,))
    monkeypatch.setattr(
        bootstrap,
        "LOCAL_SERVICE_COMPOSE_ENV_FILE",
        Path("docker/compose/.env"),
        raising=False,
    )
    monkeypatch.setattr(bootstrap, "local_service_environ", local_service_environ)
    for key in ("AWF_DATABASE_URL", "AWF_POSTGRES_PASSWORD", "COMPOSE_PROFILES"):
        monkeypatch.delenv(key, raising=False)

    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
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
            provider_environ={"AWF_DATABASE_URL": intended_database_url},
        )
    )

    assert result.service_status["status"] == "ok"
    assert calls
    assert not any(call["args"][-1] == "ollama-bridge" for call in calls)
    for call in calls:
        environ = call["env"]
        assert isinstance(environ, dict)
        assert environ["AWF_DATABASE_URL"] == intended_database_url
        assert "COMPOSE_PROFILES" not in environ


@pytest.mark.unit
def test_bootstrap_uses_explicit_env_file_instead_of_internally_resolved_compose_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service.config import local_service_environ

    asset_root = tmp_path / "checkout"
    (asset_root / "docker" / "compose").mkdir(parents=True)
    (asset_root / "docker" / "agent-runtime.Dockerfile").write_text(
        "FROM scratch\n",
        encoding="utf-8",
    )
    (asset_root / "docker" / "control-plane.Dockerfile").write_text(
        "FROM scratch\n",
        encoding="utf-8",
    )
    (asset_root / "docker" / "compose" / "local-service.yml").write_text(
        "services: {}\n",
        encoding="utf-8",
    )
    (asset_root / "pyproject.toml").write_text("[project]\nname = 'awf'\n", encoding="utf-8")
    (asset_root / "src" / "awf").mkdir(parents=True)
    (asset_root / "src" / "awf" / "__init__.py").write_text("", encoding="utf-8")

    stale_env = asset_root / "docker" / "compose" / ".env"
    stale_env.write_text(
        "\n".join(
            [
                "AWF_DATABASE_URL=postgresql+asyncpg://awf:stale@asset:5432/awf",
                "COMPOSE_PROFILES=ollama-bridge",
            ]
        ),
        encoding="utf-8",
    )
    intended_env = tmp_path / "resolved" / ".env"
    intended_env.parent.mkdir(parents=True)
    intended_database_url = "postgresql+asyncpg://awf:intended@resolved:5432/awf"
    intended_postgres_password = "explicit-env-file-only"
    intended_env.write_text(
        "\n".join(
            [
                f"AWF_DATABASE_URL={intended_database_url}",
                f"AWF_POSTGRES_PASSWORD={intended_postgres_password}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap, "_bootstrap_asset_root_candidates", lambda: (asset_root,))
    monkeypatch.setattr(
        bootstrap,
        "LOCAL_SERVICE_COMPOSE_ENV_FILE",
        Path("docker/compose/.env"),
        raising=False,
    )
    monkeypatch.setattr(bootstrap, "local_service_environ", local_service_environ)
    for key in ("AWF_DATABASE_URL", "AWF_POSTGRES_PASSWORD", "COMPOSE_PROFILES"):
        monkeypatch.delenv(key, raising=False)

    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
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
            env_file=intended_env,
        )
    )

    assert result.service_status["status"] == "ok"
    assert calls
    assert not any(call["args"][-1] == "ollama-bridge" for call in calls)
    for call in calls:
        args = call["args"]
        environ = call["env"]
        assert isinstance(args, list)
        assert isinstance(environ, dict)
        assert environ["AWF_DATABASE_URL"] == intended_database_url
        assert environ["AWF_POSTGRES_PASSWORD"] == intended_postgres_password
        assert "COMPOSE_PROFILES" not in environ
        assert str(stale_env) not in args
        assert str(intended_env) in args


@pytest.mark.unit
def test_bootstrap_falls_back_to_resolved_env_when_explicit_env_file_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service.config import local_service_environ

    asset_root = tmp_path / "checkout"
    (asset_root / "docker" / "compose").mkdir(parents=True)
    (asset_root / "docker" / "agent-runtime.Dockerfile").write_text(
        "FROM scratch\n",
        encoding="utf-8",
    )
    (asset_root / "docker" / "control-plane.Dockerfile").write_text(
        "FROM scratch\n",
        encoding="utf-8",
    )
    (asset_root / "docker" / "compose" / "local-service.yml").write_text(
        "services: {}\n",
        encoding="utf-8",
    )
    (asset_root / "pyproject.toml").write_text("[project]\nname = 'awf'\n", encoding="utf-8")
    (asset_root / "src" / "awf").mkdir(parents=True)
    (asset_root / "src" / "awf" / "__init__.py").write_text("", encoding="utf-8")

    fallback_database_url = "postgresql+asyncpg://awf:fallback@asset:5432/awf"
    fallback_env = asset_root / "docker" / "compose" / ".env"
    fallback_env.write_text(
        "\n".join(
            [
                f"AWF_DATABASE_URL={fallback_database_url}",
                "COMPOSE_PROFILES=ollama-bridge",
            ]
        ),
        encoding="utf-8",
    )
    missing_env = tmp_path / "resolved" / ".env"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap, "_bootstrap_asset_root_candidates", lambda: (asset_root,))
    monkeypatch.setattr(
        bootstrap,
        "LOCAL_SERVICE_COMPOSE_ENV_FILE",
        Path("docker/compose/.env"),
        raising=False,
    )
    monkeypatch.setattr(bootstrap, "local_service_environ", local_service_environ)
    for key in ("AWF_DATABASE_URL", "AWF_POSTGRES_PASSWORD", "COMPOSE_PROFILES"):
        monkeypatch.delenv(key, raising=False)

    calls: list[dict[str, object]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
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
            env_file=missing_env,
        )
    )

    assert result.service_status["status"] == "ok"
    assert calls
    assert any(call["args"][-1] == "ollama-bridge" for call in calls)
    for call in calls:
        args = call["args"]
        environ = call["env"]
        assert isinstance(args, list)
        assert isinstance(environ, dict)
        assert environ["AWF_DATABASE_URL"] == fallback_database_url
        assert str(missing_env) not in args


@pytest.mark.unit
def test_bootstrap_passes_compose_env_file_when_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_checkout_root: Path,
) -> None:
    compose_env_file = tmp_path / "docker" / "compose" / ".env"
    compose_env_file.parent.mkdir(parents=True)
    compose_env_file.write_text("AWF_API_TOKEN=persisted-token\n", encoding="utf-8")
    monkeypatch.setattr(
        bootstrap,
        "LOCAL_SERVICE_COMPOSE_ENV_FILE",
        compose_env_file,
        raising=False,
    )
    calls: list[list[str]] = []

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
    assert "--env-file" not in calls[0]
    source_root = source_checkout_root
    for call in calls[1:]:
        assert call[:6] == [
            "docker",
            "compose",
            "--env-file",
            str(compose_env_file),
            "-f",
            str(source_root / "docker/compose/local-service.yml"),
        ]


@pytest.mark.unit
def test_bootstrap_forwards_compose_context_to_status_collector(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_checkout_root: Path,
) -> None:
    compose_env_file = source_checkout_root / "docker" / "compose" / ".env"
    compose_env_file.write_text("AWF_API_TOKEN=persisted-token\n", encoding="utf-8")
    monkeypatch.setattr(
        bootstrap,
        "LOCAL_SERVICE_COMPOSE_ENV_FILE",
        Path("docker/compose/.env"),
        raising=False,
    )
    service_env = {"AWF_API_TOKEN": "runtime-token"}
    captured: dict[str, object] = {}

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    async def _collect(
        settings: ServiceSettings,
        *,
        strict_providers: Iterable[str] | None = None,
        provider_environ: Mapping[str, str] | None = None,
        environ: Mapping[str, str] | None = None,
        compose_file: Path | None = None,
        compose_env_file: Path | None = None,
    ) -> dict[str, object]:
        _ = strict_providers
        captured["provider_environ"] = dict(provider_environ or {})
        captured["environ"] = dict(environ or {})
        captured["compose_file"] = compose_file
        captured["compose_env_file"] = compose_env_file
        return {"service": settings.service_name, "status": "ok", "checks": {}}

    result = asyncio.run(
        run_service_bootstrap(
            _settings(tmp_path),
            options=ServiceBootstrapOptions(
                timeout_seconds=1,
                poll_interval_seconds=0.1,
                skip_agent_runtime_build=True,
            ),
            run_subprocess=_run,
            status_collector=_collect,
            sleep=_no_sleep,
            monotonic=lambda: 0.0,
            service_environ=service_env,
        )
    )

    assert result.service_status["status"] == "ok"
    assert captured == {
        "provider_environ": service_env,
        "environ": service_env,
        "compose_file": source_checkout_root / "docker/compose/local-service.yml",
        "compose_env_file": compose_env_file,
    }


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
        str(source_root / "docker/compose/local-service.yml"),
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
