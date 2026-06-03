"""ComposeManager companion-image and docker-capture command unit tests.

These tests were split out of ``test_compose_manager.py`` to keep each module
under the first-party file line limit. They cover the Docker subprocess command
helpers (``_docker_capture``) and the companion image build/inspect commands.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from awf.node import compose_manager as compose_module
from awf.node.compose_manager import (
    ComposeManager,
    ComposeOperationError,
)

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


@pytest.fixture
def manager(tmp_path: Path) -> ComposeManager:
    """Provide a compose manager rooted in the test temp directory."""
    return ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)


class _FakeProcess:
    def __init__(
        self,
        *,
        returncode: int,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


class _HangingProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.kill_called = False

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.Event().wait()
        return b"", b""

    def kill(self) -> None:
        self.kill_called = True
        self.returncode = -9

    async def wait(self) -> int | None:
        return self.returncode


class TestDockerCapture:
    """Tests for the ComposeManager low-level docker capture helper."""

    @pytest.mark.unit
    async def test_docker_capture_returns_stdout_on_success(
        self,
        manager: ComposeManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Docker capture returns stdout for successful commands."""
        calls: list[tuple[object, ...]] = []

        async def _spawn(*args: object, **_kwargs: object) -> _FakeProcess:
            calls.append(args)
            return _FakeProcess(returncode=0, stdout=b"container-a\ncontainer-b\n")

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        stdout = await manager._docker_capture(["ps", "-aq"], operation="ps")  # noqa: SLF001

        assert stdout == "container-a\ncontainer-b\n"
        assert calls == [("docker", "ps", "-aq")]

    @pytest.mark.unit
    async def test_docker_capture_classifies_daemon_connectivity_errors(
        self,
        manager: ComposeManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Docker capture maps daemon connectivity failures to docker unavailable."""

        async def _spawn(*_args: object, **_kwargs: object) -> _FakeProcess:
            return _FakeProcess(
                returncode=1,
                stderr=b"error during connect: docker endpoint unavailable",
            )

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        with pytest.raises(ComposeOperationError) as exc:
            await manager._docker_capture(["network", "ls"], operation="network ls")  # noqa: SLF001

        assert exc.value.returncode == 1
        assert exc.value.reason_code == "DOCKER_UNAVAILABLE"
        assert "docker endpoint unavailable" in exc.value.stderr

    @pytest.mark.unit
    async def test_docker_capture_classifies_command_failures(
        self,
        manager: ComposeManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Docker capture preserves ordinary command failures."""

        async def _spawn(*_args: object, **_kwargs: object) -> _FakeProcess:
            return _FakeProcess(returncode=2, stdout=b"usage\n", stderr=b"bad flag\n")

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        with pytest.raises(ComposeOperationError) as exc:
            await manager._docker_capture(["volume", "rm"], operation="volume rm")  # noqa: SLF001

        assert exc.value.returncode == 2
        assert exc.value.reason_code == "COMPOSE_COMMAND_FAILED"
        assert exc.value.stdout == "usage\n"
        assert exc.value.stderr == "bad flag\n"

    @pytest.mark.unit
    async def test_docker_capture_reports_missing_docker_binary(
        self,
        manager: ComposeManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Docker capture classifies a missing docker binary."""

        async def _raise_missing(*_args: object, **_kwargs: object) -> _FakeProcess:
            raise FileNotFoundError("docker")

        monkeypatch.setattr(
            compose_module.asyncio,
            "create_subprocess_exec",
            _raise_missing,
        )

        with pytest.raises(ComposeOperationError) as exc:
            await manager._docker_capture(["ps"], operation="ps")  # noqa: SLF001

        assert exc.value.returncode == 127
        assert exc.value.reason_code == "DOCKER_UNAVAILABLE"

    @pytest.mark.unit
    async def test_docker_capture_translates_non_filenotfound_oserror(
        self,
        manager: ComposeManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Docker capture translates non-FileNotFound OSErrors."""

        # ``PermissionError`` (docker binary present but not executable) is an
        # ``OSError`` subclass that is *not* ``FileNotFoundError``; it must still be
        # translated to a structured ``DOCKER_UNAVAILABLE`` error so best-effort
        # callers like ``capture_companion_diagnostics`` never leak a raw ``OSError``.
        async def _raise_permission(*_args: object, **_kwargs: object) -> _FakeProcess:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(
            compose_module.asyncio,
            "create_subprocess_exec",
            _raise_permission,
        )

        with pytest.raises(ComposeOperationError) as exc:
            await manager._docker_capture(["ps"], operation="ps")  # noqa: SLF001

        assert exc.value.returncode == 127
        assert exc.value.reason_code == "DOCKER_UNAVAILABLE"
        assert "Permission denied" in exc.value.stderr

    @pytest.mark.unit
    async def test_docker_capture_times_out_and_kills_hung_process(
        self,
        manager: ComposeManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Hung docker capture commands are killed and reported as timeouts."""
        process = _HangingProcess()

        async def _spawn(*_args: object, **_kwargs: object) -> _HangingProcess:
            return process

        monkeypatch.setattr(compose_module, "DOCKER_CAPTURE_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        with pytest.raises(ComposeOperationError) as exc:
            await manager._docker_capture(["ps"], operation="ps")  # noqa: SLF001

        assert process.kill_called is True
        assert exc.value.returncode == 124
        assert exc.value.reason_code == "DOCKER_COMMAND_TIMEOUT"
        assert "exceeded" in exc.value.stderr


class TestCompanionImageCommands:
    """Tests for the ComposeManager companion image Docker commands."""

    @pytest.mark.unit
    async def test_companion_image_exists_true_on_zero_exit(
        self, manager: ComposeManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """companion_image_exists returns True on a zero-exit inspect."""
        calls: list[tuple[object, ...]] = []

        async def _spawn(*args: object, **_kwargs: object) -> _FakeProcess:
            """Record the inspect command and simulate a present image."""
            calls.append(args)
            return _FakeProcess(returncode=0, stdout=b"sha256:abc\n")

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        assert await manager.companion_image_exists("awf-companion-backend:abc") is True
        assert calls[0] == ("docker", "image", "inspect", "awf-companion-backend:abc")

    @pytest.mark.unit
    async def test_companion_image_exists_false_when_inspect_fails(
        self, manager: ComposeManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """companion_image_exists returns False when the inspect fails."""

        async def _spawn(*_args: object, **_kwargs: object) -> _FakeProcess:
            return _FakeProcess(returncode=1, stderr=b"No such image")

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        assert await manager.companion_image_exists("missing:tag") is False

    @pytest.mark.unit
    async def test_companion_image_inspect_true_on_zero_exit(
        self, manager: ComposeManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """companion_image_inspect returns True on a zero-exit inspect."""
        calls: list[tuple[object, ...]] = []

        async def _spawn(*args: object, **_kwargs: object) -> _FakeProcess:
            """Record the inspect command and simulate a present image."""
            calls.append(args)
            return _FakeProcess(returncode=0, stdout=b"sha256:abc\n")

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        assert await manager.companion_image_inspect("awf-companion-backend:abc") is True
        assert calls[0] == ("docker", "image", "inspect", "awf-companion-backend:abc")

    @pytest.mark.unit
    async def test_companion_image_inspect_false_for_missing_image(
        self, manager: ComposeManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """companion_image_inspect returns False for confirmed missing images."""

        async def _spawn(*_args: object, **_kwargs: object) -> _FakeProcess:
            """Simulate Docker's missing-image inspect failure."""
            return _FakeProcess(
                returncode=1,
                stderr=b"Error response from daemon: No such image: missing:tag",
            )

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        assert await manager.companion_image_inspect("missing:tag") is False

    @pytest.mark.unit
    async def test_companion_image_inspect_preserves_non_missing_probe_errors(
        self, manager: ComposeManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """companion_image_inspect raises non-missing inspect failures unchanged."""
        probe_error = b"Cannot connect to the Docker daemon"

        async def _spawn(*_args: object, **_kwargs: object) -> _FakeProcess:
            """Simulate an inspect failure caused by Docker unavailability."""
            return _FakeProcess(returncode=1, stderr=probe_error)

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        with pytest.raises(ComposeOperationError) as raised:
            await manager.companion_image_inspect("awf-companion-backend:abc")

        assert raised.value.reason_code == "DOCKER_UNAVAILABLE"
        assert raised.value.stderr == probe_error.decode()

    @pytest.mark.unit
    async def test_companion_image_inspect_preserves_unrelated_not_found_errors(
        self, manager: ComposeManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """companion_image_inspect does not classify unrelated not-found text as missing."""
        probe_error = b"permission denied: user not found"

        async def _spawn(*_args: object, **_kwargs: object) -> _FakeProcess:
            """Simulate an unrelated inspect failure with not-found wording."""
            return _FakeProcess(returncode=1, stderr=probe_error)

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        with pytest.raises(ComposeOperationError) as raised:
            await manager.companion_image_inspect("awf-companion-backend:abc")

        assert raised.value.reason_code == "COMPOSE_COMMAND_FAILED"
        assert raised.value.stderr == probe_error.decode()

    @pytest.mark.unit
    async def test_companion_image_exists_remains_lenient_for_probe_errors(
        self, manager: ComposeManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """companion_image_exists still treats every inspect failure as absent."""

        async def _spawn(*_args: object, **_kwargs: object) -> _FakeProcess:
            """Simulate a probe failure for the lenient existence helper."""
            return _FakeProcess(returncode=1, stderr=b"Cannot connect to the Docker daemon")

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        assert await manager.companion_image_exists("awf-companion-backend:abc") is False

    @pytest.mark.unit
    async def test_build_companion_image_passes_tag_dockerfile_and_labels(
        self, manager: ComposeManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """build_companion_image passes the tag, Dockerfile, and managed labels."""
        calls: list[tuple[object, ...]] = []

        async def _spawn(*args: object, **_kwargs: object) -> _FakeProcess:
            calls.append(args)
            return _FakeProcess(returncode=0)

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        await manager.build_companion_image(
            tag="awf-companion-backend:abc",
            build_context="/host/backend",
            dockerfile="Dockerfile",
            labels={"awf.managed-companion": "true", "awf.companion.name": "backend"},
        )

        assert calls[0] == (
            "docker",
            "build",
            "-t",
            "awf-companion-backend:abc",
            "-f",
            "/host/backend/Dockerfile",
            "--label",
            "awf.managed-companion=true",
            "--label",
            "awf.companion.name=backend",
            "/host/backend",
        )

    @pytest.mark.unit
    async def test_build_companion_image_without_labels_omits_label_flags(
        self, manager: ComposeManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """build_companion_image omits the label flags when no labels are given."""
        calls: list[tuple[object, ...]] = []

        async def _spawn(*args: object, **_kwargs: object) -> _FakeProcess:
            calls.append(args)
            return _FakeProcess(returncode=0)

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        await manager.build_companion_image(
            tag="awf-companion-backend:abc",
            build_context="/host/backend",
            dockerfile="Dockerfile",
        )

        assert calls[0] == (
            "docker",
            "build",
            "-t",
            "awf-companion-backend:abc",
            "-f",
            "/host/backend/Dockerfile",
            "/host/backend",
        )
        assert "--label" not in calls[0]

    @pytest.mark.unit
    async def test_build_companion_image_anchors_dockerfile_to_build_context(
        self, manager: ComposeManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression for PRRT_kwDOSJAM6s6F5073: ``docker build`` resolves a ``-f``
        # path relative to the process working directory (the AWF service cwd),
        # not the build context. A context-relative ``dockerfile`` must be
        # anchored to the absolute build context so the pre-build does not look
        # for the Dockerfile under the service cwd, fail, and fall back to an
        # inline compose build.
        """build_companion_image anchors the Dockerfile path to the build context."""
        calls: list[tuple[object, ...]] = []

        async def _spawn(*args: object, **_kwargs: object) -> _FakeProcess:
            calls.append(args)
            return _FakeProcess(returncode=0)

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        await manager.build_companion_image(
            tag="awf-companion-backend:abc",
            build_context="/host/aira-agent",
            dockerfile="docker/backend.Dockerfile",
        )

        assert calls[0] == (
            "docker",
            "build",
            "-t",
            "awf-companion-backend:abc",
            "-f",
            "/host/aira-agent/docker/backend.Dockerfile",
            "/host/aira-agent",
        )

    @pytest.mark.unit
    async def test_build_companion_image_raises_on_failure(
        self, manager: ComposeManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """build_companion_image raises when the docker build fails."""

        async def _spawn(*_args: object, **_kwargs: object) -> _FakeProcess:
            return _FakeProcess(returncode=1, stderr=b"build failed")

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        with pytest.raises(ComposeOperationError):
            await manager.build_companion_image(
                tag="awf-companion-backend:abc",
                build_context="/host/backend",
                dockerfile="Dockerfile",
            )
