"""Capped NUL path listing and nested tracked-path residue edge regressions (part 10)."""

from __future__ import annotations

import contextlib
import os
import subprocess
from pathlib import Path

import pytest

from awf.node.git_manager import git_env_without_object_lookup_overrides
from awf.runtime.pr_monitor_runner import (
    comment_verdict_residue,
    comment_verdict_residue_io,
)
from tests.unit.runtime.test_comment_verdict_coverage_edges_parts._helpers import (
    init_git_worktree,
    init_git_worktree_with_dirty_submodule,
    init_git_worktree_with_embedded_repo,
)

_git_env = git_env_without_object_lookup_overrides


def _pipe_with_bytes(payload: bytes) -> tuple[int, object]:
    """
    Create a readable binary stream containing the supplied bytes.
    
    Parameters:
    	payload (bytes): Bytes to make available for reading.
    
    Returns:
    	tuple[int, object]: The read file descriptor and its readable binary file object.
    """
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, payload)
    finally:
        os.close(write_fd)
    return read_fd, os.fdopen(read_fd, "rb", closefd=True)


def test_read_capped_nul_path_records_returns_paths_under_caps() -> None:
    """PRRT_kwDOSJAM6s6efXeI: streaming NUL drain accepts bounded path lists."""
    _read_fd, stdout = _pipe_with_bytes(b"a.py\0b.py\0")
    try:
        records = comment_verdict_residue_io._read_capped_nul_path_records(
            stdout,
            max_records=10,
            max_bytes=10_000,
            deadline_monotonic=None,
        )
    finally:
        stdout.close()
    assert records == (b"a.py", b"b.py")


def test_read_capped_nul_path_records_fails_closed_on_path_cap() -> None:
    """PRRT_kwDOSJAM6s6efXeI: path-count cap is enforced while reading, not after."""
    payload = b"".join(f"p{i}.py\0".encode() for i in range(5))
    _read_fd, stdout = _pipe_with_bytes(payload)
    try:
        records = comment_verdict_residue_io._read_capped_nul_path_records(
            stdout,
            max_records=3,
            max_bytes=10_000,
            deadline_monotonic=None,
        )
    finally:
        stdout.close()
    assert records is None


def test_read_capped_nul_path_records_fails_closed_on_byte_cap() -> None:
    """PRRT_kwDOSJAM6s6efXeI: stdout byte cap is enforced while reading."""
    payload = b"aaaa.py\0bbbb.py\0"
    _read_fd, stdout = _pipe_with_bytes(payload)
    try:
        records = comment_verdict_residue_io._read_capped_nul_path_records(
            stdout,
            max_records=100,
            max_bytes=8,
            deadline_monotonic=None,
        )
    finally:
        stdout.close()
    assert records is None


def test_read_capped_nul_path_records_fails_closed_on_missing_terminator() -> None:
    _read_fd, stdout = _pipe_with_bytes(b"truncated.py")
    try:
        records = comment_verdict_residue_io._read_capped_nul_path_records(
            stdout,
            max_records=10,
            max_bytes=10_000,
            deadline_monotonic=None,
        )
    finally:
        stdout.close()
    assert records is None


def test_read_capped_nul_path_records_fails_closed_on_empty_path_record() -> None:
    _read_fd, stdout = _pipe_with_bytes(b"a.py\0\0b.py\0")
    try:
        records = comment_verdict_residue_io._read_capped_nul_path_records(
            stdout,
            max_records=10,
            max_bytes=10_000,
            deadline_monotonic=None,
        )
    finally:
        stdout.close()
    assert records is None


def test_read_capped_nul_path_records_fails_closed_on_negative_caps() -> None:
    _read_fd, stdout = _pipe_with_bytes(b"a.py\0")
    try:
        assert (
            comment_verdict_residue_io._read_capped_nul_path_records(
                stdout,
                max_records=-1,
                max_bytes=10_000,
                deadline_monotonic=None,
            )
            is None
        )
    finally:
        stdout.close()


def test_read_capped_nul_path_records_fails_closed_without_fileno() -> None:
    class _NoFileno:
        def fileno(self) -> int:
            """Raise an error when no file descriptor is available.
            
            Raises:
                OSError: Always, because this object has no file descriptor.
            """
            raise OSError("no fd")

    assert (
        comment_verdict_residue_io._read_capped_nul_path_records(
            _NoFileno(),  # type: ignore[arg-type]
            max_records=10,
            max_bytes=10_000,
            deadline_monotonic=None,
        )
        is None
    )


def test_read_capped_nul_path_records_fails_closed_on_expired_deadline() -> None:
    _read_fd, stdout = _pipe_with_bytes(b"a.py\0")
    try:
        records = comment_verdict_residue_io._read_capped_nul_path_records(
            stdout,
            max_records=10,
            max_bytes=10_000,
            deadline_monotonic=0.0,
        )
    finally:
        stdout.close()
    assert records is None


def test_read_capped_nul_path_records_fails_closed_on_select_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _read_fd, stdout = _pipe_with_bytes(b"a.py\0")

    def _raise_select(*_args: object, **_kwargs: object) -> list[object]:
        """Simulate a failed select operation.
        
        Raises:
            OSError: Always raised to represent a select failure.
        """
        raise OSError("select failed")

    monkeypatch.setattr(comment_verdict_residue_io.select, "select", _raise_select)
    try:
        assert (
            comment_verdict_residue_io._read_capped_nul_path_records(
                stdout,
                max_records=10,
                max_bytes=10_000,
                deadline_monotonic=None,
            )
            is None
        )
    finally:
        stdout.close()


def test_read_capped_nul_path_records_fails_closed_when_select_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _read_fd, stdout = _pipe_with_bytes(b"a.py\0")

    def _empty_select(*_args: object, **_kwargs: object) -> tuple[list[object], ...]:
        """Return empty readable, writable, and exceptional descriptor lists."""
        return ([], [], [])

    monkeypatch.setattr(comment_verdict_residue_io.select, "select", _empty_select)
    try:
        assert (
            comment_verdict_residue_io._read_capped_nul_path_records(
                stdout,
                max_records=10,
                max_bytes=10_000,
                deadline_monotonic=None,
            )
            is None
        )
    finally:
        stdout.close()


def test_read_capped_nul_path_records_fails_closed_on_read_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _read_fd, stdout = _pipe_with_bytes(b"a.py\0")

    def _raise_read(*_args: object, **_kwargs: object) -> bytes:
        raise OSError("read failed")

    monkeypatch.setattr(comment_verdict_residue_io.os, "read", _raise_read)
    try:
        assert (
            comment_verdict_residue_io._read_capped_nul_path_records(
                stdout,
                max_records=10,
                max_bytes=10_000,
                deadline_monotonic=None,
            )
            is None
        )
    finally:
        stdout.close()


def test_list_nested_untracked_paths_capped_fails_closed_on_zero_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    init_git_worktree(worktree)
    monkeypatch.setattr(
        comment_verdict_residue,
        "_nested_untrusted_git_probe_command_timeout",
        lambda: 0.0,
    )
    assert (
        comment_verdict_residue._list_nested_untracked_paths_capped(
            worktree_path=worktree,
            git_env={},
        )
        is None
    )


def test_list_nested_untracked_paths_capped_fails_closed_on_popen_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    init_git_worktree(worktree)

    def _raise_popen(*_args: object, **_kwargs: object) -> object:
        raise OSError("popen failed")

    monkeypatch.setattr(comment_verdict_residue_io.subprocess, "Popen", _raise_popen)
    assert (
        comment_verdict_residue._list_nested_untracked_paths_capped(
            worktree_path=worktree,
            git_env={},
        )
        is None
    )


def test_list_nested_untracked_paths_capped_fails_closed_when_stdout_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    init_git_worktree(worktree)

    class _FakeProc:
        stdout = None

        def poll(self) -> int:
            return 0

        def kill(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            """
            Report successful process completion without waiting.
            
            Returns:
                int: The exit status `0`.
            """
            return 0

    monkeypatch.setattr(
        comment_verdict_residue_io.subprocess,
        "Popen",
        lambda *_a, **_k: _FakeProc(),
    )
    assert (
        comment_verdict_residue._list_nested_untracked_paths_capped(
            worktree_path=worktree,
            git_env={},
        )
        is None
    )


def test_list_nested_untracked_paths_capped_fails_closed_on_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    init_git_worktree(worktree)
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    stdout = os.fdopen(read_fd, "rb", closefd=True)

    class _FakeProc:
        def __init__(self) -> None:
            """
            Initialize the object with its standard output stream.
            """
            self.stdout = stdout

        def poll(self) -> int:
            return 1

        def kill(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 1

    monkeypatch.setattr(
        comment_verdict_residue_io.subprocess,
        "Popen",
        lambda *_a, **_k: _FakeProc(),
    )
    monkeypatch.setattr(
        comment_verdict_residue_io,
        "_read_capped_nul_path_records",
        lambda *_a, **_k: (),
    )
    assert (
        comment_verdict_residue._list_nested_untracked_paths_capped(
            worktree_path=worktree,
            git_env={},
        )
        is None
    )


def test_list_nested_untracked_paths_capped_fails_closed_on_wait_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    init_git_worktree(worktree)
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    stdout = os.fdopen(read_fd, "rb", closefd=True)
    killed = {"value": False}

    class _FakeProc:
        def __init__(self) -> None:
            """
            Initialize the object with its standard output stream.
            """
            self.stdout = stdout

        def poll(self) -> int | None:
            return None if not killed["value"] else 0

        def kill(self) -> None:
            killed["value"] = True

        def wait(self, timeout: float | None = None) -> int:
            """
            Wait for the process to terminate.
            
            Parameters:
                timeout (float | None): Maximum time to wait before raising an error.
            
            Returns:
                int: Zero when the process has been terminated.
            
            Raises:
                subprocess.TimeoutExpired: If the process has not been terminated before the timeout.
            """
            if not killed["value"]:
                raise subprocess.TimeoutExpired(cmd="git", timeout=timeout or 0)
            return 0

    monkeypatch.setattr(
        comment_verdict_residue_io.subprocess,
        "Popen",
        lambda *_a, **_k: _FakeProc(),
    )
    monkeypatch.setattr(
        comment_verdict_residue_io,
        "_read_capped_nul_path_records",
        lambda *_a, **_k: (b"a.py",),
    )
    assert (
        comment_verdict_residue._list_nested_untracked_paths_capped(
            worktree_path=worktree,
            git_env={},
        )
        is None
    )
    assert killed["value"] is True


def test_list_nested_untracked_paths_capped_fails_closed_when_enum_budget_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    init_git_worktree(worktree)

    monkeypatch.setattr(
        comment_verdict_residue,
        "_list_nested_nul_git_path_records",
        lambda **_k: (b"a.py", b"b.py"),
    )
    monkeypatch.setattr(
        comment_verdict_residue,
        "_directory_enum_consume_entries",
        lambda _count: False,
    )
    assert (
        comment_verdict_residue._list_nested_untracked_paths_capped(
            worktree_path=worktree,
            git_env={},
        )
        is None
    )


def test_terminate_capped_nul_path_process_noop_when_already_exited() -> None:
    finished = subprocess.Popen(["true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finished.wait(timeout=5)
    comment_verdict_residue_io._terminate_capped_nul_path_process(finished)
    assert finished.poll() is not None


def test_list_nested_untracked_paths_capped_applies_pinned_common_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    init_git_worktree(worktree)
    captured: dict[str, object] = {}

    def _capture_records(
        command: object,
        *,
        env: object,
        max_records: object,
        max_bytes: object,
        timeout: object,
    ) -> tuple[bytes, ...]:
        """
        Capture the supplied environment and produce no records.
        
        Parameters:
            env (object): Environment value to store for inspection.
        
        Returns:
            tuple[bytes, ...]: An empty tuple.
        """
        del command, max_records, max_bytes, timeout
        captured["env"] = env
        return ()

    monkeypatch.setattr(
        comment_verdict_residue,
        "_fresh_pinned_nested_git_common_dir",
        lambda: tmp_path / "common.git",
    )
    monkeypatch.setattr(
        comment_verdict_residue,
        "_popen_capped_nul_path_records",
        _capture_records,
    )
    paths = comment_verdict_residue._list_nested_untracked_paths_capped(
        worktree_path=worktree,
        git_env={},
    )
    assert paths == set()
    env = captured["env"]
    assert isinstance(env, dict)
    assert env.get("GIT_COMMON_DIR") == str(tmp_path / "common.git")


def test_list_nested_tracked_changed_paths_capped_dedupes_and_honors_enum_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6ef8Fs: tracked name-only listing dedupes and fails closed on enum budget."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    init_git_worktree(worktree)

    monkeypatch.setattr(
        comment_verdict_residue,
        "_list_nested_nul_git_path_records",
        lambda **_k: (b"a.py", b"a.py", b"b.py"),
    )
    paths = comment_verdict_residue._list_nested_tracked_changed_paths_capped(
        worktree_path=worktree,
        git_env={},
        cached=True,
    )
    assert paths == ("a.py", "b.py")

    monkeypatch.setattr(
        comment_verdict_residue,
        "_directory_enum_consume_entries",
        lambda _count: False,
    )
    assert (
        comment_verdict_residue._list_nested_tracked_changed_paths_capped(
            worktree_path=worktree,
            git_env={},
            cached=False,
        )
        is None
    )


def test_git_nested_worktree_commit_fails_closed_when_untracked_ls_files_exceeds_path_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6efXeI: nested untracked listing must not buffer uncapped paths."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    init_git_worktree_with_dirty_submodule(worktree)
    for index in range(3):
        (worktree / "sub" / f"u{index}.py").write_text(f"{index}\n", encoding="utf-8")

    monkeypatch.setattr(
        comment_verdict_residue,
        "_NESTED_UNTRACKED_LS_FILES_MAX_PATHS",
        2,
    )
    assert (
        comment_verdict_residue._git_nested_worktree_commit(
            worktree_path=worktree,
            path="sub",
            git_env={},
        )
        is None
    )


def test_git_nested_worktree_commit_fails_closed_when_tracked_name_only_exceeds_path_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6ef8Fs: nested tracked --name-only must not buffer uncapped paths."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    init_git_worktree_with_dirty_submodule(worktree)
    sub = worktree / "sub"
    for index in range(3):
        (sub / f"t{index}.py").write_text(f"{index}\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", f"t{index}.py"],
            cwd=sub,
            check=True,
            capture_output=True,
        )

    monkeypatch.setattr(
        comment_verdict_residue,
        "_NESTED_UNTRACKED_LS_FILES_MAX_PATHS",
        2,
    )
    assert (
        comment_verdict_residue._git_nested_worktree_commit(
            worktree_path=worktree,
            path="sub",
            git_env={},
        )
        is None
    )


@pytest.mark.unit
def test_tracked_residue_changed_paths_args_nested_includes_ignore_submodules_none() -> None:
    """PRRT_kwDOSJAM6s6ehEtb: nested ``diff-files`` must override per-submodule ignore."""
    with comment_verdict_residue._untrusted_nested_git_probe():
        args = comment_verdict_residue._tracked_residue_changed_paths_args(cached=False)
    assert args == ("diff-files", "--name-only", "-z", "--ignore-submodules=none")


@pytest.mark.unit
def test_nested_tracked_paths_include_dirty_submodule_despite_per_submodule_ignore(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ehEtb: nested ``diff-files`` must pass ``--ignore-submodules=none``.

    ``submodule.<name>.ignore=all`` overrides ``-c diff.ignoreSubmodules=none`` used by
    nested probes; without the CLI flag, dirty gitlinks are omitted from tracked-path
    listings and correction residue fingerprints stay unchanged.
    """
    worktree = tmp_path / "ws_nested_per_submodule_ignore"
    worktree.mkdir()
    init_git_worktree_with_dirty_submodule(worktree)
    subprocess.run(
        ["git", "config", "submodule.sub.ignore", "all"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )

    with comment_verdict_residue._untrusted_nested_git_probe():
        paths = comment_verdict_residue._list_nested_tracked_changed_paths_capped(
            worktree_path=worktree,
            git_env=_git_env(),
            cached=False,
        )

    assert paths is not None
    assert "sub" in paths


@pytest.mark.unit
def test_nested_git_probe_keeps_validated_config_after_include_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6elv_p: post-check probes must not re-read mutable local config.

    After the validated config snapshot is pinned, an agent can inject
    ``include.path``. Live Git then fails; nested probes must keep using the
    snapshot git-dir.
    """
    worktree = tmp_path / "ws_nested_config_snapshot"
    worktree.mkdir()
    other_ws = tmp_path / "ws_other"
    other_ws.mkdir()
    poison = other_ws / "poison.inc"
    poison.write_text("broken [[[[\n", encoding="utf-8")
    nested_path = init_git_worktree_with_embedded_repo(worktree)
    nested_root = worktree / nested_path
    poisoned = {"done": False}
    real_pin = comment_verdict_residue._nested_probe_config_snapshot_git_dir

    @contextlib.contextmanager
    def _pin_then_poison_live(snapshot_git_dir: Path):
        """
        Inject an invalid live Git configuration after the repository snapshot is pinned.
        
        Parameters:
        	snapshot_git_dir (Path): Git directory whose validated snapshot is pinned during the injection.
        """
        with real_pin(snapshot_git_dir):
            if not poisoned["done"]:
                subprocess.run(
                    ["git", "config", "include.path", str(poison)],
                    cwd=nested_root,
                    check=True,
                    capture_output=True,
                )
                live = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=nested_root,
                    check=False,
                    capture_output=True,
                )
                assert live.returncode != 0
                poisoned["done"] = True
            yield

    monkeypatch.setattr(
        comment_verdict_residue,
        "_nested_probe_config_snapshot_git_dir",
        _pin_then_poison_live,
    )

    result = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_path,
        git_env=_git_env(),
    )

    assert poisoned["done"] is True
    assert result is not None


@pytest.mark.unit
@pytest.mark.timeout(2)
def test_nested_git_probe_avoids_live_rev_parse_before_config_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6ewpcq: discovery must not run git against live config before snapshot.

    After the include check, an agent can inject ``include.path`` → FIFO. A
    pre-snapshot ``rev-parse --show-toplevel`` would block until the nested-probe
    timeout; discovery must wait for the validated config snapshot.
    """
    worktree = tmp_path / "ws_pre_snapshot_revparse"
    worktree.mkdir()
    nested_path = init_git_worktree_with_embedded_repo(worktree)
    nested_root = worktree / nested_path
    fifo = tmp_path / "poison.fifo"
    os.mkfifo(fifo, mode=0o644)
    poisoned = {"done": False}
    live_discovery_calls = {"n": 0}

    real_has_includes = (
        comment_verdict_residue.untrusted_nested_repository_local_config_has_includes
    )

    def _include_check_then_poison(
        path: Path,
        *,
        containment_roots: object | None = None,
    ) -> bool:
        """
        Determine whether a path has Git includes and optionally poison the nested repository configuration after the first negative result.
        
        Parameters:
            path (Path): Path whose Git configuration is checked.
            containment_roots (object | None): Optional roots used to constrain included configuration paths.
        
        Returns:
            bool: The result of the include check.
        """
        result = real_has_includes(path, containment_roots=containment_roots)  # type: ignore[arg-type]
        if not result and not poisoned["done"]:
            subprocess.run(
                ["git", "config", "include.path", str(fifo)],
                cwd=nested_root,
                check=True,
                capture_output=True,
            )
            poisoned["done"] = True
        return result

    real_probe = comment_verdict_residue._nested_git_probe_worktree_root

    def _probe_counting_live(**kwargs: object) -> Path | None:
        if (
            comment_verdict_residue._NESTED_UNTRUSTED_GIT_PROBE_CONFIG_SNAPSHOT_GIT_DIR.get()
            is None
        ):
            live_discovery_calls["n"] += 1
        return real_probe(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        comment_verdict_residue,
        "untrusted_nested_repository_local_config_has_includes",
        _include_check_then_poison,
    )
    monkeypatch.setattr(
        comment_verdict_residue,
        "_nested_git_probe_worktree_root",
        _probe_counting_live,
    )

    result = comment_verdict_residue._git_nested_worktree_commit(
        worktree_path=worktree,
        path=nested_path,
        git_env=_git_env(),
    )

    assert poisoned["done"] is True
    assert live_discovery_calls["n"] == 0
    # Snapshot re-check sees the injected include and fails closed without hanging.
    assert result is None
