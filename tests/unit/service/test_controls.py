from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from awf.service.controls import WorkspaceStackStopError, stop_project_containers


def _mock_proc(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> AsyncMock:
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate.return_value = (stdout, stderr)
    return proc


@pytest.mark.unit
async def test_stop_project_containers_is_noop_without_project_name() -> None:
    with patch("awf.service.controls.asyncio.create_subprocess_exec") as mock_exec:
        await stop_project_containers(None)

    mock_exec.assert_not_called()


@pytest.mark.unit
async def test_stop_project_containers_returns_when_no_containers_match() -> None:
    with patch(
        "awf.service.controls.asyncio.create_subprocess_exec",
        return_value=_mock_proc(),
    ) as mock_exec:
        await stop_project_containers("awf_ws_empty")

    assert mock_exec.call_count == 1


@pytest.mark.unit
async def test_stop_project_containers_stops_matching_container_ids() -> None:
    with patch(
        "awf.service.controls.asyncio.create_subprocess_exec",
        side_effect=[
            _mock_proc(stdout=b"abc123\n def456 \n"),
            _mock_proc(stdout=b"abc123\ndef456\n"),
        ],
    ) as mock_exec:
        await stop_project_containers("awf_ws_running")

    assert mock_exec.call_count == 2
    assert mock_exec.call_args_list[1].args[:4] == ("docker", "stop", "abc123", "def456")


@pytest.mark.unit
async def test_stop_project_containers_raises_when_ps_fails() -> None:
    with (
        patch(
            "awf.service.controls.asyncio.create_subprocess_exec",
            return_value=_mock_proc(returncode=1, stderr=b"daemon unavailable"),
        ),
        pytest.raises(WorkspaceStackStopError) as exc_info,
    ):
        await stop_project_containers("awf_ws_fail")

    assert exc_info.value.error_code == "STACK_STOP_FAILED"
    assert exc_info.value.operation == "ps"
    assert exc_info.value.returncode == 1
    assert "daemon unavailable" in exc_info.value.message


@pytest.mark.unit
async def test_stop_project_containers_raises_when_stop_fails() -> None:
    with (
        patch(
            "awf.service.controls.asyncio.create_subprocess_exec",
            side_effect=[
                _mock_proc(stdout=b"abc123\n"),
                _mock_proc(returncode=17, stderr=b"permission denied"),
            ],
        ),
        pytest.raises(WorkspaceStackStopError) as exc_info,
    ):
        await stop_project_containers("awf_ws_fail")

    assert exc_info.value.error_code == "STACK_STOP_FAILED"
    assert exc_info.value.operation == "stop"
    assert exc_info.value.returncode == 17
    assert "permission denied" in exc_info.value.message
