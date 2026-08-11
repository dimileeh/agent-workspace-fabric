"""Isolated clarification re-ask adapter regression tests."""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from threading import Event, get_ident
from typing import Any

import pytest
import yaml

from awf.adapters import base as adapter_base
from awf.adapters import base_isolated_reask
from awf.adapters.base import AgentRunError
from awf.adapters.codex import CodexAdapter
from awf.adapters.opencode import OpenCodeAdapter
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.compose_exec import DEFAULT_AGENT_WORKDIR
from awf.db.enums import AgentRuntime
from awf.profiles.models import WorkspaceProfile

_PROMPT = "Add a one-line docstring to src/module/__init__.py."
_COMPOSE_PROJECT = "awf_ws_xyz"
_COMPOSE_FILE = Path("/fake/path/compose.yml")


def _git(
    args: list[str], cwd: Path, *, input: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a Git setup or inspection command for an isolated re-ask fixture."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        input=input,
    )


def _linked_reask_worktree(
    tmp_path: Path, *, object_format: str | None = None, shallow: bool = False
) -> tuple[Path, Path, str, str]:
    """Create a re-ask worktree and an unrelated branch in its shared mirror."""
    origin = tmp_path / "origin"
    origin.mkdir()
    init_args = ["init", "-q", "-b", "main"]
    if object_format is not None:
        init_args.append(f"--object-format={object_format}")
    _git(init_args, origin)
    _git(["config", "user.name", "AWF Test"], origin)
    _git(["config", "user.email", "awf@test.local"], origin)
    (origin / "README.md").write_text("reask head\n", encoding="utf-8")
    _git(["add", "README.md"], origin)
    _git(["commit", "-q", "-m", "reask head"], origin)
    head_oid = _git(["rev-parse", "HEAD"], origin).stdout.strip()
    _git(["checkout", "-q", "-b", "unrelated"], origin)
    (origin / "unrelated.txt").write_text("must stay private\n", encoding="utf-8")
    _git(["add", "unrelated.txt"], origin)
    _git(["commit", "-q", "-m", "unrelated"], origin)
    unrelated_oid = _git(["rev-parse", "HEAD"], origin).stdout.strip()
    _git(["checkout", "-q", "main"], origin)

    mirror_path = tmp_path / "awf-work" / "mirrors" / "owner-repo.git"
    mirror_path.parent.mkdir(parents=True)
    clone_command = ["git", "clone", "--mirror"]
    if shallow:
        clone_command.extend(["--depth", "1", origin.as_uri()])
    else:
        clone_command.append(str(origin))
    subprocess.run(
        [*clone_command, str(mirror_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    worktree_path = tmp_path / "awf-work" / "worktrees" / "reask"
    worktree_path.parent.mkdir(parents=True)
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(mirror_path),
            "worktree",
            "add",
            "-q",
            "-b",
            "reask",
            str(worktree_path),
            "main",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return mirror_path, worktree_path, head_oid, unrelated_oid


def _assert_snapshot_index_is_usable(
    temporary_metadata: tempfile.TemporaryDirectory[str],
    *,
    worktree_path: Path,
    source_index_path: Path,
) -> None:
    """Assert that a copied index is byte-identical and readable by Git."""
    snapshot_path = Path(temporary_metadata.name) / "linked-git"
    assert (snapshot_path / source_index_path.name).read_bytes() == source_index_path.read_bytes()
    (snapshot_path / "commondir").write_text(
        f"{Path(temporary_metadata.name) / 'common-git'}\n", encoding="utf-8"
    )
    assert (
        _git(
            [
                "--git-dir",
                str(snapshot_path),
                "--work-tree",
                str(worktree_path),
                "ls-files",
                "--error-unmatch",
                "metadata/large-index-entry-15999.txt",
            ],
            worktree_path,
        ).stdout.strip()
        == "metadata/large-index-entry-15999.txt"
    )


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
        """Verify uses requested compose exec workdir."""
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
    async def test_isolated_reask_prepares_git_snapshot_off_event_loop(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Git snapshot preparation cannot block concurrent adapter work."""
        event_loop_thread = get_ident()
        snapshot_thread: int | None = None

        def record_snapshot_thread(
            _worktree_path: Path,
            *,
            expected_ref: str,
            expected_source_mirror: Path,
        ) -> tuple[None, tuple[tuple[Path, str], ...]]:
            nonlocal snapshot_thread
            assert expected_ref == "a" * 40
            assert expected_source_mirror == tmp_path / "source-mirror"
            snapshot_thread = get_ident()
            return None, ()

        monkeypatch.setattr(
            adapter_base,
            "_isolated_reask_git_metadata_volume_binds",
            record_snapshot_thread,
        )
        runner = FakeCommandRunner()
        adapter = CodexAdapter(runner=runner)

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
            isolated_worktree_host_path=tmp_path / "reask",
            isolated_worktree_ref="a" * 40,
            isolated_worktree_source_mirror=tmp_path / "source-mirror",
        )

        assert snapshot_thread is not None
        assert snapshot_thread != event_loop_thread

    @pytest.mark.unit
    async def test_isolated_reask_cancellation_cleans_git_snapshot_created_by_worker(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A cancelled awaiter transfers worker-created snapshot cleanup ownership."""
        snapshot_started = Event()
        release_snapshot = Event()
        completed_snapshots: list[tempfile.TemporaryDirectory[str]] = []

        def create_snapshot_after_cancellation(
            _worktree_path: Path,
            *,
            expected_ref: str,
            expected_source_mirror: Path,
        ) -> tuple[tempfile.TemporaryDirectory[str], tuple[tuple[Path, str], ...]]:
            assert expected_ref == "a" * 40
            assert expected_source_mirror == tmp_path / "source-mirror"
            snapshot_started.set()
            assert release_snapshot.wait(timeout=1)
            temporary_metadata = tempfile.TemporaryDirectory[str](dir=tmp_path)
            # Keep the worker result alive: the adapter must explicitly clean
            # it instead of depending on its garbage-collection finalizer.
            completed_snapshots.append(temporary_metadata)
            return temporary_metadata, ()

        monkeypatch.setattr(
            adapter_base,
            "_isolated_reask_git_metadata_volume_binds",
            create_snapshot_after_cancellation,
        )
        adapter = CodexAdapter(runner=FakeCommandRunner())
        run_task = asyncio.create_task(
            adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                isolated_worktree_host_path=tmp_path / "reask",
                isolated_worktree_ref="a" * 40,
                isolated_worktree_source_mirror=tmp_path / "source-mirror",
            )
        )

        try:
            await asyncio.wait_for(asyncio.to_thread(snapshot_started.wait), timeout=0.2)
            run_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run_task
        finally:
            release_snapshot.set()

        for _ in range(20):
            if completed_snapshots:
                break
            await asyncio.sleep(0.01)
        assert completed_snapshots
        snapshot_path = Path(completed_snapshots[0].name)
        for _ in range(20):
            if not snapshot_path.exists():
                break
            await asyncio.sleep(0.01)
        try:
            assert not snapshot_path.exists()
        finally:
            completed_snapshots[0].cleanup()

    @pytest.mark.unit
    async def test_isolated_reask_mounts_credential_free_git_metadata_read_only(
        self, tmp_path: Path
    ) -> None:
        """Non-Codex re-asks see only self-contained Git metadata snapshots."""
        runner = FakeCommandRunner()
        adapter = OpenCodeAdapter(runner=runner)
        mirror_path, worktree_path, head_oid, _unrelated_oid = _linked_reask_worktree(tmp_path)
        linked_git_dir = mirror_path / "worktrees" / worktree_path.name
        (mirror_path / "config").write_text(
            '[remote "origin"]\n\turl = https://token@github.example/repo.git\n',
            encoding="utf-8",
        )

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
            isolated_worktree_host_path=worktree_path,
            isolated_worktree_ref=head_oid,
            isolated_worktree_source_mirror=mirror_path,
        )

        args = runner.calls[0].args
        assert f"{mirror_path}:{mirror_path}:ro" not in args
        assert any(
            value.endswith(f":{linked_git_dir}:ro") and not value.startswith(f"{linked_git_dir}:")
            for value in args
        )
        assert not any(value.endswith(f":{mirror_path / 'objects'}:ro") for value in args)
        assert not any(value.endswith(f":{mirror_path / 'refs'}:ro") for value in args)
        assert any(value.endswith(":/awf-clarification-git-common:ro") for value in args)
        assert not any(
            value.endswith(f":{DEFAULT_AGENT_WORKDIR}/.awf-clarification-git-common:ro")
            for value in args
        )

    def test_isolated_reask_git_metadata_binds_exclude_linked_git_config(
        self, tmp_path: Path
    ) -> None:
        """The snapshot excludes linked config and its clone remote."""
        mirror_path, worktree_path, head_oid, unrelated_oid = _linked_reask_worktree(tmp_path)
        work_root = tmp_path / "awf-work"
        linked_git_dir = mirror_path / "worktrees" / worktree_path.name
        (linked_git_dir / "config.worktree").write_text(
            '[remote "origin"]\n\turl = https://token@github.example/repo.git\n',
            encoding="utf-8",
        )

        temporary_metadata, binds = adapter_base._isolated_reask_git_metadata_volume_binds(
            worktree_path,
            expected_ref=head_oid,
            expected_source_mirror=mirror_path,
        )

        assert temporary_metadata is not None
        try:
            assert Path(temporary_metadata.name).parent == work_root
            assert Path(temporary_metadata.name).name.startswith(
                f".awf-clarification-git-{worktree_path.name}--"
            )
            snapshot_path = Path(temporary_metadata.name) / "linked-git"
            assert {path.name for path in snapshot_path.iterdir()} == {
                "HEAD",
                "commondir",
                "gitdir",
                "index",
            }
            assert (snapshot_path / "HEAD").read_text(encoding="utf-8") == f"{head_oid}\n"
            assert (snapshot_path / "commondir").read_text(
                encoding="utf-8"
            ) == "/awf-clarification-git-common\n"
            assert (snapshot_path / "gitdir").read_text(encoding="utf-8") == "/workspace/.git\n"
            common_path = Path(temporary_metadata.name) / "common-git"
            config_path = common_path / "config"
            assert config_path.exists()
            assert (
                subprocess.run(
                    [
                        "git",
                        "config",
                        "--file",
                        str(config_path),
                        "--get-regexp",
                        r"^remote\\..*\\.url$",
                    ],
                    cwd=tmp_path,
                    check=False,
                    capture_output=True,
                    text=True,
                ).returncode
                == 1
            )
            assert (
                _git(
                    ["--git-dir", str(common_path), "cat-file", "-e", head_oid], tmp_path
                ).returncode
                == 0
            )
            assert (
                subprocess.run(
                    ["git", "--git-dir", str(common_path), "cat-file", "-e", unrelated_oid],
                    cwd=tmp_path,
                    check=False,
                    capture_output=True,
                    text=True,
                ).returncode
                == 1
            )
            assert binds[0] == (snapshot_path, str(linked_git_dir))
            assert binds[1:] == (
                (
                    common_path,
                    "/awf-clarification-git-common",
                ),
            )
        finally:
            temporary_metadata.cleanup()

    def test_isolated_reask_git_metadata_binds_preserve_sha256_format(self, tmp_path: Path) -> None:
        """A credential-free snapshot remains usable for SHA-256 repositories."""
        mirror_path, worktree_path, head_oid, _unrelated_oid = _linked_reask_worktree(
            tmp_path, object_format="sha256"
        )

        temporary_metadata, _binds = adapter_base._isolated_reask_git_metadata_volume_binds(
            worktree_path,
            expected_ref=head_oid,
            expected_source_mirror=mirror_path,
        )

        assert temporary_metadata is not None
        try:
            common_path = Path(temporary_metadata.name) / "common-git"
            config_path = common_path / "config"
            assert (
                _git(["--git-dir", str(common_path), "rev-parse", "HEAD"], tmp_path).stdout.strip()
                == head_oid
            )
            assert (
                _git(
                    ["config", "--file", str(config_path), "--get", "core.repositoryformatversion"],
                    tmp_path,
                ).stdout.strip()
                == "1"
            )
            assert (
                _git(
                    ["config", "--file", str(config_path), "--get", "extensions.objectformat"],
                    tmp_path,
                ).stdout.strip()
                == "sha256"
            )
            assert (
                subprocess.run(
                    [
                        "git",
                        "config",
                        "--file",
                        str(config_path),
                        "--get-regexp",
                        r"^remote\\..*\\.url$",
                    ],
                    cwd=tmp_path,
                    check=False,
                    capture_output=True,
                    text=True,
                ).returncode
                == 1
            )
        finally:
            temporary_metadata.cleanup()

    def test_isolated_reask_git_metadata_binds_preserve_shallow_boundary(
        self, tmp_path: Path
    ) -> None:
        """A source mirror's shallow boundary remains available to the snapshot clone."""
        mirror_path, worktree_path, head_oid, _unrelated_oid = _linked_reask_worktree(
            tmp_path,
            shallow=True,
        )

        temporary_metadata, _binds = adapter_base._isolated_reask_git_metadata_volume_binds(
            worktree_path,
            expected_ref=head_oid,
            expected_source_mirror=mirror_path,
        )

        assert temporary_metadata is not None
        try:
            common_path = Path(temporary_metadata.name) / "common-git"
            assert (
                _git(["--git-dir", str(common_path), "rev-parse", "HEAD"], tmp_path).stdout.strip()
                == head_oid
            )
        finally:
            temporary_metadata.cleanup()

    def test_isolated_reask_git_metadata_binds_snapshot_controls_before_clone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A control-file race cannot replace the bare clone's source repository."""
        mirror_path, worktree_path, head_oid, _unrelated_oid = _linked_reask_worktree(tmp_path)
        linked_git_dir = mirror_path / "worktrees" / worktree_path.name
        alternate_origin = tmp_path / "alternate-origin"
        alternate_origin.mkdir()
        _git(["init", "-q", "-b", "alternate"], alternate_origin)
        _git(["config", "user.name", "AWF Test"], alternate_origin)
        _git(["config", "user.email", "awf@test.local"], alternate_origin)
        (alternate_origin / "private.txt").write_text("must stay private\n", encoding="utf-8")
        _git(["add", "private.txt"], alternate_origin)
        _git(["commit", "-q", "-m", "alternate"], alternate_origin)
        alternate_oid = _git(["rev-parse", "HEAD"], alternate_origin).stdout.strip()
        alternate_mirror = tmp_path / "alternate-mirror.git"
        _git(["clone", "--mirror", str(alternate_origin), str(alternate_mirror)], tmp_path)
        real_run = base_isolated_reask.subprocess.run

        def _replace_controls_before_clone(command: list[str], *args: Any, **kwargs: Any) -> Any:
            if command[:2] == ["git", "clone"]:
                (linked_git_dir / "commondir").write_text(f"{alternate_mirror}\n", encoding="utf-8")
                (linked_git_dir / "HEAD").write_text(
                    "ref: refs/heads/alternate\n", encoding="utf-8"
                )
            return real_run(command, *args, **kwargs)

        monkeypatch.setattr(base_isolated_reask.subprocess, "run", _replace_controls_before_clone)

        temporary_metadata, _binds = adapter_base._isolated_reask_git_metadata_volume_binds(
            worktree_path,
            expected_ref=head_oid,
            expected_source_mirror=mirror_path,
        )

        assert temporary_metadata is not None
        try:
            common_path = Path(temporary_metadata.name) / "common-git"
            assert (
                _git(["--git-dir", str(common_path), "rev-parse", "HEAD"], tmp_path).stdout.strip()
                == head_oid
            )
            assert (
                subprocess.run(
                    ["git", "--git-dir", str(common_path), "cat-file", "-e", alternate_oid],
                    cwd=tmp_path,
                    check=False,
                    capture_output=True,
                    text=True,
                ).returncode
                == 1
            )
        finally:
            temporary_metadata.cleanup()

    def test_isolated_reask_git_metadata_binds_excludes_alternates_added_before_snapshot_clone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A raced source alternate cannot supply objects to the snapshot clone."""
        mirror_path, worktree_path, _head_oid, _unrelated_oid = _linked_reask_worktree(tmp_path)
        alternate_origin = tmp_path / "alternate-origin"
        alternate_origin.mkdir()
        _git(["init", "-q", "-b", "alternate"], alternate_origin)
        _git(["config", "user.name", "AWF Test"], alternate_origin)
        _git(["config", "user.email", "awf@test.local"], alternate_origin)
        (alternate_origin / "private.txt").write_text(
            "separate host private object\n", encoding="utf-8"
        )
        _git(["add", "private.txt"], alternate_origin)
        _git(["commit", "-q", "-m", "alternate"], alternate_origin)
        alternate_blob = _git(["rev-parse", "HEAD:private.txt"], alternate_origin).stdout.strip()
        alternate_mirror = tmp_path / "alternate-mirror.git"
        _git(["clone", "--mirror", str(alternate_origin), str(alternate_mirror)], tmp_path)
        forged_tree = _git(
            ["--git-dir", str(mirror_path), "mktree", "--missing"],
            tmp_path,
            input=f"100644 blob {alternate_blob}\tREADME.md\n",
        ).stdout.strip()
        forged_head = _git(
            [
                "-c",
                "user.email=awf@example.com",
                "-c",
                "user.name=AWF Test",
                "--git-dir",
                str(mirror_path),
                "commit-tree",
                forged_tree,
            ],
            tmp_path,
        ).stdout.strip()
        _git(
            ["--git-dir", str(mirror_path), "update-ref", "refs/heads/reask", forged_head], tmp_path
        )
        real_run = base_isolated_reask.subprocess.run

        def _add_alternates_before_snapshot_clone(
            command: list[str], *args: Any, **kwargs: Any
        ) -> Any:
            if command[-2:] == ["rev-parse", "HEAD"]:
                alternates_path = mirror_path / "objects" / "info" / "alternates"
                alternates_path.parent.mkdir(parents=True, exist_ok=True)
                alternates_path.write_text(f"{alternate_mirror / 'objects'}\n", encoding="utf-8")
            return real_run(command, *args, **kwargs)

        monkeypatch.setattr(
            base_isolated_reask.subprocess,
            "run",
            _add_alternates_before_snapshot_clone,
        )

        temporary_metadata, binds = adapter_base._isolated_reask_git_metadata_volume_binds(
            worktree_path,
            expected_ref=forged_head,
            expected_source_mirror=mirror_path,
        )

        assert temporary_metadata is None
        assert binds == ()

    def test_isolated_reask_git_metadata_binds_rejects_head_other_than_requested_ref(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A changed linked-worktree HEAD cannot redirect a re-ask snapshot."""
        mirror_path, worktree_path, head_oid, unrelated_oid = _linked_reask_worktree(tmp_path)
        linked_git_dir = mirror_path / "worktrees" / worktree_path.name
        (linked_git_dir / "HEAD").write_text(f"{unrelated_oid}\n", encoding="utf-8")
        real_run = base_isolated_reask.subprocess.run

        def _fail_if_clone(command: list[str], *args: Any, **kwargs: Any) -> Any:
            if command[:2] == ["git", "clone"]:
                raise AssertionError("must reject a HEAD other than the requested re-ask ref")
            return real_run(command, *args, **kwargs)

        monkeypatch.setattr(base_isolated_reask.subprocess, "run", _fail_if_clone)

        temporary_metadata, binds = adapter_base._isolated_reask_git_metadata_volume_binds(
            worktree_path,
            expected_ref=head_oid,
            expected_source_mirror=mirror_path,
        )

        assert temporary_metadata is None
        assert binds == ()

    @pytest.mark.parametrize("commondir", ("", "../"))
    def test_isolated_reask_git_metadata_binds_rejects_unexpected_commondir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, commondir: str
    ) -> None:
        """A malformed common-directory control file prevents Git from cloning."""
        mirror_path, worktree_path, head_oid, _unrelated_oid = _linked_reask_worktree(tmp_path)
        linked_git_dir = mirror_path / "worktrees" / worktree_path.name
        (linked_git_dir / "commondir").write_text(f"{commondir}\n", encoding="utf-8")
        real_run = base_isolated_reask.subprocess.run

        def _fail_if_clone(command: list[str], *args: Any, **kwargs: Any) -> Any:
            if command[:2] == ["git", "clone"]:
                raise AssertionError("must reject an unexpected commondir before cloning")
            return real_run(command, *args, **kwargs)

        monkeypatch.setattr(base_isolated_reask.subprocess, "run", _fail_if_clone)

        temporary_metadata, binds = adapter_base._isolated_reask_git_metadata_volume_binds(
            worktree_path,
            expected_ref=head_oid,
            expected_source_mirror=mirror_path,
        )

        assert temporary_metadata is None
        assert binds == ()

    def test_isolated_reask_git_metadata_binds_reject_raced_symlinked_head(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Snapshotting refuses a HEAD symlink installed after opening its directory."""
        mirror_path, worktree_path, head_oid, _unrelated_oid = _linked_reask_worktree(tmp_path)
        linked_git_dir = mirror_path / "worktrees" / worktree_path.name
        replacement_head = tmp_path / "replacement-head"
        replacement_head.write_text("ref: refs/heads/reask\n", encoding="utf-8")
        real_copy = base_isolated_reask._copy_regular_git_metadata_file_from_directory_fd

        def _race_head_to_symlink(source_dir_fd: int, source_name: str, destination: Path) -> None:
            if source_name == "HEAD":
                (linked_git_dir / "HEAD").unlink()
                (linked_git_dir / "HEAD").symlink_to(replacement_head)
            real_copy(source_dir_fd, source_name, destination)

        monkeypatch.setattr(
            base_isolated_reask,
            "_copy_regular_git_metadata_file_from_directory_fd",
            _race_head_to_symlink,
        )

        temporary_metadata, binds = adapter_base._isolated_reask_git_metadata_volume_binds(
            worktree_path,
            expected_ref=head_oid,
            expected_source_mirror=mirror_path,
        )

        try:
            assert temporary_metadata is None
            assert binds == ()
        finally:
            if temporary_metadata is not None:
                temporary_metadata.cleanup()

    def test_isolated_reask_linked_worktree_git_dir_preserves_relative_symlink_path(
        self, tmp_path: Path
    ) -> None:
        """A relative Git pointer is normalized without following a linked-dir symlink."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        linked_git_dir = tmp_path / "linked-git"
        replacement_git_dir = tmp_path / "replacement-linked-git"
        replacement_git_dir.mkdir()
        linked_git_dir.symlink_to(replacement_git_dir, target_is_directory=True)
        (worktree_path / ".git").write_text("gitdir: ../linked-git\n", encoding="utf-8")

        assert (
            base_isolated_reask._isolated_reask_linked_worktree_git_dir(worktree_path)
            == linked_git_dir
        )

    def test_isolated_reask_git_metadata_binds_reject_symlinked_linked_git_dir_before_clone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A linked-admin-directory symlink cannot become the clone source."""
        mirror_path, worktree_path, head_oid, _unrelated_oid = _linked_reask_worktree(tmp_path)
        linked_git_dir = mirror_path / "worktrees" / worktree_path.name
        displaced_git_dir = tmp_path / "displaced-linked-git"
        replacement_mirror = tmp_path / "replacement-mirror.git"
        shutil.copytree(mirror_path, replacement_mirror)
        replacement_git_dir = replacement_mirror / "worktrees" / worktree_path.name
        linked_git_dir.rename(displaced_git_dir)
        linked_git_dir.symlink_to(replacement_git_dir, target_is_directory=True)
        real_run = base_isolated_reask.subprocess.run

        def _fail_if_clone(command: list[str], *args: Any, **kwargs: Any) -> Any:
            if command[:2] == ["git", "clone"]:
                raise AssertionError("must reject linked Git directory before cloning")
            return real_run(command, *args, **kwargs)

        monkeypatch.setattr(base_isolated_reask.subprocess, "run", _fail_if_clone)

        temporary_metadata, binds = adapter_base._isolated_reask_git_metadata_volume_binds(
            worktree_path,
            expected_ref=head_oid,
            expected_source_mirror=mirror_path,
        )

        assert temporary_metadata is None
        assert binds == ()

    def test_isolated_reask_git_metadata_binds_rejects_foreign_regular_linked_git_dir(
        self, tmp_path: Path
    ) -> None:
        """A regular Git pointer cannot select another mirror with the same HEAD."""
        mirror_path, worktree_path, head_oid, _unrelated_oid = _linked_reask_worktree(tmp_path)
        foreign_mirror = tmp_path / "foreign-mirror.git"
        _git(["clone", "--mirror", str(mirror_path), str(foreign_mirror)], tmp_path)
        foreign_worktree = tmp_path / "foreign-worktree"
        _git(
            [
                "--git-dir",
                str(foreign_mirror),
                "worktree",
                "add",
                "-q",
                "-b",
                "foreign-reask",
                str(foreign_worktree),
                head_oid,
            ],
            tmp_path,
        )
        foreign_git_dir = foreign_mirror / "worktrees" / foreign_worktree.name
        (worktree_path / ".git").write_text(f"gitdir: {foreign_git_dir}\n", encoding="utf-8")

        temporary_metadata, binds = adapter_base._isolated_reask_git_metadata_volume_binds(
            worktree_path,
            expected_ref=head_oid,
            expected_source_mirror=mirror_path,
        )

        assert temporary_metadata is None
        assert binds == ()

    def test_isolated_reask_git_metadata_binds_rejects_unexpected_source_worktree_entry(
        self, tmp_path: Path
    ) -> None:
        """A source-mirror entry for another worktree cannot supply a snapshot."""
        mirror_path, worktree_path, head_oid, _unrelated_oid = _linked_reask_worktree(tmp_path)
        other_worktree = worktree_path.with_name("other-reask")
        _git(
            [
                "--git-dir",
                str(mirror_path),
                "worktree",
                "add",
                "-q",
                "-b",
                "other-reask",
                str(other_worktree),
                head_oid,
            ],
            tmp_path,
        )
        other_git_dir = mirror_path / "worktrees" / other_worktree.name
        (worktree_path / ".git").write_text(f"gitdir: {other_git_dir}\n", encoding="utf-8")

        temporary_metadata, binds = adapter_base._isolated_reask_git_metadata_volume_binds(
            worktree_path,
            expected_ref=head_oid,
            expected_source_mirror=mirror_path,
        )

        assert temporary_metadata is None
        assert binds == ()

    def test_isolated_reask_git_metadata_binds_rejects_oversized_worktree_gitfile(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A sparse worktree Git pointer cannot trigger a path-based read."""
        mirror_path, worktree_path, head_oid, _unrelated_oid = _linked_reask_worktree(tmp_path)
        git_file = worktree_path / ".git"
        git_file.write_text("gitdir: ", encoding="utf-8")
        with git_file.open("r+b") as source_file:
            source_file.truncate(base_isolated_reask._MAX_ISOLATED_REASK_GIT_METADATA_BYTES + 1)
        original_read_text = Path.read_text

        def _fail_path_based_read(path: Path, *args: object, **kwargs: object) -> str:
            if path == git_file:
                raise AssertionError("the worktree Git pointer must not be read by path")
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _fail_path_based_read)

        temporary_metadata, binds = adapter_base._isolated_reask_git_metadata_volume_binds(
            worktree_path,
            expected_ref=head_oid,
            expected_source_mirror=mirror_path,
        )

        assert temporary_metadata is None
        assert binds == ()

    def test_isolated_reask_git_metadata_binds_rejects_raced_fifo_worktree_gitfile(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Replacing `.git` before its descriptor open cannot block the snapshot."""
        mirror_path, worktree_path, head_oid, _unrelated_oid = _linked_reask_worktree(tmp_path)
        git_file = worktree_path / ".git"
        real_open = base_isolated_reask.os.open
        replaced_git_file = False

        def _replace_gitfile_before_open(
            path: os.PathLike[str] | str,
            flags: int,
            *args: object,
            **kwargs: object,
        ) -> int:
            nonlocal replaced_git_file
            if path == ".git" and kwargs.get("dir_fd") is not None:
                git_file.unlink()
                os.mkfifo(git_file)
                replaced_git_file = True
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(base_isolated_reask.os, "open", _replace_gitfile_before_open)

        temporary_metadata, binds = adapter_base._isolated_reask_git_metadata_volume_binds(
            worktree_path,
            expected_ref=head_oid,
            expected_source_mirror=mirror_path,
        )

        assert replaced_git_file
        assert temporary_metadata is None
        assert binds == ()

    def test_copy_regular_git_metadata_file_rejects_fifo(self, tmp_path: Path) -> None:
        """The metadata copy helper never reads a special file."""
        source_dir = tmp_path / "linked-git"
        source_dir.mkdir()
        os.mkfifo(source_dir / "HEAD")
        destination = tmp_path / "snapshot" / "HEAD"
        destination.parent.mkdir()

        with pytest.raises(OSError, match="not a regular file"):
            base_isolated_reask._copy_regular_git_metadata_file(source_dir, "HEAD", destination)

        assert not destination.exists()

    def test_copy_regular_git_metadata_file_rejects_oversized_sparse_file(
        self, tmp_path: Path
    ) -> None:
        """The metadata copy helper rejects oversized sparse files before copying."""
        source_dir = tmp_path / "linked-git"
        source_dir.mkdir()
        source = source_dir / "HEAD"
        source.touch()
        with source.open("r+b") as source_file:
            source_file.truncate(base_isolated_reask._MAX_ISOLATED_REASK_GIT_METADATA_BYTES + 1)
        destination = tmp_path / "snapshot" / "HEAD"
        destination.parent.mkdir()

        with pytest.raises(OSError, match="exceeds size limit"):
            base_isolated_reask._copy_regular_git_metadata_file(source_dir, "HEAD", destination)

        assert not destination.exists()

    def test_copy_regular_git_metadata_file_rejects_file_that_grows_after_stat(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The metadata copy helper enforces its cap while reading a raced file."""
        source_dir = tmp_path / "linked-git"
        source_dir.mkdir()
        source = source_dir / "HEAD"
        source.write_bytes(b"safe")
        destination = tmp_path / "snapshot" / "HEAD"
        destination.parent.mkdir()
        real_fstat = base_isolated_reask.os.fstat
        grew_source = False

        def _grow_source_after_stat(fd: int) -> os.stat_result:
            nonlocal grew_source
            file_stat = real_fstat(fd)
            if not grew_source and stat.S_ISREG(file_stat.st_mode):
                with source.open("r+b") as source_file:
                    source_file.truncate(
                        base_isolated_reask._MAX_ISOLATED_REASK_GIT_METADATA_BYTES + 1
                    )
                grew_source = True
            return file_stat

        monkeypatch.setattr(base_isolated_reask.os, "fstat", _grow_source_after_stat)

        with pytest.raises(OSError, match="exceeds size limit"):
            base_isolated_reask._copy_regular_git_metadata_file(source_dir, "HEAD", destination)

        assert grew_source
        assert not destination.exists()

    def test_copy_git_object_directory_rejects_oversized_object_file(self, tmp_path: Path) -> None:
        """An oversized loose or pack object cannot fill a re-ask snapshot."""
        source = tmp_path / "objects"
        object_file = source / "pack" / "pack-too-large.pack"
        object_file.parent.mkdir(parents=True)
        object_file.touch()
        with object_file.open("r+b") as object_stream:
            object_stream.truncate(
                base_isolated_reask._MAX_ISOLATED_REASK_GIT_OBJECT_FILE_BYTES + 1
            )
        destination = tmp_path / "snapshot" / "objects"
        destination.parent.mkdir()

        with pytest.raises(OSError, match="exceeds size limit"):
            base_isolated_reask._copy_git_object_directory(source, destination)

        assert not destination.exists()

    def test_copy_git_object_directory_rejects_total_size_overflow(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Several normal-sized objects cannot exceed the snapshot's total budget."""
        source = tmp_path / "objects"
        (source / "aa").mkdir(parents=True)
        (source / "aa" / "first").write_bytes(b"ab")
        (source / "bb").mkdir()
        (source / "bb" / "second").write_bytes(b"cd")
        destination = tmp_path / "snapshot" / "objects"
        destination.parent.mkdir()
        monkeypatch.setattr(
            base_isolated_reask,
            "_MAX_ISOLATED_REASK_GIT_OBJECT_SNAPSHOT_BYTES",
            3,
            raising=False,
        )

        with pytest.raises(OSError, match="total size limit"):
            base_isolated_reask._copy_git_object_directory(source, destination)

        assert not destination.exists()

    def test_copy_git_object_directory_rejects_entry_overflow(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Zero-byte objects cannot exhaust snapshot inodes without using bytes."""
        source = tmp_path / "objects"
        pack_dir = source / "pack"
        pack_dir.mkdir(parents=True)
        (pack_dir / "empty-one.pack").touch()
        (pack_dir / "empty-two.pack").touch()
        destination = tmp_path / "snapshot" / "objects"
        destination.parent.mkdir()
        monkeypatch.setattr(
            base_isolated_reask,
            "_MAX_ISOLATED_REASK_GIT_OBJECT_SNAPSHOT_ENTRIES",
            2,
            raising=False,
        )

        with pytest.raises(OSError, match="entry limit"):
            base_isolated_reask._copy_git_object_directory(source, destination)

        assert not destination.exists()

    def test_copy_git_object_directory_rejects_excessive_directory_depth(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Nested directories cannot prolong a re-ask object snapshot indefinitely."""
        source = tmp_path / "objects"
        (source / "first" / "second").mkdir(parents=True)
        destination = tmp_path / "snapshot" / "objects"
        destination.parent.mkdir()
        monkeypatch.setattr(
            base_isolated_reask,
            "_MAX_ISOLATED_REASK_GIT_OBJECT_DIRECTORY_DEPTH",
            1,
            raising=False,
        )

        with pytest.raises(OSError, match="directory depth limit"):
            base_isolated_reask._copy_git_object_directory(source, destination)

        assert not destination.exists()

    def test_isolated_reask_git_metadata_binds_preserve_large_indexes(self, tmp_path: Path) -> None:
        """Large normal and split Git indexes remain usable in the snapshot."""
        mirror_path, worktree_path, head_oid, _unrelated_oid = _linked_reask_worktree(tmp_path)
        blob_oid = _git(
            ["hash-object", "-w", "--stdin"], worktree_path, input="payload\n"
        ).stdout.strip()
        index_info = "".join(
            f"100644 {blob_oid}\tmetadata/large-index-entry-{index:05d}.txt\n"
            for index in range(16_000)
        )
        _git(["update-index", "--index-info"], worktree_path, input=index_info)

        linked_git_dir = mirror_path / "worktrees" / worktree_path.name
        assert (linked_git_dir / "index").stat().st_size > (
            base_isolated_reask._MAX_ISOLATED_REASK_GIT_METADATA_BYTES
        )

        temporary_metadata, _binds = adapter_base._isolated_reask_git_metadata_volume_binds(
            worktree_path,
            expected_ref=head_oid,
            expected_source_mirror=mirror_path,
        )

        assert temporary_metadata is not None
        try:
            _assert_snapshot_index_is_usable(
                temporary_metadata,
                worktree_path=worktree_path,
                source_index_path=linked_git_dir / "index",
            )
        finally:
            temporary_metadata.cleanup()

        _git(["update-index", "--split-index"], worktree_path)
        shared_index_path = (
            worktree_path / _git(["rev-parse", "--shared-index-path"], worktree_path).stdout.strip()
        ).resolve()
        assert (
            shared_index_path.stat().st_size
            > base_isolated_reask._MAX_ISOLATED_REASK_GIT_METADATA_BYTES
        )

        temporary_metadata, _binds = adapter_base._isolated_reask_git_metadata_volume_binds(
            worktree_path,
            expected_ref=head_oid,
            expected_source_mirror=mirror_path,
        )

        assert temporary_metadata is not None
        try:
            _assert_snapshot_index_is_usable(
                temporary_metadata,
                worktree_path=worktree_path,
                source_index_path=shared_index_path,
            )
        finally:
            temporary_metadata.cleanup()

    def test_isolated_reask_git_metadata_binds_copy_split_index_backing_file(
        self, tmp_path: Path
    ) -> None:
        """A split index snapshot remains usable by Git in the clarification worktree."""
        mirror_path, worktree_path, head_oid, _unrelated_oid = _linked_reask_worktree(tmp_path)
        _git(["update-index", "--split-index"], worktree_path)
        shared_index_path = (
            worktree_path / _git(["rev-parse", "--shared-index-path"], worktree_path).stdout.strip()
        ).resolve()
        assert shared_index_path.is_file()

        temporary_metadata, _binds = adapter_base._isolated_reask_git_metadata_volume_binds(
            worktree_path,
            expected_ref=head_oid,
            expected_source_mirror=mirror_path,
        )

        assert temporary_metadata is not None
        try:
            snapshot_path = Path(temporary_metadata.name) / "linked-git"
            snapshot_shared_index_path = snapshot_path / shared_index_path.name
            assert snapshot_shared_index_path.read_bytes() == shared_index_path.read_bytes()

            common_path = Path(temporary_metadata.name) / "common-git"
            (snapshot_path / "commondir").write_text(f"{common_path}\n", encoding="utf-8")
            inspection_worktree = tmp_path / "inspection-worktree"
            inspection_worktree.mkdir()
            (inspection_worktree / ".git").write_text(
                f"gitdir: {snapshot_path}\n", encoding="utf-8"
            )
            status = subprocess.run(
                ["git", "-C", str(inspection_worktree), "status", "--porcelain"],
                check=False,
                capture_output=True,
                text=True,
            )
            assert status.returncode == 0, status.stderr
        finally:
            temporary_metadata.cleanup()

    def test_isolated_reask_git_metadata_binds_disables_fsmonitor_for_shared_index_lookup(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Shared-index discovery cannot execute an agent-configured fsmonitor hook."""
        mirror_path, worktree_path, head_oid, _unrelated_oid = _linked_reask_worktree(tmp_path)
        _git(["update-index", "--split-index"], worktree_path)
        real_run = base_isolated_reask.subprocess.run
        shared_index_commands: list[list[str]] = []

        def _record_shared_index_lookup(command: list[str], *args: Any, **kwargs: Any) -> Any:
            if command[-2:] == ["rev-parse", "--shared-index-path"]:
                shared_index_commands.append(command)
            return real_run(command, *args, **kwargs)

        monkeypatch.setattr(base_isolated_reask.subprocess, "run", _record_shared_index_lookup)

        temporary_metadata, _binds = adapter_base._isolated_reask_git_metadata_volume_binds(
            worktree_path,
            expected_ref=head_oid,
            expected_source_mirror=mirror_path,
        )

        assert temporary_metadata is not None
        try:
            assert shared_index_commands == [
                [
                    "git",
                    "-C",
                    str(worktree_path),
                    "-c",
                    "core.fsmonitor=false",
                    "rev-parse",
                    "--shared-index-path",
                ]
            ]
        finally:
            temporary_metadata.cleanup()

    def test_isolated_reask_git_metadata_binds_keep_snapshot_when_shared_index_lookup_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Optional split-index discovery cannot discard a completed snapshot."""
        mirror_path, worktree_path, head_oid, _unrelated_oid = _linked_reask_worktree(tmp_path)
        real_run = base_isolated_reask.subprocess.run

        def _shared_index_lookup_failure(command: list[str], *args: Any, **kwargs: Any) -> Any:
            if command == [
                "git",
                "-C",
                str(worktree_path),
                "-c",
                "core.fsmonitor=false",
                "rev-parse",
                "--shared-index-path",
            ]:
                raise subprocess.CalledProcessError(1, command)
            return real_run(command, *args, **kwargs)

        monkeypatch.setattr(base_isolated_reask.subprocess, "run", _shared_index_lookup_failure)

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

    def test_isolated_reask_git_metadata_binds_skip_external_shared_index(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An external shared-index path cannot discard a completed snapshot."""
        mirror_path, worktree_path, head_oid, _unrelated_oid = _linked_reask_worktree(tmp_path)
        external_shared_index = tmp_path / "sharedindex.external"
        external_shared_index.write_bytes(b"external split-index backing file")
        real_run = base_isolated_reask.subprocess.run

        def _external_shared_index(command: list[str], *args: Any, **kwargs: Any) -> Any:
            if command == [
                "git",
                "-C",
                str(worktree_path),
                "-c",
                "core.fsmonitor=false",
                "rev-parse",
                "--shared-index-path",
            ]:
                return subprocess.CompletedProcess(command, 0, stdout=str(external_shared_index))
            return real_run(command, *args, **kwargs)

        monkeypatch.setattr(base_isolated_reask.subprocess, "run", _external_shared_index)

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

    @pytest.mark.unit
    async def test_isolated_reask_reconciles_interrupted_legacy_model_migration_once(
        self, tmp_path: Path
    ) -> None:
        """A retry attaches an existing sidecar before recording reconciliation."""
        compose_file = _write_legacy_opencode_ollama_compose(tmp_path)
        assert adapter_base.upgrade_persisted_clarification_service(
            compose_file=compose_file,
            workspace_id="ws_legacy",
            agent_runtime=AgentRuntime.opencode,
            agent_model="ollama/kimi-k2.6:cloud",
        ) == ("ollama-sidecar",)

        runner = FakeCommandRunner()
        runner.queue_result()
        runner.queue_result(stdout="stateful-model-container\n")
        runner.queue_result()
        adapter = OpenCodeAdapter(runner=runner)

        await adapter.run(
            compose_project="awf_ws_legacy",
            compose_file=compose_file,
            prompt=_PROMPT,
            model="ollama/kimi-k2.6:cloud",
            workspace_id="ws_legacy",
            isolated_worktree_host_path=tmp_path / "reask",
        )

        assert runner.calls[2].args == [
            "docker",
            "network",
            "connect",
            "--alias",
            "ollama-sidecar",
            "awf-ws_legacy-clarification-model-net",
            "stateful-model-container",
        ]
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

        assert len(runner.calls) == 5
        assert "run" in runner.calls[-1].args

    @pytest.mark.unit
    async def test_isolated_reask_retries_existing_network_after_marker_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed marker retries with a service alias without replacing the sidecar."""
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
        runner.queue_result()
        runner.queue_result(stdout="stateful-model-container\n")
        runner.queue_result()
        runner.queue_result()
        runner.queue_result(stdout="stateful-model-container\n")
        runner.queue_result(stdout="stateful-model-container\n")
        runner.queue_result(
            returncode=1,
            stderr="endpoint with name stateful-model-container already exists in network",
        )
        adapter = OpenCodeAdapter(runner=runner)

        for _ in range(2):
            await adapter.run(
                compose_project="awf_ws_legacy",
                compose_file=compose_file,
                prompt=_PROMPT,
                model="ollama/kimi-k2.6:cloud",
                workspace_id="ws_legacy",
                isolated_worktree_host_path=tmp_path / "reask",
            )

        assert all("--force-recreate" not in call.args for call in runner.calls)
        assert "run" in runner.calls[3].args
        assert "run" in runner.calls[-1].args
        assert runner.calls[6].args == [
            "docker",
            "network",
            "connect",
            "--alias",
            "ollama-sidecar",
            "awf-ws_legacy-clarification-model-net",
            "stateful-model-container",
        ]
        assert runner.calls[7].args == [
            "docker",
            "network",
            "disconnect",
            "awf-ws_legacy-clarification-model-net",
            "stateful-model-container",
        ]
        assert runner.calls[8].args == [
            "docker",
            "network",
            "connect",
            "--alias",
            "ollama-sidecar",
            "awf-ws_legacy-clarification-model-net",
            "stateful-model-container",
        ]
        assert (
            yaml.safe_load(compose_file.read_text(encoding="utf-8"))[
                "x-awf-persisted-clarification-model-network-reconciled"
            ]
            is False
        )

    @pytest.mark.unit
    async def test_isolated_reask_restores_existing_endpoint_after_alias_reconciliation_fails(
        self, tmp_path: Path
    ) -> None:
        """A failed alias reattach never leaves the stateful sidecar disconnected."""
        compose_file = _write_legacy_opencode_ollama_compose(tmp_path)
        original_compose_file = compose_file.read_bytes()
        runner = FakeCommandRunner()
        runner.queue_result()
        runner.queue_result(stdout="stateful-model-container\n")
        runner.queue_result(
            returncode=1,
            stderr="endpoint with name stateful-model-container already exists in network",
        )
        runner.queue_result()
        runner.queue_result(returncode=1, stderr="could not attach service alias")
        runner.queue_result(
            returncode=1,
            stderr="endpoint with name stateful-model-container already exists in network",
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
        assert runner.calls[3].args == [
            "docker",
            "network",
            "disconnect",
            "awf-ws_legacy-clarification-model-net",
            "stateful-model-container",
        ]
        assert runner.calls[4].args == [
            "docker",
            "network",
            "connect",
            "--alias",
            "ollama-sidecar",
            "awf-ws_legacy-clarification-model-net",
            "stateful-model-container",
        ]
        assert runner.calls[5].args == runner.calls[4].args
        assert all("run" not in call.args for call in runner.calls)
        assert compose_file.read_bytes() == original_compose_file

    @pytest.mark.unit
    async def test_isolated_reask_cancellation_restores_existing_endpoint_alias(
        self, tmp_path: Path
    ) -> None:
        """Cancellation while detaching an existing endpoint restores its alias."""
        compose_file = _write_legacy_opencode_ollama_compose(tmp_path)

        class _CancellingDisconnectRunner(FakeCommandRunner):
            """Cancel immediately after Docker can have detached the endpoint."""

            async def run(self, args: list[str], **kwargs: Any) -> CommandResult:
                result = await super().run(args, **kwargs)
                if args[0:3] == ["docker", "network", "disconnect"]:
                    raise asyncio.CancelledError
                return result

        runner = _CancellingDisconnectRunner()
        runner.queue_result()
        runner.queue_result(stdout="stateful-model-container\n")
        runner.queue_result(
            returncode=1,
            stderr="endpoint with name stateful-model-container already exists in network",
        )
        runner.queue_result()
        runner.queue_result()
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

        assert runner.calls[4].args == [
            "docker",
            "network",
            "connect",
            "--alias",
            "ollama-sidecar",
            "awf-ws_legacy-clarification-model-net",
            "stateful-model-container",
        ]

    @pytest.mark.unit
    async def test_isolated_reask_cancellation_preserves_endpoint_existing_before_connect(
        self, tmp_path: Path
    ) -> None:
        """Cancellation after an uncertain connect keeps a pre-existing endpoint."""
        compose_file = _write_legacy_opencode_ollama_compose(tmp_path)

        class _CancellingFirstConnectRunner(FakeCommandRunner):
            """Cancel after Docker can have handled the initial connect request."""

            cancelled_connect = False

            async def run(self, args: list[str], **kwargs: Any) -> CommandResult:
                result = await super().run(args, **kwargs)
                if args[0:3] == ["docker", "network", "connect"] and not self.cancelled_connect:
                    self.cancelled_connect = True
                    raise asyncio.CancelledError
                return result

        runner = _CancellingFirstConnectRunner()
        runner.queue_result(stdout="stateful-model-container\n")
        runner.queue_result(stdout="stateful-model-container\n")
        runner.queue_result()
        runner.queue_result()
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

        assert runner.calls[3].args == [
            "docker",
            "network",
            "connect",
            "--alias",
            "ollama-sidecar",
            "awf-ws_legacy-clarification-model-net",
            "stateful-model-container",
        ]
        assert all("disconnect" not in call.args for call in runner.calls)

    @pytest.mark.unit
    async def test_isolated_reask_rolls_back_only_new_network_attachments(
        self, tmp_path: Path
    ) -> None:
        """A failed second attachment leaves stateful model containers untouched."""
        compose_file = _write_legacy_opencode_ollama_compose(tmp_path)
        original_compose_file = compose_file.read_bytes()
        runner = FakeCommandRunner()
        runner.queue_result(
            returncode=1,
            stderr="Error response from daemon: network awf-ws_legacy-clarification-model-net not found",
        )
        runner.queue_result()
        runner.queue_result(stdout="first-model-container\nsecond-model-container\n")
        runner.queue_result()
        runner.queue_result(returncode=1, stderr="could not attach second model")
        runner.queue_result()
        runner.queue_result(returncode=1, stderr="container is not connected to network")
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
        assert runner.calls[5].args == [
            "docker",
            "network",
            "disconnect",
            "awf-ws_legacy-clarification-model-net",
            "second-model-container",
        ]
        assert runner.calls[6].args == [
            "docker",
            "network",
            "disconnect",
            "awf-ws_legacy-clarification-model-net",
            "first-model-container",
        ]
        assert runner.calls[7].args == [
            "docker",
            "network",
            "rm",
            "awf-ws_legacy-clarification-model-net",
        ]
        assert all("rm" not in call.args or "compose" not in call.args for call in runner.calls)
        assert all("--force-recreate" not in call.args for call in runner.calls)
        assert compose_file.read_bytes() == original_compose_file

    @pytest.mark.unit
    async def test_isolated_reask_cancellation_detaches_the_existing_sidecar(
        self, tmp_path: Path
    ) -> None:
        """Cancellation after connect never requires model-sidecar recreation."""
        compose_file = _write_legacy_opencode_ollama_compose(tmp_path)
        original_compose_file = compose_file.read_bytes()

        class _CancellingConnectRunner(FakeCommandRunner):
            """Cancel immediately after Docker can have applied the connection."""

            async def run(self, args: list[str], **kwargs: Any) -> CommandResult:
                result = await super().run(args, **kwargs)
                if args[0:3] == ["docker", "network", "connect"]:
                    raise asyncio.CancelledError
                return result

        runner = _CancellingConnectRunner()
        runner.queue_result(
            returncode=1,
            stderr="Error response from daemon: network awf-ws_legacy-clarification-model-net not found",
        )
        runner.queue_result()
        runner.queue_result(stdout="stateful-model-container\n")
        runner.queue_result()
        runner.queue_result()
        runner.queue_result()
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

        assert runner.calls[4].args == [
            "docker",
            "network",
            "disconnect",
            "awf-ws_legacy-clarification-model-net",
            "stateful-model-container",
        ]
        assert runner.calls[5].args == [
            "docker",
            "network",
            "rm",
            "awf-ws_legacy-clarification-model-net",
        ]
        assert all("--force-recreate" not in call.args for call in runner.calls)
        assert compose_file.read_bytes() == original_compose_file

    @pytest.mark.unit
    async def test_isolated_reask_cancellation_removes_network_created_before_runner_returns(
        self, tmp_path: Path
    ) -> None:
        """Cancellation after network creation removes the possibly-created network."""
        compose_file = _write_legacy_opencode_ollama_compose(tmp_path)

        class _CancellingCreateRunner(FakeCommandRunner):
            """Cancel immediately after Docker can have created the network."""

            network_creation_marker: str | None = None

            async def run(self, args: list[str], **kwargs: Any) -> CommandResult:
                result = await super().run(args, **kwargs)
                if args[0:3] == ["docker", "network", "create"]:
                    self.network_creation_marker = next(
                        label.removeprefix("io.awf.clarification-network-creation=")
                        for label in args
                        if label.startswith("io.awf.clarification-network-creation=")
                    )
                    raise asyncio.CancelledError
                if (
                    args[0:3] == ["docker", "network", "inspect"]
                    and self.network_creation_marker is not None
                ):
                    return CommandResult(
                        returncode=result.returncode,
                        stdout=f"{self.network_creation_marker}\n",
                        stderr=result.stderr,
                        reason_code=result.reason_code,
                    )
                return result

        runner = _CancellingCreateRunner()
        runner.queue_result(
            returncode=1,
            stderr="Error response from daemon: network awf-ws_legacy-clarification-model-net not found",
        )
        runner.queue_result()
        runner.queue_result()
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

        assert runner.calls[2].args[0:3] == ["docker", "network", "inspect"]
        assert runner.calls[3].args == [
            "docker",
            "network",
            "rm",
            "awf-ws_legacy-clarification-model-net",
        ]
