"""Local service log helper tests."""

from __future__ import annotations

import subprocess

import pytest

from awf.service.logs import ServiceLogName, ServiceLogsError, run_service_logs


@pytest.mark.unit
def test_service_logs_follow_failure_mentions_terminal_output() -> None:
    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs == {"check": False, "capture_output": False, "text": True}
        return subprocess.CompletedProcess(args, returncode=17, stdout=None, stderr=None)

    with pytest.raises(ServiceLogsError) as exc_info:
        run_service_logs(
            services=[ServiceLogName.api],
            follow=True,
            run_subprocess=_run,
        )

    assert exc_info.value.returncode == 17
    assert exc_info.value.detail == (
        "docker compose logs --follow exited with a non-zero status; "
        "docker output was already written directly to the terminal"
    )
