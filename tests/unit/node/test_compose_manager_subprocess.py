"""Compose up/down unit tests with a mocked subprocess.

Covers the code paths that the integration test exercises (up/down,
down-noop, error propagation) but without a running Docker daemon, so the
behaviour is verified quickly on every ``pytest`` run rather than only when
a docker socket is present.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from awf.node.compose_manager import (
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
    async def test_up_uses_spec_compose_timeout_for_wait_and_capture(
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
        assert wait_for_timeouts == [960.0]

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
