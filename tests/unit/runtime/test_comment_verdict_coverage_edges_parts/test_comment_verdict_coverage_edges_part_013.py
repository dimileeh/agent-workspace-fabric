"""Git index stage-map path-scoping and large-index residue regressions (part 13)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf.node.git_manager import git_env_without_object_lookup_overrides
from awf.runtime.pr_monitor_runner import comment_verdict_residue
from tests.unit.runtime.test_comment_verdict_coverage_edges_parts._helpers import (
    init_git_worktree,
)

_git_env = git_env_without_object_lookup_overrides


@pytest.mark.unit
def test_load_git_index_stage_map_fails_closed_on_entry_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("past deadline must not spawn ls-files")

    monkeypatch.setattr(comment_verdict_residue, "_popen_capped_nul_path_records", _boom)
    with comment_verdict_residue._residue_fingerprint_nested_scan_budget():
        holder = comment_verdict_residue._ORDINARY_FINGERPRINT_GIT_DEADLINE.get()
        assert holder is not None and holder.deadline is not None
        holder.deadline = 0.0
        assert (
            comment_verdict_residue._load_git_index_stage_map(
                worktree_path=tmp_path,
                git_env={},
                paths=("src/x.py",),
            )
            is None
        )


@pytest.mark.unit
def test_load_git_index_stage_map_batches_argv_path_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Large dirty path lists must be chunked to stay under ARG_MAX."""
    monkeypatch.setattr(comment_verdict_residue, "_INDEX_STAGE_LS_FILES_PATH_CHUNK", 2)
    calls: list[list[str]] = []

    def _popen(
        command: object,
        *,
        env: object,
        max_records: object,
        max_bytes: object,
        timeout: object,
    ) -> tuple[bytes, ...]:
        del env, max_records, max_bytes, timeout
        cmd = list(command)  # type: ignore[arg-type]
        chunk_paths = cmd[cmd.index("--") + 1 :]
        calls.append(chunk_paths)
        return tuple(
            b"100644 " + b"b" * 40 + b" 0\t" + path.encode("utf-8") for path in chunk_paths
        )

    monkeypatch.setattr(comment_verdict_residue, "_popen_capped_nul_path_records", _popen)
    result = comment_verdict_residue._load_git_index_stage_map(
        worktree_path=tmp_path,
        git_env={},
        paths=("a.py", "b.py", "c.py"),
    )
    assert calls == [["a.py", "b.py"], ["c.py"]]
    assert result == {
        "a.py": (("0", "100644", "b" * 40),),
        "b.py": (("0", "100644", "b" * 40),),
        "c.py": (("0", "100644", "b" * 40),),
    }


@pytest.mark.unit
def test_load_git_index_stage_map_fails_closed_on_mid_chunk_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(comment_verdict_residue, "_INDEX_STAGE_LS_FILES_PATH_CHUNK", 1)
    calls = {"n": 0}

    def _popen(
        command: object,
        *,
        env: object,
        max_records: object,
        max_bytes: object,
        timeout: object,
    ) -> tuple[bytes, ...]:
        del command, env, max_records, max_bytes, timeout
        calls["n"] += 1
        return (b"100644 " + b"c" * 40 + b" 0\ta.py",)

    monkeypatch.setattr(comment_verdict_residue, "_popen_capped_nul_path_records", _popen)
    with comment_verdict_residue._residue_fingerprint_nested_scan_budget():
        holder = comment_verdict_residue._ORDINARY_FINGERPRINT_GIT_DEADLINE.get()
        assert holder is not None and holder.deadline is not None

        def _past() -> bool:
            # Trip only after the first chunk so the in-loop deadline branch runs.
            return calls["n"] >= 1

        monkeypatch.setattr(
            comment_verdict_residue,
            "_ordinary_fingerprint_git_past_deadline",
            _past,
        )
        assert (
            comment_verdict_residue._load_git_index_stage_map(
                worktree_path=tmp_path,
                git_env={},
                paths=("a.py", "b.py"),
            )
            is None
        )
    assert calls["n"] == 1


@pytest.mark.unit
def test_load_git_index_stage_map_scopes_ls_files_to_requested_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6ewISJ: stage map must not dump the whole index under dirty-path caps."""
    captured: dict[str, object] = {}

    def _popen(
        command: object,
        *,
        env: object,
        max_records: object,
        max_bytes: object,
        timeout: object,
    ) -> tuple[bytes, ...]:
        del env, max_records, max_bytes, timeout
        captured["command"] = list(command)  # type: ignore[arg-type]
        return (b"100644 " + b"a" * 40 + b" 0\tsrc/x.py",)

    monkeypatch.setattr(comment_verdict_residue, "_popen_capped_nul_path_records", _popen)
    result = comment_verdict_residue._load_git_index_stage_map(
        worktree_path=tmp_path,
        git_env={},
        paths=("src/x.py",),
    )
    assert result == {"src/x.py": (("0", "100644", "a" * 40),)}
    command = captured["command"]
    assert isinstance(command, list)
    assert "--literal-pathspecs" in command
    assert command.index("--literal-pathspecs") < command.index("ls-files")
    assert "--" in command
    assert command[command.index("--") + 1 :] == ["src/x.py"]


@pytest.mark.unit
def test_load_git_index_stage_map_uses_literal_pathspecs_for_glob_named_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6ewp-V: dirty paths must not be interpreted as Git pathspec magic."""
    captured: dict[str, object] = {}
    magic_path = ":(attr:foo)bar"

    def _popen(
        command: object,
        *,
        env: object,
        max_records: object,
        max_bytes: object,
        timeout: object,
    ) -> tuple[bytes, ...]:
        del env, max_records, max_bytes, timeout
        captured["command"] = list(command)  # type: ignore[arg-type]
        return (b"100644 " + b"d" * 40 + b" 0\t" + magic_path.encode("utf-8"),)

    monkeypatch.setattr(comment_verdict_residue, "_popen_capped_nul_path_records", _popen)
    result = comment_verdict_residue._load_git_index_stage_map(
        worktree_path=tmp_path,
        git_env={},
        paths=(magic_path,),
    )
    assert result == {magic_path: (("0", "100644", "d" * 40),)}
    command = captured["command"]
    assert isinstance(command, list)
    assert command[command.index("--literal-pathspecs") + 1] == "ls-files"
    assert command[command.index("--") + 1 :] == [magic_path]


@pytest.mark.unit
def test_load_git_index_stage_map_resolves_colon_magic_literal_filenames(
    tmp_path: Path,
) -> None:
    """Real Git: ``:(attr:…)`` filenames miss without ``--literal-pathspecs``."""
    worktree = tmp_path / "ws_literal_pathspecs"
    worktree.mkdir()
    init_git_worktree(worktree)
    magic_name = ":(attr:foo)bar"
    magic_path = worktree / magic_name
    magic_path.write_text("tracked\n", encoding="utf-8")
    subprocess.run(
        ["git", "--literal-pathspecs", "add", "--", magic_name],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "add magic name"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    magic_path.write_text("dirty\n", encoding="utf-8")
    subprocess.run(
        ["git", "--literal-pathspecs", "add", "--", magic_name],
        cwd=worktree,
        check=True,
        capture_output=True,
    )

    with comment_verdict_residue._residue_fingerprint_nested_scan_budget():
        stage_map = comment_verdict_residue._load_git_index_stage_map(
            worktree_path=worktree,
            git_env=_git_env(),
            paths=(magic_name,),
        )
    assert stage_map is not None
    assert magic_name in stage_map
    first_blob = stage_map[magic_name][0][2]

    magic_path.write_text("mutated\n", encoding="utf-8")
    subprocess.run(
        ["git", "--literal-pathspecs", "add", "--", magic_name],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    with comment_verdict_residue._residue_fingerprint_nested_scan_budget():
        stage_map_after = comment_verdict_residue._load_git_index_stage_map(
            worktree_path=worktree,
            git_env=_git_env(),
            paths=(magic_name,),
        )
    assert stage_map_after is not None
    assert magic_name in stage_map_after
    assert stage_map_after[magic_name][0][2] != first_blob


@pytest.mark.unit
def test_hash_tracked_residue_diffs_survives_large_index_via_path_scoped_stage_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whole-index caps must not fail-close fingerprints when only dirty paths are staged."""
    worktree = tmp_path / "ws_large_index_scope"
    worktree.mkdir()
    init_git_worktree(worktree)
    target = worktree / "src" / "x.py"
    target.write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/x.py"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add tracked"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    target.write_text("dirty\n", encoding="utf-8")

    real_popen = comment_verdict_residue._popen_capped_nul_path_records

    def _popen(
        command: object,
        *,
        env: object,
        max_records: object,
        max_bytes: object,
        timeout: object,
    ) -> tuple[bytes, ...] | None:
        cmd = list(command)  # type: ignore[arg-type]
        if (
            "ls-files" in cmd
            and "--stage" in cmd
            and ("--" not in cmd or cmd.index("--") == len(cmd) - 1)
        ):
            # Simulate whole-index listing exceeding dirty-path caps.
            return None
        return real_popen(
            cmd,
            env=env,  # type: ignore[arg-type]
            max_records=max_records,  # type: ignore[arg-type]
            max_bytes=max_bytes,  # type: ignore[arg-type]
            timeout=timeout,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(comment_verdict_residue, "_popen_capped_nul_path_records", _popen)
    with comment_verdict_residue._residue_fingerprint_nested_scan_budget():
        result = comment_verdict_residue._hash_tracked_residue_diffs(
            worktree_path=worktree,
            git_env=_git_env(),
            cached=False,
        )
    assert result is not None
