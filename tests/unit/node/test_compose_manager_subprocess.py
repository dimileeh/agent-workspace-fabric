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
        assert "--project-name" in cmd and spec.project_name() in cmd
        assert "--file" in cmd
        assert "up" in cmd and "-d" in cmd and "--wait" in cmd

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
        assert exc.value.reason_code == "COMPOSE_COMMAND_FAILED"

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
        assert "--project-name" in cmd and "awf_ws_custom" in cmd
        assert "--file" in cmd and str(compose_file) in cmd
        assert "down" in cmd and "-v" in cmd

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
