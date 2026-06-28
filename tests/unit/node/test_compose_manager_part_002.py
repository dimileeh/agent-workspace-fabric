"""ComposeManager unit tests — teardown and compose-command execution.

Docker-daemon-dependent tests live under ``tests/integration/`` and are
skipped when a daemon isn't available. These unit tests verify the teardown
fallback paths and the ``docker compose`` command-execution contract.

Split sibling of ``test_compose_manager.py`` (see
``tests/unit/runtime/test_monitor_completion_gc_part_002.py`` for the
precedent).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import structlog

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


class TestRenderTeardown:
    """Tests for teardown and compose-command execution paths."""

    @pytest.mark.unit
    def test_strict_undefined_catches_missing_vars(self) -> None:
        """Template rendering fails loudly for missing context variables."""
        # Guard: if the template starts referencing a new variable without the
        # WorkspaceComposeSpec supplying it, rendering must fail loudly rather
        # than silently emitting empty YAML values.
        from jinja2 import Environment, StrictUndefined
        from jinja2.exceptions import UndefinedError

        env = Environment(undefined=StrictUndefined, autoescape=False)
        tmpl = env.from_string("name: {{ only_in_template }}")
        with pytest.raises(UndefinedError):
            tmpl.render()

    @pytest.mark.unit
    async def test_down_project_is_noop_when_compose_file_is_missing(
        self,
        tmp_path: Path,
    ) -> None:
        """Downing a missing compose file avoids invoking docker compose."""

        class _RecordingComposeManager(ComposeManager):
            def __init__(self) -> None:
                super().__init__(work_dir=tmp_path / "work", template_path=_TEMPLATE)
                self.calls: list[tuple[str, Path, list[str], str]] = []

            async def _compose(
                self,
                project_name: str,
                compose_file: Path,
                args: list[str],
                *,
                operation: str,
            ) -> None:
                self.calls.append((project_name, compose_file, args, operation))

        manager = _RecordingComposeManager()

        ran = await manager.down_project(
            project_name="awf_ws_missing",
            compose_file=tmp_path / "missing-compose.yml",
            workspace_id="ws_missing",
        )

        assert manager.calls == []
        # A missing compose file noops and signals the down never ran so
        # callers (e.g. ``teardown_project``) can fall back to a label reap.
        assert ran is False

    @pytest.mark.unit
    async def test_remove_project_by_label_removes_containers_networks_and_volumes(
        self,
        tmp_path: Path,
    ) -> None:
        """Project label cleanup removes containers, networks, and volumes."""

        class _RecordingComposeManager(ComposeManager):
            def __init__(self) -> None:
                super().__init__(work_dir=tmp_path / "work", template_path=_TEMPLATE)
                self.calls: list[tuple[str, ...]] = []

            async def _docker_capture(self, args: list[str], *, operation: str) -> str:
                self.calls.append((operation, *args))
                if operation == "ps":
                    return "container-a\ncontainer-b\n"
                if operation == "network ls":
                    return "network-a\nnetwork-b\n"
                if operation == "volume ls":
                    return "volume-a\nvolume-b\n"
                return ""

        manager = _RecordingComposeManager()

        with structlog.testing.capture_logs() as captured:
            await manager.remove_project_by_label(
                project_name="awf_ws_lost",
                workspace_id="ws_lost",
                remove_volumes=True,
            )

        label_filter = "label=com.docker.compose.project=awf_ws_lost"
        assert manager.calls == [
            ("ps", "ps", "-aq", "--filter", label_filter),
            ("rm", "rm", "-f", "container-a", "container-b"),
            ("network ls", "network", "ls", "-q", "--filter", label_filter),
            ("network rm", "network", "rm", "network-a", "network-b"),
            ("volume ls", "volume", "ls", "-q", "--filter", label_filter),
            ("volume rm", "volume", "rm", "-f", "volume-a", "volume-b"),
        ]
        event = next(item for item in captured if item["event"] == "compose.project_label_removed")
        assert event["containers"] == 2
        assert event["networks"] == 2
        assert event["volumes"] == 2

    @pytest.mark.unit
    async def test_remove_project_by_label_can_keep_volumes(
        self,
        tmp_path: Path,
    ) -> None:
        """Project label cleanup can leave matching volumes in place."""

        class _RecordingComposeManager(ComposeManager):
            def __init__(self) -> None:
                super().__init__(work_dir=tmp_path / "work", template_path=_TEMPLATE)
                self.calls: list[tuple[str, ...]] = []

            async def _docker_capture(self, args: list[str], *, operation: str) -> str:
                self.calls.append((operation, *args))
                if operation == "ps":
                    return ""
                if operation == "network ls":
                    return "network-a\n"
                if operation == "volume ls":
                    raise AssertionError("volume listing should be skipped")
                return ""

        manager = _RecordingComposeManager()

        with structlog.testing.capture_logs() as captured:
            await manager.remove_project_by_label(
                project_name="awf_ws_keep_volumes",
                workspace_id="ws_keep_volumes",
                remove_volumes=False,
            )

        label_filter = "label=com.docker.compose.project=awf_ws_keep_volumes"
        assert manager.calls == [
            ("ps", "ps", "-aq", "--filter", label_filter),
            ("network ls", "network", "ls", "-q", "--filter", label_filter),
            ("network rm", "network", "rm", "network-a"),
        ]
        event = next(item for item in captured if item["event"] == "compose.project_label_removed")
        assert event["volumes"] == 0

    @pytest.mark.unit
    async def test_teardown_project_removes_volumes_via_down(
        self,
        tmp_path: Path,
    ) -> None:
        """A normal teardown drives ``down -v`` and reports success."""
        compose_file = tmp_path / "work" / "compose" / "ws_ok" / "compose.yml"
        compose_file.parent.mkdir(parents=True, exist_ok=True)
        compose_file.write_text("services: {}\n", encoding="utf-8")

        class _RecordingComposeManager(ComposeManager):
            def __init__(self) -> None:
                super().__init__(work_dir=tmp_path / "work", template_path=_TEMPLATE)
                self.calls: list[tuple[str, list[str]]] = []

            async def _compose(
                self,
                project_name: str,
                compose_file: Path,
                args: list[str],
                *,
                operation: str,
            ) -> None:
                self.calls.append((operation, args))

        manager = _RecordingComposeManager()

        result = await manager.teardown_project(
            project_name="awf_ws_ok",
            compose_file=compose_file,
            workspace_id="ws_ok",
            remove_volumes=True,
        )

        assert result.status == "succeeded"
        assert result.reason_code == "DOCKER_COMPOSE_DOWN_SUCCEEDED"
        assert result.ok is True
        assert manager.calls == [("down", ["down", "--remove-orphans", "-v"])]

    @pytest.mark.unit
    async def test_teardown_project_falls_back_to_label_removal(
        self,
        tmp_path: Path,
    ) -> None:
        """When ``down`` fails, teardown reaps volumes via label removal."""
        compose_file = tmp_path / "work" / "compose" / "ws_fb" / "compose.yml"
        compose_file.parent.mkdir(parents=True, exist_ok=True)
        compose_file.write_text("services: {}\n", encoding="utf-8")

        class _RecordingComposeManager(ComposeManager):
            def __init__(self) -> None:
                super().__init__(work_dir=tmp_path / "work", template_path=_TEMPLATE)
                self.label_calls: list[tuple[str, ...]] = []

            async def _compose(
                self,
                project_name: str,
                compose_file: Path,
                args: list[str],
                *,
                operation: str,
            ) -> None:
                raise ComposeOperationError(
                    operation=operation,
                    returncode=1,
                    stdout="",
                    stderr="compose file unusable",
                )

            async def _docker_capture(self, args: list[str], *, operation: str) -> str:
                self.label_calls.append((operation, *args))
                if operation == "volume ls":
                    return "awf-ws_fb-dind_data\n"
                return ""

            async def _docker(self, args: list[str], *, operation: str) -> None:
                self.label_calls.append((operation, *args))

        manager = _RecordingComposeManager()

        result = await manager.teardown_project(
            project_name="awf_ws_fb",
            compose_file=compose_file,
            workspace_id="ws_fb",
            remove_volumes=True,
        )

        assert result.status == "succeeded"
        assert result.reason_code == "DOCKER_COMPOSE_PROJECT_LABEL_REMOVED"
        # Volume removal was attempted on the label-scoped fallback path.
        assert ("volume rm", "volume", "rm", "-f", "awf-ws_fb-dind_data") in manager.label_calls

    @pytest.mark.unit
    async def test_teardown_project_reports_failure_when_fallback_also_fails(
        self,
        tmp_path: Path,
    ) -> None:
        """Both ``down`` and label removal failing yields a loud failure."""
        compose_file = tmp_path / "work" / "compose" / "ws_fail" / "compose.yml"
        compose_file.parent.mkdir(parents=True, exist_ok=True)
        compose_file.write_text("services: {}\n", encoding="utf-8")

        class _RecordingComposeManager(ComposeManager):
            def __init__(self) -> None:
                super().__init__(work_dir=tmp_path / "work", template_path=_TEMPLATE)

            async def _compose(
                self,
                project_name: str,
                compose_file: Path,
                args: list[str],
                *,
                operation: str,
            ) -> None:
                raise ComposeOperationError(
                    operation=operation, returncode=1, stdout="", stderr="down boom"
                )

            async def _docker_capture(self, args: list[str], *, operation: str) -> str:
                raise ComposeOperationError(
                    operation=operation,
                    returncode=1,
                    stdout="",
                    stderr="daemon unreachable",
                    reason_code="DOCKER_UNAVAILABLE",
                )

        manager = _RecordingComposeManager()

        result = await manager.teardown_project(
            project_name="awf_ws_fail",
            compose_file=compose_file,
            workspace_id="ws_fail",
            remove_volumes=True,
        )

        assert result.status == "failed"
        # The fallback failed with a specific ``DOCKER_UNAVAILABLE`` classification;
        # teardown must surface that rather than collapse it into the generic
        # down-failed bucket, while still reporting a loud failure.
        assert result.reason_code == "DOCKER_UNAVAILABLE"
        assert result.error is not None
        assert result.ok is False

    @pytest.mark.unit
    async def test_teardown_project_reaps_stale_volumes_when_compose_file_missing(
        self,
        tmp_path: Path,
    ) -> None:
        """A gone compose dir still reaps volumes an earlier GC run left behind.

        Regression for the historical leak: an earlier GC run removed the
        compose directory without ``-v``, so the ``awf_<workspace>`` volumes
        survive. ``teardown_project`` must not short-circuit on the missing
        compose file -- it falls back to label-scoped teardown so the leaked
        volumes are reclaimed instead of being reported as a successful skip.
        """

        class _RecordingComposeManager(ComposeManager):
            def __init__(self) -> None:
                super().__init__(work_dir=tmp_path / "work", template_path=_TEMPLATE)
                self.compose_calls: list[str] = []
                self.label_calls: list[tuple[str, ...]] = []

            async def _compose(self, *args: object, **kwargs: object) -> None:
                self.compose_calls.append("compose")

            async def _docker_capture(self, args: list[str], *, operation: str) -> str:
                self.label_calls.append((operation, *args))
                if operation == "volume ls":
                    return "awf-ws_gone-dind_data\n"
                return ""

            async def _docker(self, args: list[str], *, operation: str) -> None:
                self.label_calls.append((operation, *args))

        manager = _RecordingComposeManager()

        result = await manager.teardown_project(
            project_name="awf_ws_gone",
            compose_file=tmp_path / "work" / "compose" / "ws_gone" / "compose.yml",
            workspace_id="ws_gone",
            remove_volumes=True,
        )

        assert result.status == "succeeded"
        assert result.reason_code == "DOCKER_COMPOSE_PROJECT_LABEL_REMOVED"
        assert result.ok is True
        # No compose file -> never invokes ``down``; reaps via label scope instead.
        assert manager.compose_calls == []
        assert ("volume rm", "volume", "rm", "-f", "awf-ws_gone-dind_data") in manager.label_calls

    @pytest.mark.unit
    async def test_teardown_project_reaps_volumes_when_compose_file_vanishes_mid_teardown(
        self,
        tmp_path: Path,
    ) -> None:
        """A compose file removed between the existence check and ``down`` still reaps.

        ``teardown_project`` confirms the compose file exists, but a concurrent
        GC run can delete the compose directory before ``down_project`` runs its
        own existence check. ``down_project`` then silently noops; teardown must
        route to the label-scoped fallback so the per-workspace volumes are
        reclaimed instead of being reported as a successful down that removed
        nothing.
        """
        compose_file = tmp_path / "work" / "compose" / "ws_race" / "compose.yml"
        compose_file.parent.mkdir(parents=True, exist_ok=True)
        compose_file.write_text("services: {}\n", encoding="utf-8")

        class _RacingComposeManager(ComposeManager):
            def __init__(self) -> None:
                super().__init__(work_dir=tmp_path / "work", template_path=_TEMPLATE)
                self.compose_calls: list[str] = []
                self.label_calls: list[tuple[str, ...]] = []

            async def down_project(
                self,
                *,
                project_name: str,
                compose_file: Path,
                workspace_id: str,
                remove_volumes: bool = True,
            ) -> bool:
                # Simulate a concurrent GC removing the compose directory after
                # ``teardown_project``'s existence check but before the down.
                compose_file.unlink()
                return await super().down_project(
                    project_name=project_name,
                    compose_file=compose_file,
                    workspace_id=workspace_id,
                    remove_volumes=remove_volumes,
                )

            async def _compose(self, *args: object, **kwargs: object) -> None:
                self.compose_calls.append("compose")

            async def _docker_capture(self, args: list[str], *, operation: str) -> str:
                self.label_calls.append((operation, *args))
                if operation == "volume ls":
                    return "awf-ws_race-dind_data\n"
                return ""

            async def _docker(self, args: list[str], *, operation: str) -> None:
                self.label_calls.append((operation, *args))

        manager = _RacingComposeManager()

        result = await manager.teardown_project(
            project_name="awf_ws_race",
            compose_file=compose_file,
            workspace_id="ws_race",
            remove_volumes=True,
        )

        assert result.status == "succeeded"
        assert result.reason_code == "DOCKER_COMPOSE_PROJECT_LABEL_REMOVED"
        assert result.ok is True
        # ``down`` noop'd (file vanished) -> never invoked compose; the leaked
        # volume is reaped via label scope instead.
        assert manager.compose_calls == []
        assert ("volume rm", "volume", "rm", "-f", "awf-ws_race-dind_data") in manager.label_calls

    @pytest.mark.unit
    async def test_teardown_project_is_idempotent_when_nothing_left_to_reap(
        self,
        tmp_path: Path,
    ) -> None:
        """A truly gone stack (no compose file, no labelled resources) stays ok."""

        class _RecordingComposeManager(ComposeManager):
            def __init__(self) -> None:
                super().__init__(work_dir=tmp_path / "work", template_path=_TEMPLATE)
                self.compose_calls: list[str] = []
                self.removed: list[str] = []

            async def _compose(self, *args: object, **kwargs: object) -> None:
                self.compose_calls.append("compose")

            async def _docker_capture(self, *args: object, **kwargs: object) -> str:
                return ""

            async def _docker(self, args: list[str], *, operation: str) -> None:
                self.removed.append(operation)

        manager = _RecordingComposeManager()

        result = await manager.teardown_project(
            project_name="awf_ws_empty",
            compose_file=tmp_path / "work" / "compose" / "ws_empty" / "compose.yml",
            workspace_id="ws_empty",
            remove_volumes=True,
        )

        assert result.ok is True
        assert result.status == "succeeded"
        # Nothing matched the project label, so no removal commands ran.
        assert manager.compose_calls == []
        assert manager.removed == []

    @pytest.mark.unit
    async def test_teardown_project_removes_label_less_volume_by_name(
        self,
        tmp_path: Path,
    ) -> None:
        """A row-less volume whose compose-project label is gone is removed by name.

        Regression for PRRT_kwDOSJAM6s6LCiLk: the #637 name fallback recovers a
        workspace id from ``awf-ws_gone-postgres_data`` when its
        ``com.docker.compose.project`` label *value* is empty, so the label-scoped
        ``volume ls --filter label=...=awf-ws_gone`` matches nothing. Without
        forwarding the recovered name the reaper would report the stack reaped
        while the volume silently remained; the teardown must ``volume rm`` it by
        name and count it in the removal log.
        """

        class _RecordingComposeManager(ComposeManager):
            def __init__(self) -> None:
                super().__init__(work_dir=tmp_path / "work", template_path=_TEMPLATE)
                self.label_calls: list[tuple[str, ...]] = []

            async def _docker_capture(self, args: list[str], *, operation: str) -> str:
                self.label_calls.append((operation, *args))
                # Label-scoped probes find nothing: the volume lost its project label.
                return ""

            async def _docker(self, args: list[str], *, operation: str) -> None:
                self.label_calls.append((operation, *args))

        manager = _RecordingComposeManager()

        with structlog.testing.capture_logs() as captured:
            result = await manager.teardown_project(
                project_name="awf-ws_gone",
                compose_file=tmp_path / "work" / "compose" / "ws_gone" / "compose.yml",
                workspace_id="ws_gone",
                remove_volumes=True,
                fallback_volume_names=("awf-ws_gone-postgres_data",),
            )

        assert result.status == "succeeded"
        assert result.reason_code == "DOCKER_COMPOSE_PROJECT_LABEL_REMOVED"
        # The recovered name is removed by name even though the label filter
        # matched nothing -- the leak the fallback exists to close.
        assert (
            "volume rm",
            "volume",
            "rm",
            "-f",
            "awf-ws_gone-postgres_data",
        ) in manager.label_calls
        event = next(item for item in captured if item["event"] == "compose.project_label_removed")
        assert event["volumes"] == 1

    @pytest.mark.unit
    async def test_remove_project_by_label_dedups_and_unions_fallback_volume_names(
        self,
        tmp_path: Path,
    ) -> None:
        """Fallback names union with the label-scoped set, deduped, removed once.

        A workspace surfaces several volumes (``-postgres_data`` + ``-dind_data``).
        A volume still carrying the project label is found by the label scope; a
        sibling that lost its label is recovered by name. The by-name removal must
        add only the missing one (no double-remove of the label-matched volume).
        """

        class _RecordingComposeManager(ComposeManager):
            def __init__(self) -> None:
                super().__init__(work_dir=tmp_path / "work", template_path=_TEMPLATE)
                self.calls: list[tuple[str, ...]] = []

            async def _docker_capture(self, args: list[str], *, operation: str) -> str:
                self.calls.append((operation, *args))
                if operation == "volume ls":
                    return "awf-ws_dup-postgres_data\n"
                return ""

            async def _docker(self, args: list[str], *, operation: str) -> None:
                self.calls.append((operation, *args))

        manager = _RecordingComposeManager()

        with structlog.testing.capture_logs() as captured:
            await manager.remove_project_by_label(
                project_name="awf-ws_dup",
                workspace_id="ws_dup",
                remove_volumes=True,
                # The first name is already label-matched (deduped out); the second
                # is the genuinely label-less sibling that only a by-name reap reaches.
                fallback_volume_names=("awf-ws_dup-postgres_data", "awf-ws_dup-dind_data"),
            )

        assert (
            "volume rm",
            "volume",
            "rm",
            "-f",
            "awf-ws_dup-postgres_data",
            "awf-ws_dup-dind_data",
        ) in manager.calls
        event = next(item for item in captured if item["event"] == "compose.project_label_removed")
        assert event["volumes"] == 2

    @pytest.mark.unit
    async def test_remove_project_by_label_skips_fallback_names_when_keeping_volumes(
        self,
        tmp_path: Path,
    ) -> None:
        """``remove_volumes=False`` leaves recovered volume names in place too.

        A retained-terminal stack keeps its volumes as salvage evidence; the
        by-name fallback must honour that gate and never remove them, exactly like
        the label-scoped volume reap it backstops.
        """

        class _RecordingComposeManager(ComposeManager):
            def __init__(self) -> None:
                super().__init__(work_dir=tmp_path / "work", template_path=_TEMPLATE)
                self.calls: list[tuple[str, ...]] = []

            async def _docker_capture(self, args: list[str], *, operation: str) -> str:
                self.calls.append((operation, *args))
                if operation == "volume ls":
                    raise AssertionError("volume listing should be skipped")
                return ""

            async def _docker(self, args: list[str], *, operation: str) -> None:
                self.calls.append((operation, *args))

        manager = _RecordingComposeManager()

        await manager.remove_project_by_label(
            project_name="awf-ws_keep",
            workspace_id="ws_keep",
            remove_volumes=False,
            fallback_volume_names=("awf-ws_keep-postgres_data",),
        )

        assert not any(call[0] == "volume rm" for call in manager.calls)

    @pytest.mark.unit
    async def test_teardown_project_fails_loud_when_label_probe_unavailable(
        self,
        tmp_path: Path,
    ) -> None:
        """A missing compose file with an unreachable daemon is a loud failure."""

        class _RecordingComposeManager(ComposeManager):
            def __init__(self) -> None:
                super().__init__(work_dir=tmp_path / "work", template_path=_TEMPLATE)

            async def _docker_capture(self, args: list[str], *, operation: str) -> str:
                raise ComposeOperationError(
                    operation=operation,
                    returncode=1,
                    stdout="",
                    stderr="daemon unreachable",
                    reason_code="DOCKER_UNAVAILABLE",
                )

        manager = _RecordingComposeManager()

        result = await manager.teardown_project(
            project_name="awf_ws_nodaemon",
            compose_file=tmp_path / "work" / "compose" / "ws_nodaemon" / "compose.yml",
            workspace_id="ws_nodaemon",
            remove_volumes=True,
        )

        assert result.status == "failed"
        # The label probe failed with ``DOCKER_UNAVAILABLE``; that specific
        # classification must survive instead of collapsing into the generic
        # down-failed code, while the failure stays loud.
        assert result.reason_code == "DOCKER_UNAVAILABLE"
        assert result.ok is False

    @pytest.mark.unit
    async def test_compose_command_reports_missing_docker_binary(
        self,
        manager: ComposeManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Compose command execution classifies a missing docker binary."""

        async def _raise_missing(*_args: object, **_kwargs: object) -> _FakeProcess:
            raise FileNotFoundError("docker")

        monkeypatch.setattr(
            compose_module.asyncio,
            "create_subprocess_exec",
            _raise_missing,
        )

        with pytest.raises(ComposeOperationError) as exc:
            await manager._compose(  # noqa: SLF001
                "awf_ws_missing_docker",
                tmp_path / "compose.yml",
                ["up", "-d"],
                operation="up",
            )

        assert exc.value.returncode == 127
        assert exc.value.reason_code == "DOCKER_UNAVAILABLE"
        assert "docker" in exc.value.stderr

    @pytest.mark.unit
    async def test_compose_command_uses_environment_project_and_file_contract(
        self,
        manager: ComposeManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Compose commands receive COMPOSE_PROJECT_NAME and COMPOSE_FILE."""
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        async def _spawn(*args: object, **kwargs: object) -> _FakeProcess:
            calls.append((args, kwargs))
            return _FakeProcess(returncode=0)

        compose_file = tmp_path / "compose.yml"
        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        await manager._compose(  # noqa: SLF001
            "awf_ws_long_flags",
            compose_file,
            ["up", "-d"],
            operation="up",
        )

        assert calls
        args, kwargs = calls[0]
        assert args == ("docker", "compose", "up", "-d")
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert env["COMPOSE_PROJECT_NAME"] == "awf_ws_long_flags"
        assert env["COMPOSE_FILE"] == str(compose_file)

    @pytest.mark.unit
    async def test_ensure_project_up_without_wait_omits_wait_flags(
        self,
        manager: ComposeManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Launching without wait omits compose wait flags."""
        calls: list[tuple[object, ...]] = []

        async def _spawn(*args: object, **_kwargs: object) -> _FakeProcess:
            calls.append(args)
            return _FakeProcess(returncode=0)

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        await manager.ensure_project_up(
            project_name="awf_ws_resume",
            compose_file=tmp_path / "compose.yml",
            workspace_id="ws_resume",
            wait=False,
        )

        assert calls
        assert calls[0] == ("docker", "compose", "up", "-d", "--remove-orphans")
        assert "--wait" not in calls[0]

    @pytest.mark.unit
    async def test_ensure_project_up_can_force_recreate_services(
        self,
        manager: ComposeManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Recovery callers can force a recreate of a specific service."""
        calls: list[tuple[object, ...]] = []

        async def _spawn(*args: object, **_kwargs: object) -> _FakeProcess:
            calls.append(args)
            return _FakeProcess(returncode=0)

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        await manager.ensure_project_up(
            project_name="awf_ws_resume",
            compose_file=tmp_path / "compose.yml",
            workspace_id="ws_resume",
            force_recreate=True,
            services=("agent",),
        )

        assert calls
        assert calls[0] == (
            "docker",
            "compose",
            "up",
            "-d",
            "--remove-orphans",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "300",
            "agent",
        )

    @pytest.mark.unit
    async def test_compose_command_times_out_and_kills_hung_process(
        self,
        manager: ComposeManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Hung compose commands are killed and reported as timeouts."""
        process = _HangingProcess()

        async def _spawn(*_args: object, **_kwargs: object) -> _HangingProcess:
            return process

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        with pytest.raises(ComposeOperationError) as exc:
            await manager._compose(  # noqa: SLF001
                "awf_ws_hung_compose",
                tmp_path / "compose.yml",
                ["up", "-d", "--wait"],
                operation="up",
                capture_timeout_seconds=0.01,
            )

        assert process.kill_called is True
        assert exc.value.returncode == 124
        assert exc.value.reason_code == "DOCKER_COMMAND_TIMEOUT"
        assert "docker compose up exceeded 0.01s timeout" in exc.value.stderr
