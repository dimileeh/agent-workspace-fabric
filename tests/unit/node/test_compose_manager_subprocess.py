"""Compose up/down unit tests with a mocked subprocess.

Covers the code paths that the integration test exercises (up/down,
down-noop, error propagation) but without a running Docker daemon, so the
behaviour is verified quickly on every ``pytest`` run rather than only when
a docker socket is present.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from awf.node.compose_manager import (
    COMPOSE_CAPTURE_TIMEOUT_BUFFER_SECONDS,
    DEFAULT_SERVICE_STARTUP_LOG_TAIL_LINES,
    SERVICE_STARTUP_DIAGNOSTICS_SCHEMA,
    ComposeManager,
    ComposeOperationError,
    WorkspaceComposeSpec,
)

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


@pytest.fixture
def manager(tmp_path: Path) -> ComposeManager:
    return ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)


def _spec(tmp_path: Path) -> WorkspaceComposeSpec:
    return WorkspaceComposeSpec(
        workspace_id="ws_unit_mock",
        worktree_host_path=tmp_path / "worktree",
        postgres_password="pw",
    )


def _mock_proc(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> AsyncMock:
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate.return_value = (stdout, stderr)
    return proc


class TestUp:
    @pytest.mark.unit
    async def test_up_invokes_docker_compose_up_d_wait(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        spec = _spec(tmp_path)
        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            return_value=_mock_proc(),
        ) as mock_exec:
            await manager.up(spec, wait=True)

        assert mock_exec.call_count == 1
        cmd = mock_exec.call_args[0]
        assert cmd[:2] == ("docker", "compose")
        env = mock_exec.call_args.kwargs["env"]
        assert env["COMPOSE_PROJECT_NAME"] == spec.project_name()
        assert Path(env["COMPOSE_FILE"]).name == "compose.yml"
        assert "up" in cmd and "-d" in cmd and "--wait" in cmd
        assert "--remove-orphans" in cmd
        assert "--wait-timeout" in cmd and "300" in cmd

    @pytest.mark.unit
    async def test_up_budgets_build_separately_from_compose_timeout(
        self,
        manager: ComposeManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        spec = WorkspaceComposeSpec(
            workspace_id="ws_unit_mock",
            worktree_host_path=tmp_path / "worktree",
            postgres_password="pw",
            compose_up_timeout_seconds=900,
        )
        wait_for_timeouts: list[float] = []

        async def _wait_for(awaitable, timeout: float):  # type: ignore[no-untyped-def]
            wait_for_timeouts.append(timeout)
            return await awaitable

        monkeypatch.setattr("awf.node.compose_manager.asyncio.wait_for", _wait_for)
        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            return_value=_mock_proc(),
        ) as mock_exec:
            await manager.up(spec, wait=True)

        cmd = mock_exec.call_args[0]
        wait_timeout_index = cmd.index("--wait-timeout")
        assert cmd[wait_timeout_index + 1] == "900"
        assert wait_for_timeouts == [900.0 + 900.0 + COMPOSE_CAPTURE_TIMEOUT_BUFFER_SECONDS]

    @pytest.mark.unit
    async def test_up_without_wait_omits_wait_flag(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        spec = _spec(tmp_path)
        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            return_value=_mock_proc(),
        ) as mock_exec:
            await manager.up(spec, wait=False)

        cmd = mock_exec.call_args[0]
        assert "--wait" not in cmd

    @pytest.mark.unit
    async def test_up_raises_structured_error_on_nonzero_exit(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        spec = _spec(tmp_path)
        with (
            patch(
                "awf.node.compose_manager.asyncio.create_subprocess_exec",
                return_value=_mock_proc(returncode=1, stderr=b"daemon unreachable"),
            ),
            pytest.raises(ComposeOperationError) as exc,
        ):
            await manager.up(spec)

        assert exc.value.operation == "up"
        assert exc.value.returncode == 1
        assert "daemon unreachable" in exc.value.stderr
        assert exc.value.reason_code == "DOCKER_UNAVAILABLE"

    @pytest.mark.unit
    async def test_up_retries_once_when_docker_compose_dispatch_drops_compose(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        spec = _spec(tmp_path)
        first = _mock_proc(
            returncode=125,
            stderr=(
                b"unknown shorthand flag: 'd' in -d\n\nUsage:  docker [OPTIONS] COMMAND [ARG...]\n"
            ),
        )
        second = _mock_proc(returncode=0)
        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            side_effect=[first, second],
        ) as mock_exec:
            await manager.up(spec, wait=True)

        assert mock_exec.call_count == 2
        assert mock_exec.call_args_list[0].args[:2] == ("docker", "compose")
        assert mock_exec.call_args_list[1].args[:2] == ("docker", "compose")

    @pytest.mark.unit
    async def test_up_renders_compose_file_before_running(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        spec = _spec(tmp_path)
        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            return_value=_mock_proc(),
        ):
            paths = await manager.up(spec)

        assert paths.compose_file.exists()
        # Sanity check: the path we'd inspect from another call matches.
        assert paths.compose_file.name == "compose.yml"


class TestEnsureProjectUp:
    @pytest.mark.unit
    async def test_uses_persisted_project_and_file_without_rendering(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        compose_file = tmp_path / "persisted-compose.yml"
        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            return_value=_mock_proc(),
        ) as mock_exec:
            await manager.ensure_project_up(
                project_name="awf_persisted_ws",
                compose_file=compose_file,
                workspace_id="ws_persisted",
                wait=True,
            )

        cmd = mock_exec.call_args[0]
        assert cmd == (
            "docker",
            "compose",
            "up",
            "-d",
            "--remove-orphans",
            "--wait",
            "--wait-timeout",
            "300",
        )
        env = mock_exec.call_args.kwargs["env"]
        assert env["COMPOSE_PROJECT_NAME"] == "awf_persisted_ws"
        assert env["COMPOSE_FILE"] == str(compose_file)
        assert not (tmp_path / "work" / "compose" / "ws_persisted").exists()

    @pytest.mark.unit
    async def test_ensure_project_up_budgets_build_separately_from_compose_timeout(
        self,
        manager: ComposeManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        compose_file = tmp_path / "persisted-compose.yml"
        wait_for_timeouts: list[float] = []

        async def _wait_for(awaitable, timeout: float):  # type: ignore[no-untyped-def]
            wait_for_timeouts.append(timeout)
            return await awaitable

        monkeypatch.setattr("awf.node.compose_manager.asyncio.wait_for", _wait_for)
        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            return_value=_mock_proc(),
        ) as mock_exec:
            await manager.ensure_project_up(
                project_name="awf_persisted_ws",
                compose_file=compose_file,
                workspace_id="ws_persisted",
                wait=True,
                compose_up_timeout_seconds=900,
            )

        cmd = mock_exec.call_args[0]
        wait_timeout_index = cmd.index("--wait-timeout")
        assert cmd[wait_timeout_index + 1] == "900"
        assert wait_for_timeouts == [900.0 + 900.0 + COMPOSE_CAPTURE_TIMEOUT_BUFFER_SECONDS]


class TestDown:
    @pytest.mark.unit
    async def test_down_is_noop_when_project_never_rendered(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        spec = _spec(tmp_path)
        with patch("awf.node.compose_manager.asyncio.create_subprocess_exec") as mock_exec:
            await manager.down(spec)

        mock_exec.assert_not_called()

    @pytest.mark.unit
    async def test_down_invokes_docker_compose_down_v(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        spec = _spec(tmp_path)
        # Materialize the compose file so down() thinks there's something to tear down.
        manager.render(spec)

        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            return_value=_mock_proc(),
        ) as mock_exec:
            await manager.down(spec, remove_volumes=True)

        cmd = mock_exec.call_args[0]
        assert "down" in cmd and "-v" in cmd
        assert "--remove-orphans" in cmd

    @pytest.mark.unit
    async def test_down_project_uses_supplied_compose_file_path(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        compose_file = tmp_path / "custom-compose.yml"
        compose_file.write_text("services: {}\n", encoding="utf-8")

        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            return_value=_mock_proc(),
        ) as mock_exec:
            await manager.down_project(
                project_name="awf_ws_custom",
                compose_file=compose_file,
                workspace_id="ws_custom",
                remove_volumes=True,
            )

        cmd = mock_exec.call_args[0]
        env = mock_exec.call_args.kwargs["env"]
        assert env["COMPOSE_PROJECT_NAME"] == "awf_ws_custom"
        assert env["COMPOSE_FILE"] == str(compose_file)
        assert "down" in cmd and "-v" in cmd
        assert "--remove-orphans" in cmd

    @pytest.mark.unit
    async def test_down_without_volumes_omits_v(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        spec = _spec(tmp_path)
        manager.render(spec)
        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            return_value=_mock_proc(),
        ) as mock_exec:
            await manager.down(spec, remove_volumes=False)

        cmd = mock_exec.call_args[0]
        assert "down" in cmd and "-v" not in cmd

    @pytest.mark.unit
    async def test_down_raises_on_failure(self, manager: ComposeManager, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        manager.render(spec)
        with (
            patch(
                "awf.node.compose_manager.asyncio.create_subprocess_exec",
                return_value=_mock_proc(returncode=17, stderr=b"network in use"),
            ),
            pytest.raises(ComposeOperationError) as exc,
        ):
            await manager.down(spec)

        assert exc.value.operation == "down"
        assert exc.value.returncode == 17

    @pytest.mark.unit
    async def test_remove_project_by_label_skips_empty_resource_sets(
        self, manager: ComposeManager
    ) -> None:
        with (
            patch.object(manager, "_docker_resource_ids", side_effect=[[], [], []]) as resource_ids,
            patch.object(manager, "_docker") as docker,
        ):
            await manager.remove_project_by_label(
                project_name="awf_ws_empty",
                workspace_id="ws_empty",
                remove_volumes=True,
            )

        assert resource_ids.call_count == 3
        docker.assert_not_called()

    @pytest.mark.unit
    async def test_remove_project_by_label_can_preserve_volumes(
        self, manager: ComposeManager
    ) -> None:
        with (
            patch.object(
                manager, "_docker_resource_ids", side_effect=[["container"], ["net"]]
            ) as resource_ids,
            patch.object(manager, "_docker") as docker,
        ):
            await manager.remove_project_by_label(
                project_name="awf_ws_preserve",
                workspace_id="ws_preserve",
                remove_volumes=False,
            )

        assert resource_ids.call_count == 2
        docker.assert_any_await(["rm", "-f", "container"], operation="rm")
        docker.assert_any_await(["network", "rm", "net"], operation="network rm")


# ── Companion startup diagnostics (issue #299) ───────────────────────────────

# Known secret bodies planted into companion logs / healthcheck output. The
# headline regression for #299 is that NONE of these raw bodies may appear in
# the persisted, redacted diagnostics payload.
_SECRET_URL_PW = "S3CRETPW"
_SECRET_ASSIGNMENT = "topsecretvalue"
_SECRET_BEARER = "bearersecretbody123456"
_SECRET_PROVIDER_TOKEN = "ghp_exampletokenABCDEF0123456789"

_BACKEND_LOGS = (
    "backend boot sequence start\n"
    f"AWF_API_TOKEN={_SECRET_ASSIGNMENT}\n"
    f"Authorization: Bearer {_SECRET_BEARER}\n"
    f"using provider token {_SECRET_PROVIDER_TOKEN}\n"
    f"connecting to postgres://appuser:{_SECRET_URL_PW}@db/awf\n"
)


def _inspect_entry(
    *,
    container_id: str | None,
    service: str | None,
    status: str = "running",
    exit_code: int = 0,
    health_status: str | None = None,
    health_log: list[dict[str, Any]] | None = None,
    healthcheck_test: list[str] | None = None,
) -> dict[str, Any]:
    config: dict[str, Any] = {}
    if service is not None:
        config["Labels"] = {"com.docker.compose.service": service}
    if healthcheck_test is not None:
        config["Healthcheck"] = {"Test": healthcheck_test}
    state: dict[str, Any] = {"Status": status, "ExitCode": exit_code}
    if health_status is not None:
        state["Health"] = {"Status": health_status, "Log": health_log or []}
    entry: dict[str, Any] = {"Config": config, "State": state}
    if container_id is not None:
        entry["Id"] = container_id
    return entry


def _docker_dispatch(
    *,
    ps_ids: list[str] | None = None,
    ps_returncode: int = 0,
    ps_stderr: bytes = b"",
    inspect_stdout: bytes | None = None,
    inspect_returncode: int = 0,
    inspect_stderr: bytes = b"",
    logs_by_container: dict[str, bytes] | None = None,
) -> Callable[..., AsyncMock]:
    """Build a ``create_subprocess_exec`` side effect dispatching on subcommand."""

    def _fake(*cmd: str, **_kwargs: Any) -> AsyncMock:
        subcommand = cmd[1]
        if subcommand == "ps":
            stdout = "\n".join(ps_ids or []).encode() if ps_ids else b""
            return _mock_proc(returncode=ps_returncode, stdout=stdout, stderr=ps_stderr)
        if subcommand == "inspect":
            return _mock_proc(
                returncode=inspect_returncode,
                stdout=inspect_stdout or b"",
                stderr=inspect_stderr,
            )
        if subcommand == "logs":
            container_id = cmd[-1]
            body = (logs_by_container or {}).get(container_id)
            if body is None:
                return _mock_proc(returncode=1, stderr=b"Error: No such container")
            # docker logs combines stderr into stdout (combine_stderr=True), so
            # the captured process yields ``None`` for the stderr stream.
            return _mock_proc(returncode=0, stdout=body, stderr=None)
        raise AssertionError(f"unexpected docker subcommand: {subcommand}")

    return _fake


class TestCaptureCompanionDiagnostics:
    @pytest.mark.unit
    async def test_happy_path_captures_unhealthy_only_and_excludes_healthy(
        self, manager: ComposeManager
    ) -> None:
        inspect = json.dumps(
            [
                _inspect_entry(
                    container_id="c_postgres",
                    service="postgres",
                    health_status="healthy",
                    healthcheck_test=["CMD-SHELL", "pg_isready -U awf"],
                ),
                _inspect_entry(
                    container_id="c_backend",
                    service="backend",
                    health_status="unhealthy",
                    health_log=[{"ExitCode": 1, "Output": "probe failed: connection refused"}],
                    healthcheck_test=["CMD-SHELL", "curl -f http://localhost:8000/health"],
                ),
            ]
        ).encode()
        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            side_effect=_docker_dispatch(
                ps_ids=["c_postgres", "c_backend"],
                inspect_stdout=inspect,
                logs_by_container={"c_backend": _BACKEND_LOGS.encode()},
            ),
        ):
            payload = await manager.capture_companion_diagnostics(
                project_name="awf_ws_diag",
                workspace_id="ws_diag",
                tail_lines=50,
            )

        assert payload["schema"] == SERVICE_STARTUP_DIAGNOSTICS_SCHEMA
        assert payload["compose_project"] == "awf_ws_diag"
        assert payload["compose_file"].endswith("ws_diag/compose.yml")
        assert payload["tail_lines"] == 50
        assert payload["containers_inspected"] == 2
        # Healthy postgres excluded; unhealthy backend captured.
        assert set(payload["companion_health"]) == {"backend"}
        assert payload["companion_health"]["backend"]["health_status"] == "unhealthy"
        assert payload["companion_health"]["backend"]["health_log"][0]["ExitCode"] == 1
        assert payload["healthcheck_test"]["backend"] == [
            "CMD-SHELL",
            "curl -f http://localhost:8000/health",
        ]
        assert "backend" in payload["companion_logs"]
        assert isinstance(payload["companion_logs"]["backend"], list)
        assert "postgres" not in payload["companion_logs"]

    @pytest.mark.unit
    async def test_redacts_planted_secrets_in_logs_and_health_output(
        self, manager: ComposeManager
    ) -> None:
        inspect = json.dumps(
            [
                _inspect_entry(
                    container_id="c_backend",
                    service="backend",
                    health_status="unhealthy",
                    health_log=[
                        {
                            "ExitCode": 1,
                            "Output": f"db url postgres://appuser:{_SECRET_URL_PW}@db/awf",
                        }
                    ],
                    healthcheck_test=["CMD-SHELL", "check"],
                ),
            ]
        ).encode()
        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            side_effect=_docker_dispatch(
                ps_ids=["c_backend"],
                inspect_stdout=inspect,
                logs_by_container={"c_backend": _BACKEND_LOGS.encode()},
            ),
        ):
            payload = await manager.capture_companion_diagnostics(
                project_name="awf_ws_secret",
                workspace_id="ws_secret",
            )

        blob = json.dumps(payload)
        for raw_secret in (
            _SECRET_URL_PW,
            _SECRET_ASSIGNMENT,
            _SECRET_BEARER,
            _SECRET_PROVIDER_TOKEN,
        ):
            assert raw_secret not in blob, f"raw secret leaked: {raw_secret}"
        assert "[redacted]" in blob

    @pytest.mark.unit
    async def test_default_tail_lines_used_when_unspecified(self, manager: ComposeManager) -> None:
        inspect = json.dumps(
            [
                _inspect_entry(
                    container_id="c_backend",
                    service="backend",
                    health_status="unhealthy",
                    healthcheck_test=["CMD", "true"],
                ),
            ]
        ).encode()
        seen_logs_cmds: list[tuple[str, ...]] = []

        def _spy(*cmd: str, **kwargs: Any) -> AsyncMock:
            if cmd[1] == "logs":
                seen_logs_cmds.append(cmd)
            return _docker_dispatch(
                ps_ids=["c_backend"],
                inspect_stdout=inspect,
                logs_by_container={"c_backend": b"line\n"},
            )(*cmd, **kwargs)

        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            side_effect=_spy,
        ):
            payload = await manager.capture_companion_diagnostics(
                project_name="awf_ws_default",
                workspace_id="ws_default",
            )

        assert payload["tail_lines"] == DEFAULT_SERVICE_STARTUP_LOG_TAIL_LINES
        assert seen_logs_cmds
        assert str(DEFAULT_SERVICE_STARTUP_LOG_TAIL_LINES) in seen_logs_cmds[0]

    @pytest.mark.unit
    async def test_exited_container_without_healthcheck_keyed_by_id_fallback(
        self, manager: ComposeManager
    ) -> None:
        inspect = json.dumps(
            [
                _inspect_entry(
                    container_id="c_orphan",
                    service=None,  # no compose-service label → fall back to id
                    status="exited",
                    exit_code=2,
                ),
            ]
        ).encode()
        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            side_effect=_docker_dispatch(
                ps_ids=["c_orphan"],
                inspect_stdout=inspect,
                logs_by_container={"c_orphan": b"crashed early\n"},
            ),
        ):
            payload = await manager.capture_companion_diagnostics(
                project_name="awf_ws_exit",
                workspace_id="ws_exit",
            )

        assert "c_orphan" in payload["companion_health"]
        assert payload["companion_health"]["c_orphan"]["exit_code"] == 2
        assert payload["companion_health"]["c_orphan"]["health_status"] is None
        # No healthcheck declared → no test array recorded for this service.
        assert "healthcheck_test" not in payload or "c_orphan" not in payload["healthcheck_test"]
        assert payload["companion_logs"]["c_orphan"] == ["crashed early"]

    @pytest.mark.unit
    async def test_running_sidecar_with_none_health_status_excluded(
        self, manager: ComposeManager
    ) -> None:
        # Docker/Podman report the literal ``"none"`` (``types.NoHealthcheck``)
        # for containers without a healthcheck. A running sidecar carrying that
        # status must be treated like an absent ``Health`` block — i.e. excluded
        # — so it is not dragged into diagnostics just because a different
        # companion failed startup.
        inspect = json.dumps(
            [
                _inspect_entry(
                    container_id="c_agent",
                    service="agent",
                    status="running",
                    health_status="none",
                ),
                _inspect_entry(
                    container_id="c_backend",
                    service="backend",
                    health_status="unhealthy",
                    health_log=[{"ExitCode": 1, "Output": "probe failed"}],
                    healthcheck_test=["CMD-SHELL", "check"],
                ),
            ]
        ).encode()
        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            side_effect=_docker_dispatch(
                ps_ids=["c_agent", "c_backend"],
                inspect_stdout=inspect,
                logs_by_container={"c_backend": _BACKEND_LOGS.encode()},
            ),
        ):
            payload = await manager.capture_companion_diagnostics(
                project_name="awf_ws_none",
                workspace_id="ws_none",
            )

        # Only the genuinely unhealthy backend is captured; the running agent
        # with ``health_status == "none"`` is excluded.
        assert set(payload["companion_health"]) == {"backend"}
        assert "agent" not in payload.get("companion_logs", {})

    @pytest.mark.unit
    async def test_unhealthy_container_without_id_skips_logs(self, manager: ComposeManager) -> None:
        inspect = json.dumps(
            [
                _inspect_entry(
                    container_id=None,
                    service="ghost",
                    health_status="unhealthy",
                ),
            ]
        ).encode()
        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            side_effect=_docker_dispatch(
                ps_ids=["whatever"],
                inspect_stdout=inspect,
                logs_by_container={},
            ),
        ):
            payload = await manager.capture_companion_diagnostics(
                project_name="awf_ws_noid",
                workspace_id="ws_noid",
            )

        assert "ghost" in payload["companion_health"]
        assert "companion_logs" not in payload or "ghost" not in payload["companion_logs"]

    @pytest.mark.unit
    async def test_per_container_logs_error_records_marker_and_continues(
        self, manager: ComposeManager
    ) -> None:
        inspect = json.dumps(
            [
                _inspect_entry(
                    container_id="c_gone",
                    service="backend",
                    health_status="unhealthy",
                    healthcheck_test=["CMD", "x"],
                ),
            ]
        ).encode()
        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            side_effect=_docker_dispatch(
                ps_ids=["c_gone"],
                inspect_stdout=inspect,
                logs_by_container={},  # logs call fails → container gone race
            ),
        ):
            payload = await manager.capture_companion_diagnostics(
                project_name="awf_ws_gone",
                workspace_id="ws_gone",
            )

        marker = payload["companion_logs_capture_error"]
        assert isinstance(marker, dict)
        assert isinstance(marker["backend"], str)
        # Health + healthcheck still captured despite the logs failure.
        assert "backend" in payload["companion_health"]
        assert payload["healthcheck_test"]["backend"] == ["CMD", "x"]

    @pytest.mark.unit
    async def test_logs_daemon_failure_classified_docker_unavailable_with_combined_stderr(
        self, manager: ComposeManager
    ) -> None:
        """Daemon failures on the ``docker logs`` path must read ``DOCKER_UNAVAILABLE``.

        ``capture_companion_diagnostics`` runs ``docker logs`` with
        ``combine_stderr=True``, which folds the child's stderr into stdout and makes
        ``communicate()`` yield ``None`` for the stderr stream. A docker-daemon error
        therefore surfaces in *stdout*, so daemon detection must inspect stdout in that
        mode — otherwise the always-empty stderr makes it misclassify the marker as
        ``COMPOSE_COMMAND_FAILED``.
        """
        inspect = json.dumps(
            [
                _inspect_entry(
                    container_id="c_backend",
                    service="backend",
                    health_status="unhealthy",
                    healthcheck_test=["CMD", "x"],
                ),
            ]
        ).encode()
        daemon_error = (
            b"Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
            b"Is the docker daemon running?"
        )

        def _fake(*cmd: str, **_kwargs: Any) -> AsyncMock:
            subcommand = cmd[1]
            if subcommand == "ps":
                return _mock_proc(returncode=0, stdout=b"c_backend")
            if subcommand == "inspect":
                return _mock_proc(returncode=0, stdout=inspect)
            if subcommand == "logs":
                # combine_stderr=True folds stderr into stdout, so the daemon error
                # lands in stdout and communicate() returns None for the stderr stream.
                return _mock_proc(returncode=1, stdout=daemon_error, stderr=None)
            raise AssertionError(f"unexpected docker subcommand: {subcommand}")

        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            side_effect=_fake,
        ):
            payload = await manager.capture_companion_diagnostics(
                project_name="awf_ws_daemon",
                workspace_id="ws_daemon",
            )

        marker = payload["companion_logs_capture_error"]["backend"]
        assert marker.startswith("DOCKER_UNAVAILABLE"), marker

    @pytest.mark.unit
    async def test_capture_error_marker_is_always_a_dict_keyed_by_scope(
        self, manager: ComposeManager
    ) -> None:
        """``companion_logs_capture_error`` is a uniform ``dict[str, str]``.

        Regression for the inconsistent-shape review: top-level failures
        (ps/inspect/JSON) are keyed under ``_top_level`` and per-container logs
        failures under the compose service name, so consumers never have to
        special-case a bare ``str`` vs a per-service ``dict``.
        """
        inspect = json.dumps(
            [
                _inspect_entry(
                    container_id="c_gone",
                    service="backend",
                    health_status="unhealthy",
                ),
            ]
        ).encode()
        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            side_effect=_docker_dispatch(
                ps_ids=["c_gone"],
                inspect_stdout=inspect,
                logs_by_container={},  # per-container logs failure → service-keyed marker
            ),
        ):
            per_container = await manager.capture_companion_diagnostics(
                project_name="awf_ws_shape_pc",
                workspace_id="ws_shape_pc",
            )
        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            side_effect=_docker_dispatch(ps_returncode=1, ps_stderr=b"boom"),
        ):
            top_level = await manager.capture_companion_diagnostics(
                project_name="awf_ws_shape_tl",
                workspace_id="ws_shape_tl",
            )

        for marker in (
            per_container["companion_logs_capture_error"],
            top_level["companion_logs_capture_error"],
        ):
            assert isinstance(marker, dict)
            assert all(isinstance(k, str) and isinstance(v, str) for k, v in marker.items())
        assert set(per_container["companion_logs_capture_error"]) == {"backend"}
        assert set(top_level["companion_logs_capture_error"]) == {"_top_level"}

    @pytest.mark.unit
    async def test_unidentifiable_containers_get_distinct_fallback_keys(
        self, manager: ComposeManager
    ) -> None:
        """Containers lacking both a compose service label and an ``Id`` must not
        collide on a single ``<unknown>`` key — each unhealthy one keeps its own
        diagnostics entry via an index-disambiguated fallback key.
        """
        inspect = json.dumps(
            [
                _inspect_entry(container_id=None, service=None, status="exited", exit_code=1),
                _inspect_entry(container_id=None, service=None, status="exited", exit_code=2),
            ]
        ).encode()
        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            side_effect=_docker_dispatch(
                ps_ids=["a", "b"],
                inspect_stdout=inspect,
                logs_by_container={},
            ),
        ):
            payload = await manager.capture_companion_diagnostics(
                project_name="awf_ws_noident",
                workspace_id="ws_noident",
            )

        # Both unhealthy containers survive under distinct keys, not collapsed.
        assert len(payload["companion_health"]) == 2
        assert set(payload["companion_health"]) == {"<unknown-0>", "<unknown-1>"}

    @pytest.mark.unit
    async def test_top_level_ps_error_returns_partial_payload(
        self, manager: ComposeManager
    ) -> None:
        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            side_effect=_docker_dispatch(ps_returncode=1, ps_stderr=b"docker ps boom"),
        ):
            payload = await manager.capture_companion_diagnostics(
                project_name="awf_ws_psfail",
                workspace_id="ws_psfail",
            )

        assert isinstance(payload, dict)
        marker = payload["companion_logs_capture_error"]
        assert isinstance(marker, dict)
        assert marker["_top_level"].startswith("ps:")
        assert "docker ps boom" in marker["_top_level"]
        assert "companion_health" not in payload

    @pytest.mark.unit
    async def test_inspect_error_returns_partial_payload(self, manager: ComposeManager) -> None:
        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            side_effect=_docker_dispatch(
                ps_ids=["c_backend"],
                inspect_returncode=1,
                inspect_stderr=b"docker inspect boom",
            ),
        ):
            payload = await manager.capture_companion_diagnostics(
                project_name="awf_ws_inspectfail",
                workspace_id="ws_inspectfail",
            )

        marker = payload["companion_logs_capture_error"]
        assert isinstance(marker, dict)
        assert marker["_top_level"].startswith("inspect:")
        assert "companion_health" not in payload

    @pytest.mark.unit
    async def test_unparseable_inspect_json_returns_partial_payload(
        self, manager: ComposeManager
    ) -> None:
        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            side_effect=_docker_dispatch(
                ps_ids=["c_backend"],
                inspect_stdout=b"not json at all",
            ),
        ):
            payload = await manager.capture_companion_diagnostics(
                project_name="awf_ws_badjson",
                workspace_id="ws_badjson",
            )

        marker = payload["companion_logs_capture_error"]
        assert isinstance(marker, dict)
        assert marker["_top_level"].startswith("inspect: unparseable JSON")
        assert "companion_health" not in payload

    @pytest.mark.unit
    async def test_non_list_inspect_json_yields_no_companions(
        self, manager: ComposeManager
    ) -> None:
        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            side_effect=_docker_dispatch(
                ps_ids=["c_backend"],
                inspect_stdout=b"{}",
            ),
        ):
            payload = await manager.capture_companion_diagnostics(
                project_name="awf_ws_dict",
                workspace_id="ws_dict",
            )

        assert payload["containers_inspected"] == 0
        assert "companion_health" not in payload

    @pytest.mark.unit
    async def test_malformed_inspect_entries_are_skipped_defensively(
        self, manager: ComposeManager
    ) -> None:
        inspect = json.dumps(
            [
                "not-a-container-mapping",  # non-Mapping list element
                {"Id": "c_badstate", "State": "not-a-mapping"},  # State not a Mapping
                {
                    # No Id (logs skipped); malformed health-log entry skipped.
                    "Config": {"Labels": {"com.docker.compose.service": "svc"}},
                    "State": {
                        "Status": "running",
                        "Health": {
                            "Status": "unhealthy",
                            "Log": ["bad-entry", {"ExitCode": 9, "Output": "real"}],
                        },
                    },
                },
            ]
        ).encode()
        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            side_effect=_docker_dispatch(
                ps_ids=["a", "b", "c"],
                inspect_stdout=inspect,
                logs_by_container={},
            ),
        ):
            payload = await manager.capture_companion_diagnostics(
                project_name="awf_ws_malformed",
                workspace_id="ws_malformed",
            )

        assert payload["containers_inspected"] == 3
        # Only the well-formed unhealthy service is captured.
        assert set(payload["companion_health"]) == {"svc"}
        assert payload["companion_health"]["svc"]["health_log"] == [
            {"ExitCode": 9, "Output": "real"}
        ]
        assert "companion_logs" not in payload

    @pytest.mark.unit
    async def test_no_containers_skips_inspect_and_logs(self, manager: ComposeManager) -> None:
        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            side_effect=_docker_dispatch(ps_ids=[]),
        ) as mock_exec:
            payload = await manager.capture_companion_diagnostics(
                project_name="awf_ws_empty",
                workspace_id="ws_empty",
            )

        assert payload["containers_inspected"] == 0
        assert "companion_health" not in payload
        # Only the ``docker ps`` call ran; no inspect/logs.
        assert mock_exec.call_count == 1

    @pytest.mark.unit
    async def test_capture_never_raises_on_docker_unavailable(
        self, manager: ComposeManager
    ) -> None:
        with patch(
            "awf.node.compose_manager.asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("docker not installed"),
        ):
            payload = await manager.capture_companion_diagnostics(
                project_name="awf_ws_nodocker",
                workspace_id="ws_nodocker",
            )

        marker = payload["companion_logs_capture_error"]
        assert isinstance(marker, dict)
        assert marker["_top_level"].startswith("ps:")
