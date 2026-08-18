"""Isolated re-ask Git snapshot and legacy-upgrade regression coverage."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from awf.adapters import base as adapter_base
from awf.adapters import base_isolated_reask
from awf.adapters.opencode import OpenCodeAdapter
from awf.common.commands import FakeCommandRunner
from awf.profiles.models import WorkspaceProfile
from tests.unit.adapters.test_adapter_legacy_reask import _PROMPT, _linked_reask_worktree


class TestIsolatedReaskAdapter:
    """Clarification re-asks preserve legacy stack recovery behavior."""

    def test_isolated_reask_git_metadata_binds_keeps_snapshot_when_split_index_copy_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Optional split-index copying cannot discard a completed snapshot."""
        mirror_path, worktree_path, head_oid, _unrelated_oid = _linked_reask_worktree(tmp_path)
        monkeypatch.setattr(
            base_isolated_reask,
            "_split_index_backing_file_name",
            lambda _index_path, _expected_ref: "sharedindex.missing",
        )

        temporary_metadata, binds = adapter_base._isolated_reask_git_metadata_volume_binds(
            worktree_path,
            expected_ref=head_oid,
            expected_source_mirror=mirror_path,
        )

        assert temporary_metadata is not None
        try:
            snapshot_path = Path(temporary_metadata.name) / "linked-git"
            assert (snapshot_path / "index").is_file()
            assert binds == (
                (snapshot_path, str(mirror_path / "worktrees" / worktree_path.name)),
                (Path(temporary_metadata.name) / "common-git", "/awf-clarification-git-common"),
            )
        finally:
            temporary_metadata.cleanup()

    def test_isolated_reask_git_metadata_binds_skip_snapshot_when_clone_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A failed local snapshot never falls back to shared mirror binds."""
        mirror_path, worktree_path, head_oid, _unrelated_oid = _linked_reask_worktree(tmp_path)

        def _clone_failure(*_args: Any, **_kwargs: Any) -> None:
            raise subprocess.CalledProcessError(1, ["git", "clone"])

        monkeypatch.setattr(base_isolated_reask.subprocess, "run", _clone_failure)

        temporary_metadata, binds = adapter_base._isolated_reask_git_metadata_volume_binds(
            worktree_path,
            expected_ref=head_oid,
            expected_source_mirror=mirror_path,
        )

        assert temporary_metadata is None
        assert binds == ()

    def test_isolated_reask_git_metadata_binds_skip_snapshot_when_tempdir_creation_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A local metadata snapshot is never replaced with a shared-mirror bind."""
        mirror_path, worktree_path, head_oid, _unrelated_oid = _linked_reask_worktree(tmp_path)

        class _TemporaryDirectoryFailure:
            @classmethod
            def __class_getitem__(cls, _item: object) -> type[_TemporaryDirectoryFailure]:
                return cls

            def __new__(cls, *_args: object, **_kwargs: object) -> _TemporaryDirectoryFailure:
                raise OSError("temporary metadata directory unavailable")

        monkeypatch.setattr(
            base_isolated_reask.tempfile,
            "TemporaryDirectory",
            _TemporaryDirectoryFailure,
        )

        temporary_metadata, binds = adapter_base._isolated_reask_git_metadata_volume_binds(
            worktree_path,
            expected_ref=head_oid,
            expected_source_mirror=mirror_path,
        )

        assert temporary_metadata is None
        assert binds == ()

    def test_linked_worktree_common_git_dir_preserves_absolute_commondir(
        self, tmp_path: Path
    ) -> None:
        """A valid absolute commondir does not acquire the linked-directory prefix."""
        snapshot_path = tmp_path / "snapshot"
        snapshot_path.mkdir()
        expected_source_mirror = tmp_path / "mirror"
        (snapshot_path / "commondir").write_text(f"{expected_source_mirror}\n", encoding="utf-8")
        linked_git_dir = tmp_path / "worktrees" / "reask"
        linked_git_dir.mkdir(parents=True)

        assert (
            base_isolated_reask._linked_worktree_common_git_dir(  # noqa: SLF001
                snapshot_path,
                linked_git_dir,
                expected_source_mirror=expected_source_mirror,
            )
            == expected_source_mirror
        )

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
    async def test_isolated_reask_attaches_selected_legacy_model_service_without_recreation(
        self, tmp_path: Path
    ) -> None:
        """A stateful legacy sidecar keeps its container while gaining the route."""
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
        runner.queue_result(
            returncode=1,
            stderr="Error response from daemon: network awf-ws_legacy-clarification-model-net not found",
        )
        runner.queue_result()
        runner.queue_result(stdout="stateful-model-container\n")
        runner.queue_result()
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
            "network",
            "inspect",
            "--format",
            '{{ range $container_id, $_ := .Containers }}{{ printf "%s\\n" $container_id }}{{ end }}',
            "awf-ws_legacy-clarification-model-net",
        ]
        assert runner.calls[1].args[:8] == [
            "docker",
            "network",
            "create",
            "--internal",
            "--label",
            "com.docker.compose.project=awf_ws_legacy",
            "--label",
            "com.docker.compose.network=clarification_model_net",
        ]
        assert runner.calls[1].args[-1] == "awf-ws_legacy-clarification-model-net"
        assert any(
            label.startswith("io.awf.clarification-network-creation=")
            for label in runner.calls[1].args
        )
        assert runner.calls[2].args == [
            "docker",
            "compose",
            "-p",
            "awf_ws_legacy",
            "-f",
            str(compose_file),
            "ps",
            "-q",
            "ollama-sidecar",
        ]
        assert runner.calls[3].args == [
            "docker",
            "network",
            "connect",
            "--alias",
            "ollama-sidecar",
            "awf-ws_legacy-clarification-model-net",
            "stateful-model-container",
        ]
        assert all("--force-recreate" not in call.args for call in runner.calls)
        assert "run" in runner.calls[4].args
        assert runner.calls[4].args[runner.calls[4].args.index("run") + 1 :][0:3] == [
            "--rm",
            "--no-deps",
            "-T",
        ]
