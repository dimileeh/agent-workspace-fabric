from __future__ import annotations

import importlib.util
import subprocess
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_retry_helper() -> ModuleType:
    helper_path = REPO_ROOT / ".github" / "scripts" / "ci_docker_build_retry.py"
    spec = importlib.util.spec_from_file_location("ci_docker_build_retry", helper_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ci_docker_build_retry = _load_retry_helper()


def test_run_with_retries_preserves_failed_attempt_output_before_success(capsys) -> None:
    command = ("docker", "buildx", "build", "--file", "docker/agent-runtime.Dockerfile", ".")
    results: deque[subprocess.CompletedProcess[str]] = deque(
        [
            subprocess.CompletedProcess(
                args=list(command),
                returncode=1,
                stdout="metadata stdout\n",
                stderr="502 Bad Gateway\n",
            ),
            subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout="build succeeded\n",
                stderr="",
            ),
        ]
    )
    calls: list[tuple[str, ...]] = []
    sleeps: list[float] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(args))
        return results.popleft()

    exit_code = ci_docker_build_retry.run_with_retries(
        command,
        attempts=2,
        delay_seconds=0.25,
        runner=runner,
        sleeper=sleeps.append,
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert calls == [command, command]
    assert sleeps == [0.25]
    assert "metadata stdout\n" in captured.out
    assert "build succeeded\n" in captured.out
    assert "502 Bad Gateway\n" in captured.err
    assert "ci.docker_build attempt 1/2 failed with exit code 1" in captured.err
    assert "ci.docker_build attempt 2/2 succeeded" in captured.err


def test_run_with_retries_returns_final_exit_code_when_exhausted(capsys) -> None:
    command = ("docker", "buildx", "build", "--file", "docker/control-plane.Dockerfile", ".")
    results: deque[subprocess.CompletedProcess[str]] = deque(
        [
            subprocess.CompletedProcess(
                args=list(command),
                returncode=12,
                stdout="first failure stdout\n",
                stderr="first failure stderr\n",
            ),
            subprocess.CompletedProcess(
                args=list(command),
                returncode=42,
                stdout="last failure stdout\n",
                stderr="last failure stderr\n",
            ),
        ]
    )
    sleeps: list[float] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return results.popleft()

    exit_code = ci_docker_build_retry.run_with_retries(
        command,
        attempts=2,
        delay_seconds=0.5,
        runner=runner,
        sleeper=sleeps.append,
    )

    captured = capsys.readouterr()

    assert exit_code == 42
    assert sleeps == [0.5]
    assert "first failure stdout\n" in captured.out
    assert "last failure stdout\n" in captured.out
    assert "first failure stderr\n" in captured.err
    assert "last failure stderr\n" in captured.err
    assert "ci.docker_build attempt 1/2 failed with exit code 12" in captured.err
    assert "ci.docker_build attempt 2/2 failed with exit code 42" in captured.err
    assert "ci.docker_build exhausted 2 attempts; final exit code 42" in captured.err
