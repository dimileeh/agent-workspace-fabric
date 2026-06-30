"""Failure-edge coverage for GitManager mirror hook repair."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf.node import git_manager as git_module


class TestRepairMirrorHooksPathFailureEdges:
    @pytest.mark.unit
    async def test_ignores_git_object_lookup_envs_for_config_repair(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify ignores git object lookup envs for config repair."""
        mirror = tmp_path / "mirror.git"
        mirror.mkdir()
        subprocess.run(
            ["git", "init", "--bare", str(mirror)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "--git-dir", str(mirror), "config", "core.hooksPath", "/dev/null"],
            check=True,
            capture_output=True,
        )
        monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "")
        monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "")

        result = await git_module.repair_mirror_hooks_path(mirror)

        assert result is True
        check = subprocess.run(
            ["git", "--git-dir", str(mirror), "config", "--local", "core.hooksPath"],
            capture_output=True,
            text=True,
        )
        assert check.returncode != 0

    @pytest.mark.unit
    async def test_noop_when_hooks_path_not_set(self, tmp_path: Path) -> None:
        """Verify noop when hooks path not set."""
        mirror = tmp_path / "mirror.git"
        mirror.mkdir()
        subprocess.run(
            ["git", "init", "--bare", str(mirror)],
            check=True,
            capture_output=True,
        )

        result = await git_module.repair_mirror_hooks_path(mirror)

        assert result is False

    @pytest.mark.unit
    async def test_repair_fails_when_poisoned_hooks_origin_is_unmapped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify repair fails when poisoned hooks origin is unmapped."""
        config_path = tmp_path / "mirror.git" / "config"
        config_path.parent.mkdir()
        probe_value = git_module._HooksPathConfigValue(  # noqa: SLF001
            hooks_path="/dev/null",
            origin_path=None,
        )

        async def _probe_hooks_path_config(**_kwargs: object) -> tuple[object, ...]:
            """Test helper for probe hooks path config."""
            return (probe_value,)

        monkeypatch.setattr(git_module, "_probe_hooks_path_config", _probe_hooks_path_config)

        with pytest.raises(git_module.GitOperationError) as raised:
            await git_module._repair_hooks_path_config(  # noqa: SLF001
                git_args=("--git-dir", str(config_path.parent)),
                config_scope_args=("--local",),
                config_path=config_path,
                operation_prefix="mirror",
            )

        assert raised.value.operation == "mirror.hooks_path_include_repair"
        assert raised.value.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
        assert raised.value.stdout == "/dev/null"
        assert "origin is not directly included" in raised.value.stderr

    @pytest.mark.unit
    async def test_repair_fails_when_include_path_probe_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify repair fails when include path probe fails."""
        config_path = tmp_path / "mirror.git" / "config"
        config_path.parent.mkdir()

        async def _run_git_config(**_kwargs: object) -> tuple[int, str, str]:
            """Test helper for run git config."""
            return 2, "", "config read failed"

        monkeypatch.setattr(git_module, "_run_git_config", _run_git_config)

        with pytest.raises(git_module.GitOperationError) as raised:
            await git_module._unset_matching_include_path(  # noqa: SLF001
                git_args=("--git-dir", str(config_path.parent)),
                config_scope_args=("--local",),
                config_path=config_path,
                included_origin=tmp_path / "included-hooks.conf",
                operation_prefix="mirror",
            )

        assert raised.value.operation == "mirror.hooks_path_include_probe"
        assert raised.value.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
        assert raised.value.returncode == 2
        assert raised.value.stderr == "config read failed"

    @pytest.mark.unit
    async def test_repair_fails_when_includeif_probe_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify repair fails when includeif probe fails."""
        config_path = tmp_path / "mirror.git" / "config"
        config_path.parent.mkdir()
        calls: list[tuple[str, ...]] = []

        async def _run_git_config(
            *, args: tuple[str, ...], **_kwargs: object
        ) -> tuple[int, str, str]:
            """Test helper for run git config."""
            calls.append(args)
            if args == ("--get-all", "include.path"):
                return 1, "", ""
            return 2, "", "includeIf probe failed"

        monkeypatch.setattr(git_module, "_run_git_config", _run_git_config)

        with pytest.raises(git_module.GitOperationError) as raised:
            await git_module._unset_matching_include_path(  # noqa: SLF001
                git_args=("--git-dir", str(config_path.parent)),
                config_scope_args=("--local",),
                config_path=config_path,
                included_origin=tmp_path / "included-hooks.conf",
                operation_prefix="mirror",
            )

        assert calls == [
            ("--get-all", "include.path"),
            ("--get-regexp", r"^includeIf\..*\.path$"),
        ]
        assert raised.value.operation == "mirror.hooks_path_include_probe"
        assert raised.value.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
        assert raised.value.returncode == 2
        assert raised.value.stderr == "includeIf probe failed"

    @pytest.mark.unit
    async def test_repair_ignores_malformed_includeif_probe_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify repair ignores malformed includeif probe line."""
        config_path = tmp_path / "mirror.git" / "config"
        config_path.parent.mkdir()

        async def _run_git_config(
            *, args: tuple[str, ...], **_kwargs: object
        ) -> tuple[int, str, str]:
            """Test helper for run git config."""
            if args == ("--get-all", "include.path"):
                return 1, "", ""
            if args == ("--get-regexp", r"^includeIf\..*\.path$"):
                return 0, "includeIf.gitdir:bad.path\n", ""
            raise AssertionError(f"unexpected git config args: {args!r}")

        monkeypatch.setattr(git_module, "_run_git_config", _run_git_config)

        removed = await git_module._unset_matching_include_path(  # noqa: SLF001
            git_args=("--git-dir", str(config_path.parent)),
            config_scope_args=("--local",),
            config_path=config_path,
            included_origin=tmp_path / "included-hooks.conf",
            operation_prefix="mirror",
        )

        assert removed is False

    @pytest.mark.unit
    async def test_repair_fails_when_matching_include_unset_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify repair fails when matching include unset fails."""
        config_path = tmp_path / "mirror.git" / "config"
        config_path.parent.mkdir()
        included_config = tmp_path / "included-hooks.conf"
        included_config.write_text("[core]\n\thooksPath = /dev/null\n", encoding="utf-8")

        async def _run_git_config(
            *, args: tuple[str, ...], **_kwargs: object
        ) -> tuple[int, str, str]:
            """Test helper for run git config."""
            if args == ("--get-all", "include.path"):
                return 0, f"not-it.conf\n{included_config}\n", ""
            if args == ("--get-regexp", r"^includeIf\..*\.path$"):
                return 1, "", ""
            assert args[0] == "--unset-all"
            return 2, "", "include unset failed"

        monkeypatch.setattr(git_module, "_run_git_config", _run_git_config)

        with pytest.raises(git_module.GitOperationError) as raised:
            await git_module._unset_matching_include_path(  # noqa: SLF001
                git_args=("--git-dir", str(config_path.parent)),
                config_scope_args=("--local",),
                config_path=config_path,
                included_origin=included_config,
                operation_prefix="mirror",
            )

        assert raised.value.operation == "mirror.hooks_path_include_repair"
        assert raised.value.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
        assert raised.value.returncode == 2
        assert raised.value.stderr == "include unset failed"

    @pytest.mark.unit
    async def test_repair_fails_when_hooks_path_unset_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify repair fails when hooks path unset fails."""
        config_path = tmp_path / "mirror.git" / "config"
        config_path.parent.mkdir()
        probe_value = git_module._HooksPathConfigValue(  # noqa: SLF001
            hooks_path="/dev/null",
            origin_path=config_path,
        )

        async def _probe_hooks_path_config(**_kwargs: object) -> tuple[object, ...]:
            """Test helper for probe hooks path config."""
            return (probe_value,)

        async def _run_git_config(**_kwargs: object) -> tuple[int, str, str]:
            """Test helper for run git config."""
            return 2, "", "hooksPath unset failed"

        monkeypatch.setattr(git_module, "_probe_hooks_path_config", _probe_hooks_path_config)
        monkeypatch.setattr(git_module, "_run_git_config", _run_git_config)

        with pytest.raises(git_module.GitOperationError) as raised:
            await git_module._repair_hooks_path_config(  # noqa: SLF001
                git_args=("--git-dir", str(config_path.parent)),
                config_scope_args=("--local",),
                config_path=config_path,
                operation_prefix="mirror",
            )

        assert raised.value.operation == "mirror.hooks_path_repair"
        assert raised.value.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
        assert raised.value.returncode == 2
        assert raised.value.stderr == "hooksPath unset failed"
