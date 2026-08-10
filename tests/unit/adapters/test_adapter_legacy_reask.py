"""Isolated clarification re-ask adapter regression tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event
from typing import Any

import pytest
import yaml

from awf.adapters import base as adapter_base
from awf.adapters.base import AgentRunError
from awf.adapters.codex import CodexAdapter
from awf.adapters.opencode import OpenCodeAdapter
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.db.enums import AgentRuntime
from awf.profiles.models import WorkspaceProfile

_PROMPT = "Add a one-line docstring to src/module/__init__.py."
_COMPOSE_PROJECT = "awf_ws_xyz"
_COMPOSE_FILE = Path("/fake/path/compose.yml")


def _write_legacy_opencode_ollama_compose(tmp_path: Path) -> Path:
    """Create a legacy stack whose clarification endpoint is an Ollama sidecar."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "ollama-sidecar": {
                        "image": "ollama/ollama:latest",
                        "networks": ["awf_net"],
                    },
                    "agent": {
                        "image": "awf-agent-runtime:latest",
                        "environment": {
                            "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama-sidecar:11434"
                        },
                        "networks": ["awf_net"],
                    },
                },
                "networks": {"awf_net": {"name": "awf-ws_legacy-net"}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return compose_file


class TestIsolatedReaskAdapter:
    """Clarification re-asks preserve legacy stack recovery behavior."""

    @pytest.mark.unit
    async def test_uses_requested_compose_exec_workdir(self) -> None:
        runner = FakeCommandRunner()
        adapter = CodexAdapter(runner=runner)

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
            workdir="/workspace/.awf-needs-human-reask-test",
        )

        args = runner.calls[0].args
        exec_idx = args.index("exec")
        assert args[exec_idx : exec_idx + 4] == [
            "exec",
            "-T",
            "-w",
            "/workspace/.awf-needs-human-reask-test",
        ]
        assert "--skip-git-repo-check" not in args

    async def test_uses_one_off_container_for_isolated_reask_worktree(self) -> None:
        """A clarification re-ask must not share the primary agent mount namespace."""
        runner = FakeCommandRunner()
        adapter = CodexAdapter(runner=runner)

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
            isolated_worktree_host_path=Path("/worktrees/ws_xyz/.awf-needs-human-reask-test"),
        )

        args = runner.calls[0].args
        run_idx = args.index("run")
        assert args[run_idx : run_idx + 4] == ["run", "--rm", "--no-deps", "-T"]
        assert args[args.index("-w", run_idx) + 1] == "/workspace"
        assert args[args.index("-v", run_idx) + 1] == (
            "/worktrees/ws_xyz/.awf-needs-human-reask-test:/workspace"
        )
        service_idx = args.index("clarification", run_idx)
        assert service_idx > args.index("-v", run_idx)
        assert "-e" not in args[run_idx:service_idx]
        assert args[service_idx + 1 : service_idx + 3] == ["sh", "-lc"]
        assert "--skip-git-repo-check" in args[service_idx:]

    @pytest.mark.unit
    async def test_isolated_reask_upgrade_keeps_selected_opencode_provider_credentials(
        self, tmp_path: Path
    ) -> None:
        """A legacy OpenCode re-ask keeps credentials for its selected provider."""
        compose_file = tmp_path / "compose.yml"
        compose_file.write_text(
            yaml.safe_dump(
                {
                    "services": {
                        "agent": {
                            "image": "awf-agent-runtime:latest",
                            "environment": {"OPENAI_API_KEY": "${OPENAI_API_KEY}"},
                            "volumes": [
                                f"{tmp_path / 'worktree'}:/workspace",
                                f"{tmp_path / 'codex'}:/home/agent/.codex:rw",
                            ],
                        }
                    },
                    "networks": {"awf_net": {"name": "awf-ws_legacy-net"}},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        runner = FakeCommandRunner()
        adapter = OpenCodeAdapter(runner=runner)

        await adapter.run(
            compose_project="awf_ws_legacy",
            compose_file=compose_file,
            prompt=_PROMPT,
            model="openai/gpt-5.3-codex",
            workspace_id="ws_legacy",
            isolated_worktree_host_path=tmp_path / "reask",
        )

        rendered = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
        clarification = rendered["services"]["clarification"]
        assert clarification["profiles"] == ["awf-clarification"]
        assert clarification["environment"] == {"OPENAI_API_KEY": "${OPENAI_API_KEY}"}
        args = runner.calls[0].args
        assert args.index("clarification", args.index("run")) > args.index("run")

    @pytest.mark.unit
    async def test_isolated_reask_recreates_selected_legacy_model_service_before_clarification(
        self, tmp_path: Path
    ) -> None:
        """A legacy sidecar is ready on its new model network before the re-ask."""
        compose_file = tmp_path / "compose.yml"
        compose_file.write_text(
            yaml.safe_dump(
                {
                    "services": {
                        "ollama-sidecar": {
                            "image": "ollama/ollama:latest",
                            "networks": ["awf_net"],
                        },
                        "agent": {
                            "image": "awf-agent-runtime:latest",
                            "environment": {
                                "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama-sidecar:11434"
                            },
                            "networks": ["awf_net"],
                        },
                    },
                    "networks": {"awf_net": {"name": "awf-ws_legacy-net"}},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        runner = FakeCommandRunner()
        adapter = OpenCodeAdapter(runner=runner)
        profile = WorkspaceProfile.model_validate(
            {
                "name": "bounded-sidecar-readiness",
                "docker": {"startup_timeout_seconds": 123},
            }
        )

        await adapter.run(
            compose_project="awf_ws_legacy",
            compose_file=compose_file,
            prompt=_PROMPT,
            model="ollama/kimi-k2.6:cloud",
            workspace_id="ws_legacy",
            profile=profile,
            isolated_worktree_host_path=tmp_path / "reask",
        )

        assert runner.calls[0].args == [
            "docker",
            "compose",
            "-p",
            "awf_ws_legacy",
            "-f",
            str(compose_file),
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "123",
            "ollama-sidecar",
        ]
        assert runner.calls[0].timeout_seconds == 306.0
        assert "run" in runner.calls[1].args
        assert runner.calls[1].args[runner.calls[1].args.index("run") + 1 :][0:3] == [
            "--rm",
            "--no-deps",
            "-T",
        ]

    @pytest.mark.unit
    async def test_isolated_reask_reconciles_interrupted_legacy_model_migration_once(
        self, tmp_path: Path
    ) -> None:
        """A retry completes a migration persisted before its sidecar recreation."""
        compose_file = _write_legacy_opencode_ollama_compose(tmp_path)
        assert adapter_base.upgrade_persisted_clarification_service(
            compose_file=compose_file,
            workspace_id="ws_legacy",
            agent_runtime=AgentRuntime.opencode,
            agent_model="ollama/kimi-k2.6:cloud",
        ) == ("ollama-sidecar",)

        runner = FakeCommandRunner()
        adapter = OpenCodeAdapter(runner=runner)

        await adapter.run(
            compose_project="awf_ws_legacy",
            compose_file=compose_file,
            prompt=_PROMPT,
            model="ollama/kimi-k2.6:cloud",
            workspace_id="ws_legacy",
            isolated_worktree_host_path=tmp_path / "reask",
        )

        assert "up" in runner.calls[0].args
        assert "ollama-sidecar" in runner.calls[0].args
        assert (
            yaml.safe_load(compose_file.read_text(encoding="utf-8"))[
                "x-awf-persisted-clarification-model-network-reconciled"
            ]
            is True
        )

        await adapter.run(
            compose_project="awf_ws_legacy",
            compose_file=compose_file,
            prompt=_PROMPT,
            model="ollama/kimi-k2.6:cloud",
            workspace_id="ws_legacy",
            isolated_worktree_host_path=tmp_path / "reask",
        )

        assert len(runner.calls) == 3
        assert "run" in runner.calls[-1].args

    @pytest.mark.unit
    async def test_isolated_reask_runs_after_persisted_migration_marker_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A marker-write failure leaves a safe retry path without blocking clarification."""
        compose_file = _write_legacy_opencode_ollama_compose(tmp_path)

        def fail_marker(*, compose_file: Path) -> None:
            del compose_file
            raise ValueError("disk full")

        monkeypatch.setattr(
            adapter_base,
            "mark_persisted_clarification_model_network_reconciled",
            fail_marker,
        )
        runner = FakeCommandRunner()
        adapter = OpenCodeAdapter(runner=runner)

        await adapter.run(
            compose_project="awf_ws_legacy",
            compose_file=compose_file,
            prompt=_PROMPT,
            model="ollama/kimi-k2.6:cloud",
            workspace_id="ws_legacy",
            isolated_worktree_host_path=tmp_path / "reask",
        )

        assert "up" in runner.calls[0].args
        assert "run" in runner.calls[1].args
        assert (
            yaml.safe_load(compose_file.read_text(encoding="utf-8"))[
                "x-awf-persisted-clarification-model-network-reconciled"
            ]
            is False
        )

    @pytest.mark.unit
    async def test_isolated_reask_restores_legacy_compose_when_upgrade_is_cancelled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cancellation waits for the upgrade worker before restoring its file edit."""
        compose_file = tmp_path / "compose.yml"
        original_compose_file = b"services:\n  agent:\n    image: awf-agent-runtime:latest\n"
        compose_file.write_bytes(original_compose_file)
        upgrade_started = Event()
        finish_upgrade = Event()
        upgrade_finished = Event()

        def blocking_upgrade(**kwargs: object) -> tuple[str, ...]:
            upgraded_compose_file = kwargs["compose_file"]
            assert isinstance(upgraded_compose_file, Path)
            upgrade_started.set()
            assert finish_upgrade.wait(timeout=1)
            upgraded_compose_file.write_text(
                "services:\n  clarification:\n    image: awf-agent-runtime:latest\n",
                encoding="utf-8",
            )
            upgrade_finished.set()
            return ("ollama-sidecar",)

        monkeypatch.setattr(
            adapter_base,
            "upgrade_persisted_clarification_service",
            blocking_upgrade,
        )
        runner = FakeCommandRunner()
        adapter = OpenCodeAdapter(runner=runner)
        task = asyncio.create_task(
            adapter.run(
                compose_project="awf_ws_legacy",
                compose_file=compose_file,
                prompt=_PROMPT,
                workspace_id="ws_legacy",
                isolated_worktree_host_path=tmp_path / "reask",
            )
        )

        assert await asyncio.to_thread(upgrade_started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish_upgrade.set()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert upgrade_finished.is_set()
        assert compose_file.read_bytes() == original_compose_file
        assert runner.calls == []

    @pytest.mark.unit
    async def test_isolated_reask_stops_when_legacy_model_service_recreation_fails(
        self, tmp_path: Path
    ) -> None:
        """Do not launch an unreachable clarification container after a failed update."""
        compose_file = tmp_path / "compose.yml"
        compose_file.write_text(
            yaml.safe_dump(
                {
                    "services": {
                        "ollama-sidecar": {
                            "image": "ollama/ollama:latest",
                            "networks": ["awf_net"],
                        },
                        "agent": {
                            "image": "awf-agent-runtime:latest",
                            "environment": {
                                "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama-sidecar:11434"
                            },
                            "networks": ["awf_net"],
                        },
                    },
                    "networks": {"awf_net": {"name": "awf-ws_legacy-net"}},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        runner = FakeCommandRunner()
        runner.queue_result(returncode=1, stderr="could not recreate model sidecar")
        adapter = OpenCodeAdapter(runner=runner)

        with pytest.raises(AgentRunError) as exc:
            await adapter.run(
                compose_project="awf_ws_legacy",
                compose_file=compose_file,
                prompt=_PROMPT,
                model="ollama/kimi-k2.6:cloud",
                workspace_id="ws_legacy",
                isolated_worktree_host_path=tmp_path / "reask",
            )

        assert exc.value.reason_code == "CLARIFICATION_MODEL_SERVICE_UPDATE_FAILED"
        assert len(runner.calls) == 4
        assert "run" not in runner.calls[0].args
        assert runner.calls[1].args == [
            "docker",
            "compose",
            "-p",
            "awf_ws_legacy",
            "-f",
            str(compose_file),
            "rm",
            "--stop",
            "--force",
            "ollama-sidecar",
        ]
        assert runner.calls[2].args == [
            "docker",
            "network",
            "rm",
            "awf-ws_legacy-clarification-model-net",
        ]
        assert runner.calls[3].args == [
            "docker",
            "compose",
            "-p",
            "awf_ws_legacy",
            "-f",
            str(compose_file),
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "300",
            "ollama-sidecar",
        ]
        assert runner.calls[3].timeout_seconds == 660.0
        assert (
            "clarification"
            not in yaml.safe_load(compose_file.read_text(encoding="utf-8"))["services"]
        )

        await adapter.run(
            compose_project="awf_ws_legacy",
            compose_file=compose_file,
            prompt=_PROMPT,
            model="ollama/kimi-k2.6:cloud",
            workspace_id="ws_legacy",
            isolated_worktree_host_path=tmp_path / "reask",
        )

        assert runner.calls[4].args == [
            "docker",
            "compose",
            "-p",
            "awf_ws_legacy",
            "-f",
            str(compose_file),
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "300",
            "ollama-sidecar",
        ]
        assert runner.calls[4].timeout_seconds == 660.0
        assert "run" in runner.calls[5].args

    @pytest.mark.unit
    async def test_isolated_reask_keeps_migration_compose_when_network_reap_fails(
        self, tmp_path: Path
    ) -> None:
        """A failed network reap retains its Compose declaration for later teardown."""
        compose_file = _write_legacy_opencode_ollama_compose(tmp_path)
        runner = FakeCommandRunner()
        runner.queue_result(returncode=1, stderr="could not recreate model sidecar")
        runner.queue_result()
        runner.queue_result(returncode=1, stderr="network still has active endpoints")
        adapter = OpenCodeAdapter(runner=runner)

        with pytest.raises(AgentRunError) as exc:
            await adapter.run(
                compose_project="awf_ws_legacy",
                compose_file=compose_file,
                prompt=_PROMPT,
                model="ollama/kimi-k2.6:cloud",
                workspace_id="ws_legacy",
                isolated_worktree_host_path=tmp_path / "reask",
            )

        assert exc.value.reason_code == "CLARIFICATION_MODEL_NETWORK_CLEANUP_FAILED"
        assert exc.value.result.stderr == "network still has active endpoints"
        assert exc.value.details == {"services": ("ollama-sidecar",)}
        assert len(runner.calls) == 3
        rendered = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
        assert rendered["services"]["ollama-sidecar"]["networks"] == [
            "awf_net",
            "clarification_model_net",
        ]
        assert rendered["networks"]["clarification_model_net"] == {
            "name": "awf-ws_legacy-clarification-model-net",
            "internal": True,
        }

    @pytest.mark.unit
    async def test_isolated_reask_restores_legacy_compose_when_network_reap_is_absent(
        self, tmp_path: Path
    ) -> None:
        """An already-reaped model network is a successful rollback cleanup."""
        compose_file = _write_legacy_opencode_ollama_compose(tmp_path)
        original_compose_file = compose_file.read_bytes()
        runner = FakeCommandRunner()
        runner.queue_result(returncode=1, stderr="could not recreate model sidecar")
        runner.queue_result()
        runner.queue_result(
            returncode=1,
            stderr=(
                "Error response from daemon: network "
                "awf-ws_legacy-clarification-model-net not found"
            ),
        )
        adapter = OpenCodeAdapter(runner=runner)

        with pytest.raises(AgentRunError) as exc:
            await adapter.run(
                compose_project="awf_ws_legacy",
                compose_file=compose_file,
                prompt=_PROMPT,
                model="ollama/kimi-k2.6:cloud",
                workspace_id="ws_legacy",
                isolated_worktree_host_path=tmp_path / "reask",
            )

        assert exc.value.reason_code == "CLARIFICATION_MODEL_SERVICE_UPDATE_FAILED"
        assert len(runner.calls) == 4
        assert compose_file.read_bytes() == original_compose_file

    @pytest.mark.unit
    async def test_isolated_reask_surfaces_legacy_model_service_recovery_failure(
        self, tmp_path: Path
    ) -> None:
        """Report a failed rollback recovery instead of the original update failure."""
        compose_file = tmp_path / "compose.yml"
        compose_file.write_text(
            yaml.safe_dump(
                {
                    "services": {
                        "ollama-sidecar": {
                            "image": "ollama/ollama:latest",
                            "networks": ["awf_net"],
                        },
                        "agent": {
                            "image": "awf-agent-runtime:latest",
                            "environment": {
                                "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama-sidecar:11434"
                            },
                            "networks": ["awf_net"],
                        },
                    },
                    "networks": {"awf_net": {"name": "awf-ws_legacy-net"}},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        runner = FakeCommandRunner()
        runner.queue_result(returncode=1, stderr="could not recreate model sidecar")
        runner.queue_result()
        runner.queue_result()
        runner.queue_result(returncode=1, stderr="could not restore model sidecar")
        adapter = OpenCodeAdapter(runner=runner)

        with pytest.raises(AgentRunError) as exc:
            await adapter.run(
                compose_project="awf_ws_legacy",
                compose_file=compose_file,
                prompt=_PROMPT,
                model="ollama/kimi-k2.6:cloud",
                workspace_id="ws_legacy",
                isolated_worktree_host_path=tmp_path / "reask",
            )

        assert exc.value.reason_code == "CLARIFICATION_MODEL_SERVICE_RECOVERY_FAILED"
        assert exc.value.result.returncode == 1
        assert exc.value.result.stderr == "could not restore model sidecar"
        assert exc.value.details == {"services": ("ollama-sidecar",)}
        assert len(runner.calls) == 4
        assert "run" not in runner.calls[-1].args

    @pytest.mark.unit
    async def test_isolated_reask_surfaces_legacy_model_service_recovery_exception(
        self, tmp_path: Path
    ) -> None:
        """Report a rollback recovery exception instead of the update failure."""
        compose_file = _write_legacy_opencode_ollama_compose(tmp_path)

        class _FailingLegacyRecoveryRunner(FakeCommandRunner):
            async def run(self, args: list[str], **kwargs: Any) -> CommandResult:
                result = await super().run(args, **kwargs)
                if len(self.calls) == 4:
                    raise FileNotFoundError("docker not found")
                return result

        runner = _FailingLegacyRecoveryRunner()
        runner.queue_result(returncode=1, stderr="could not recreate model sidecar")
        runner.queue_result()
        runner.queue_result()
        adapter = OpenCodeAdapter(runner=runner)

        with pytest.raises(AgentRunError) as exc:
            await adapter.run(
                compose_project="awf_ws_legacy",
                compose_file=compose_file,
                prompt=_PROMPT,
                model="ollama/kimi-k2.6:cloud",
                workspace_id="ws_legacy",
                isolated_worktree_host_path=tmp_path / "reask",
            )

        assert exc.value.reason_code == "CLARIFICATION_MODEL_SERVICE_RECOVERY_FAILED"
        assert exc.value.result.returncode == 1
        assert exc.value.result.stderr == "FileNotFoundError: docker not found"
        assert exc.value.details == {"services": ("ollama-sidecar",)}
        assert len(runner.calls) == 4

    @pytest.mark.unit
    async def test_isolated_reask_surfaces_terminal_failure_when_legacy_restore_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Do not recreate from the migrated definition if legacy restore fails."""
        compose_file = tmp_path / "compose.yml"
        compose_file.write_text(
            yaml.safe_dump(
                {
                    "services": {
                        "ollama-sidecar": {
                            "image": "ollama/ollama:latest",
                            "networks": ["awf_net"],
                        },
                        "agent": {
                            "image": "awf-agent-runtime:latest",
                            "environment": {
                                "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama-sidecar:11434"
                            },
                            "networks": ["awf_net"],
                        },
                    },
                    "networks": {"awf_net": {"name": "awf-ws_legacy-net"}},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        runner = FakeCommandRunner()
        runner.queue_result(returncode=1, stderr="could not recreate model sidecar")
        adapter = OpenCodeAdapter(runner=runner)

        def fail_restore(*, compose_file: Path, contents: bytes) -> None:
            del compose_file, contents
            raise OSError("disk full")

        monkeypatch.setattr(adapter_base, "_restore_compose_file", fail_restore)

        with pytest.raises(AgentRunError) as exc:
            await adapter.run(
                compose_project="awf_ws_legacy",
                compose_file=compose_file,
                prompt=_PROMPT,
                model="ollama/kimi-k2.6:cloud",
                workspace_id="ws_legacy",
                isolated_worktree_host_path=tmp_path / "reask",
            )

        assert exc.value.reason_code == "CLARIFICATION_MODEL_SERVICE_RECOVERY_FAILED"
        assert exc.value.details == {"services": ("ollama-sidecar",)}
        assert len(runner.calls) == 3

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("restore_fails", "expected_shield_calls", "expected_calls"),
        [(False, 5, 4), (True, 3, 3)],
        ids=["legacy-restore-succeeds", "legacy-restore-fails"],
    )
    async def test_isolated_reask_rolls_back_legacy_migration_when_model_recreation_is_cancelled(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        restore_fails: bool,
        expected_shield_calls: int,
        expected_calls: int,
    ) -> None:
        """Cancellation restores a legacy sidecar only after its definition is restored."""
        compose_file = tmp_path / "compose.yml"
        compose_file.write_text(
            yaml.safe_dump(
                {
                    "services": {
                        "ollama-sidecar": {
                            "image": "ollama/ollama:latest",
                            "networks": ["awf_net"],
                        },
                        "agent": {
                            "image": "awf-agent-runtime:latest",
                            "environment": {
                                "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama-sidecar:11434"
                            },
                            "networks": ["awf_net"],
                        },
                    },
                    "networks": {"awf_net": {"name": "awf-ws_legacy-net"}},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        original_compose_file = compose_file.read_bytes()

        class _CancellingSidecarUpdateRunner(FakeCommandRunner):
            async def run(self, args: list[str], **kwargs: Any) -> CommandResult:
                result = await super().run(args, **kwargs)
                if len(self.calls) == 1:
                    raise asyncio.CancelledError
                return result

        runner = _CancellingSidecarUpdateRunner()
        adapter = OpenCodeAdapter(runner=runner)
        if restore_fails:

            def fail_restore(*, compose_file: Path, contents: bytes) -> None:
                del compose_file, contents
                raise OSError("disk full")

            monkeypatch.setattr(adapter_base, "_restore_compose_file", fail_restore)
        original_shield = asyncio.shield
        shield_calls = 0

        async def cancel_cleanup_and_recovery_shield(task: asyncio.Future[Any]) -> Any:
            nonlocal shield_calls
            shield_calls += 1
            # The first shield protects the legacy upgrade worker. Interrupt
            # the cleanup and recovery shields below, as this regression is
            # specifically about cancellation during model recreation.
            if shield_calls in {2, 4}:
                raise asyncio.CancelledError
            return await original_shield(task)

        monkeypatch.setattr(adapter_base.asyncio, "shield", cancel_cleanup_and_recovery_shield)

        with pytest.raises(asyncio.CancelledError):
            await adapter.run(
                compose_project="awf_ws_legacy",
                compose_file=compose_file,
                prompt=_PROMPT,
                model="ollama/kimi-k2.6:cloud",
                workspace_id="ws_legacy",
                isolated_worktree_host_path=tmp_path / "reask",
            )

        assert shield_calls == expected_shield_calls
        assert len(runner.calls) == expected_calls
        assert runner.calls[1].args == [
            "docker",
            "compose",
            "-p",
            "awf_ws_legacy",
            "-f",
            str(compose_file),
            "rm",
            "--stop",
            "--force",
            "ollama-sidecar",
        ]
        assert runner.calls[2].args == [
            "docker",
            "network",
            "rm",
            "awf-ws_legacy-clarification-model-net",
        ]
        if restore_fails:
            assert runner.calls[-1].args == [
                "docker",
                "network",
                "rm",
                "awf-ws_legacy-clarification-model-net",
            ]
        else:
            assert runner.calls[3].args == [
                "docker",
                "compose",
                "-p",
                "awf_ws_legacy",
                "-f",
                str(compose_file),
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                "--wait",
                "--wait-timeout",
                "300",
                "ollama-sidecar",
            ]
            assert runner.calls[3].timeout_seconds == 660.0
            assert compose_file.read_bytes() == original_compose_file

    @pytest.mark.unit
    @pytest.mark.parametrize("recovery_raises", [False, True], ids=["nonzero", "raises"])
    async def test_isolated_reask_surfaces_failed_legacy_recovery_after_cancellation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery_raises: bool
    ) -> None:
        """Do not discard recovery failure after cancelled model recreation."""
        compose_file = _write_legacy_opencode_ollama_compose(tmp_path)
        original_compose_file = compose_file.read_bytes()

        class _CancellingThenFailingRecoveryRunner(FakeCommandRunner):
            async def run(self, args: list[str], **kwargs: Any) -> CommandResult:
                result = await super().run(args, **kwargs)
                if len(self.calls) == 1:
                    raise asyncio.CancelledError
                if len(self.calls) == 4:
                    if recovery_raises:
                        raise RuntimeError("could not restore model sidecar")
                    return CommandResult(
                        returncode=1,
                        stdout="",
                        stderr="could not restore model sidecar",
                    )
                return result

        runner = _CancellingThenFailingRecoveryRunner()
        adapter = OpenCodeAdapter(runner=runner)
        if recovery_raises:
            original_shield = asyncio.shield
            shield_calls = 0

            async def cancel_recovery_shield(task: asyncio.Future[Any]) -> Any:
                nonlocal shield_calls
                shield_calls += 1
                if shield_calls == 3:
                    await asyncio.sleep(0)
                    raise asyncio.CancelledError
                return await original_shield(task)

            monkeypatch.setattr(adapter_base.asyncio, "shield", cancel_recovery_shield)

        if recovery_raises:
            with pytest.raises(RuntimeError, match="could not restore model sidecar"):
                await adapter.run(
                    compose_project="awf_ws_legacy",
                    compose_file=compose_file,
                    prompt=_PROMPT,
                    model="ollama/kimi-k2.6:cloud",
                    workspace_id="ws_legacy",
                    isolated_worktree_host_path=tmp_path / "reask",
                )
            assert shield_calls == 3
        else:
            with pytest.raises(AgentRunError) as exc:
                await adapter.run(
                    compose_project="awf_ws_legacy",
                    compose_file=compose_file,
                    prompt=_PROMPT,
                    model="ollama/kimi-k2.6:cloud",
                    workspace_id="ws_legacy",
                    isolated_worktree_host_path=tmp_path / "reask",
                )

            assert exc.value.reason_code == "CLARIFICATION_MODEL_SERVICE_RECOVERY_FAILED"
            assert exc.value.result.stderr == "could not restore model sidecar"
            assert exc.value.details == {"services": ("ollama-sidecar",)}

        assert len(runner.calls) == 4
        assert compose_file.read_bytes() == original_compose_file

    @pytest.mark.unit
    async def test_isolated_reask_keeps_migration_compose_when_cancelled_network_reap_fails(
        self, tmp_path: Path
    ) -> None:
        """Cancellation cannot restore a file that would orphan a failed network reap."""
        compose_file = _write_legacy_opencode_ollama_compose(tmp_path)

        class _CancellingSidecarUpdateRunner(FakeCommandRunner):
            async def run(self, args: list[str], **kwargs: Any) -> CommandResult:
                result = await super().run(args, **kwargs)
                if len(self.calls) == 1:
                    raise asyncio.CancelledError
                return result

        runner = _CancellingSidecarUpdateRunner()
        runner.queue_result()
        runner.queue_result()
        runner.queue_result(returncode=1, stderr="network still has active endpoints")
        adapter = OpenCodeAdapter(runner=runner)

        with pytest.raises(AgentRunError) as exc:
            await adapter.run(
                compose_project="awf_ws_legacy",
                compose_file=compose_file,
                prompt=_PROMPT,
                model="ollama/kimi-k2.6:cloud",
                workspace_id="ws_legacy",
                isolated_worktree_host_path=tmp_path / "reask",
            )

        assert exc.value.reason_code == "CLARIFICATION_MODEL_NETWORK_CLEANUP_FAILED"
        assert exc.value.result.stderr == "network still has active endpoints"
        assert len(runner.calls) == 3
        rendered = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
        assert rendered["services"]["ollama-sidecar"]["networks"] == [
            "awf_net",
            "clarification_model_net",
        ]
        assert rendered["networks"]["clarification_model_net"] == {
            "name": "awf-ws_legacy-clarification-model-net",
            "internal": True,
        }

    @pytest.mark.unit
    async def test_isolated_reask_restores_legacy_compose_when_cancelled_network_reap_is_absent(
        self, tmp_path: Path
    ) -> None:
        """Cancellation succeeds when another cleanup already removed the network."""
        compose_file = _write_legacy_opencode_ollama_compose(tmp_path)
        original_compose_file = compose_file.read_bytes()

        class _CancellingSidecarUpdateRunner(FakeCommandRunner):
            async def run(self, args: list[str], **kwargs: Any) -> CommandResult:
                result = await super().run(args, **kwargs)
                if len(self.calls) == 1:
                    raise asyncio.CancelledError
                return result

        runner = _CancellingSidecarUpdateRunner()
        runner.queue_result()
        runner.queue_result()
        runner.queue_result(
            returncode=1,
            stderr=(
                "Error response from daemon: network "
                "awf-ws_legacy-clarification-model-net not found"
            ),
        )
        adapter = OpenCodeAdapter(runner=runner)

        with pytest.raises(asyncio.CancelledError):
            await adapter.run(
                compose_project="awf_ws_legacy",
                compose_file=compose_file,
                prompt=_PROMPT,
                model="ollama/kimi-k2.6:cloud",
                workspace_id="ws_legacy",
                isolated_worktree_host_path=tmp_path / "reask",
            )

        assert len(runner.calls) == 4
        assert compose_file.read_bytes() == original_compose_file

    @pytest.mark.unit
    async def test_isolated_reask_rolls_back_legacy_migration_when_model_recreation_raises(
        self, tmp_path: Path
    ) -> None:
        """Runner failures cannot leave a legacy sidecar off the model network."""
        compose_file = tmp_path / "compose.yml"
        compose_file.write_text(
            yaml.safe_dump(
                {
                    "services": {
                        "ollama-sidecar": {
                            "image": "ollama/ollama:latest",
                            "networks": ["awf_net"],
                        },
                        "agent": {
                            "image": "awf-agent-runtime:latest",
                            "environment": {
                                "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama-sidecar:11434"
                            },
                            "networks": ["awf_net"],
                        },
                    },
                    "networks": {"awf_net": {"name": "awf-ws_legacy-net"}},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        original_compose_file = compose_file.read_bytes()

        class _FailingSidecarUpdateRunner(FakeCommandRunner):
            async def run(self, args: list[str], **kwargs: Any) -> CommandResult:
                await super().run(args, **kwargs)
                raise FileNotFoundError("docker not found")

        runner = _FailingSidecarUpdateRunner()
        adapter = OpenCodeAdapter(runner=runner)

        with pytest.raises(FileNotFoundError, match="docker not found"):
            await adapter.run(
                compose_project="awf_ws_legacy",
                compose_file=compose_file,
                prompt=_PROMPT,
                model="ollama/kimi-k2.6:cloud",
                workspace_id="ws_legacy",
                isolated_worktree_host_path=tmp_path / "reask",
            )

        assert len(runner.calls) == 1
        assert compose_file.read_bytes() == original_compose_file
