"""Capped NUL path listing and nested tracked-path residue edge regressions (part 10)."""

from __future__ import annotations

import contextlib
import os
import signal
import stat
import subprocess
from pathlib import Path

import pytest

from awf.common.commands import CommandResult
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
    """Return a readable binary file object backed by an OS pipe containing ``payload``."""
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
            self.stdout = stdout

        def poll(self) -> int | None:
            return None if not killed["value"] else 0

        def kill(self) -> None:
            killed["value"] = True

        def wait(self, timeout: float | None = None) -> int:
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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_correction_residue_fingerprint_includes_local_git_config_metadata(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6e0Xdl: config-only mutations must change the residue fingerprint.

    ``git config --local url.*.insteadOf`` leaves porcelain clean, so a path-only
    fingerprint returned empty and non-FIXED verdicts were accepted while the
    rewrite survived rollback and could redirect a later control-plane push.
    """
    from types import SimpleNamespace

    from awf.common.commands import CommandResult
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_git_meta"
    worktree.mkdir()
    init_git_worktree(worktree)

    assert fp_mod.remember_item_start_local_git_configs(worktree) is True

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout="", stderr="", stdout_bytes=b"")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_git_meta",
        worktree_path=worktree,
    )
    assert start_fp is not None
    assert start_fp.startswith("git-meta:")
    assert not fp_mod._fingerprint_has_pr_worthy_path_residue(start_fp)

    poison_key = "url.file:///attacker/.insteadOf"
    poison_value = "https://github.com/"
    subprocess.run(
        ["git", "config", "--local", poison_key, poison_value],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    plain_status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    assert plain_status.stdout.strip() == ""

    poisoned_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_git_meta",
        worktree_path=worktree,
    )
    assert poisoned_fp is not None
    assert poisoned_fp.startswith("git-meta:")
    assert poisoned_fp != start_fp
    assert comment_verdict_residue._correction_authored_mutation_vs_start(
        attempt_start_head="abc123",
        pre_sink_head="abc123",
        correction_start_residue_fp=start_fp,
        pre_sink_residue_fp=poisoned_fp,
    )
    assert (
        await comment_verdict_residue._correction_attempt_left_pr_worthy_residue(
            runner,
            workspace_id="ws_git_meta",
            worktree_path=worktree,
        )
        is False
    )

    assert fp_mod.restore_item_start_local_git_configs(worktree) is True
    get_poison = subprocess.run(
        ["git", "config", "--local", "--get", poison_key],
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    assert get_poison.returncode != 0

    restored_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_git_meta",
        worktree_path=worktree,
    )
    assert restored_fp == start_fp


@pytest.mark.unit
@pytest.mark.asyncio
async def test_protocol_retry_rollback_restores_local_git_config_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6e0Xdl: rollback must restore item-start local Git config."""
    from types import SimpleNamespace

    from awf.runtime.pr_monitor_runner import comment_verdict, comment_verdict_residue_fingerprint
    from awf.runtime.validation_worktree import (
        ValidationWorktreeCheck,
        ValidationWorktreeCleanup,
    )

    worktree = tmp_path / "ws_git_meta_rollback"
    worktree.mkdir()
    init_git_worktree(worktree)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert comment_verdict_residue_fingerprint.remember_item_start_local_git_configs(worktree)
    subprocess.run(
        ["git", "config", "--local", "url.file:///attacker/.insteadOf", "https://github.com/"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )

    async def _cleanup(**_kwargs: object) -> ValidationWorktreeCleanup:
        return ValidationWorktreeCleanup(
            cleaned=True,
            check=ValidationWorktreeCheck(clean=True, paths=()),
            restore_ref=head,
        )

    monkeypatch.setattr(
        "awf.runtime.validation_worktree.cleanup_validation_worktree_side_effects",
        _cleanup,
    )

    async def _rev_parse_head(_path: Path) -> str:
        return head

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        del cmd, _kwargs
        return CommandResult(returncode=0, stdout=f"{head}\n", stderr="")

    runner = SimpleNamespace(
        _deps=SimpleNamespace(
            adapter=SimpleNamespace(is_hosted=False),
            runner=SimpleNamespace(run=_run),
        ),
        _rev_parse_head=_rev_parse_head,
    )
    assert await comment_verdict._rollback_unaccepted_protocol_retry_changes(
        runner,
        workspace_id="ws_git_meta_rollback",
        worktree_path=worktree,
        item_start_head=head,
        state=None,
    )
    get_poison = subprocess.run(
        ["git", "config", "--local", "--get", "url.file:///attacker/.insteadOf"],
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    assert get_poison.returncode != 0


@pytest.mark.unit
def test_remember_item_start_local_git_configs_clears_stale_cache_on_snapshot_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6e0xSO: failed snapshot must not leave a prior cache entry.

    A later item on a reused worktree path must not restore an earlier item's
    local Git config blob when the new remember() snapshot fails closed.
    """
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_stale_git_config_cache"
    worktree.mkdir()
    init_git_worktree(worktree)

    assert fp_mod.remember_item_start_local_git_configs(worktree) is True
    key = str(worktree.resolve())
    assert key in fp_mod._ITEM_START_LOCAL_GIT_CONFIGS

    subprocess.run(
        ["git", "config", "--local", "user.email", "later-item@example.com"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(
        fp_mod,
        "_snapshot_worktree_local_git_configs",
        lambda _path, **_kwargs: None,
    )
    assert fp_mod.remember_item_start_local_git_configs(worktree) is False
    assert key not in fp_mod._ITEM_START_LOCAL_GIT_CONFIGS
    assert key not in fp_mod._ITEM_START_GIT_LINKAGE
    assert key not in fp_mod._ITEM_START_NESTED_GIT_LINKAGES

    assert fp_mod.restore_item_start_local_git_configs(worktree) is True
    email = subprocess.run(
        ["git", "config", "--local", "--get", "user.email"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert email == "later-item@example.com"


@pytest.mark.unit
def test_write_local_git_config_file_replaces_fifo_without_blocking(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6e2x5c: restore must not open a destination FIFO with O_TRUNC.

    Opening the existing inode blocks forever on a reader-less FIFO before the
    post-open fstat guard can refuse it. Write a fresh temp inode and atomically
    replace the path instead.
    """
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    dest = tmp_path / "config"
    os.mkfifo(dest)
    assert stat.S_ISFIFO(dest.lstat().st_mode)

    # Alarm so a regression that opens the FIFO fails the test promptly instead
    # of hanging the suite / monitor event loop.
    previous = signal.signal(signal.SIGALRM, lambda *_args: (_ for _ in ()).throw(TimeoutError()))
    signal.alarm(2)
    try:
        assert fp_mod._write_local_git_config_file(dest, "[core]\n\trepositoryformatversion = 0\n")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)

    mode = dest.lstat().st_mode
    assert stat.S_ISREG(mode)
    assert "[core]" in dest.read_text(encoding="utf-8")


@pytest.mark.unit
def test_write_local_git_config_file_does_not_truncate_hard_linked_target(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6e2x5c: restore must not truncate a hard-linked shared inode.

    Shared mirror refs are agent-writable; a hard-linked ``config`` opened with
    ``O_TRUNC`` would zero the linked target before fstat can refuse the write.
    """
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    shared = tmp_path / "refs_heads_main"
    shared.write_text("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n", encoding="utf-8")
    config = tmp_path / "config"
    os.link(shared, config)
    assert config.stat().st_ino == shared.stat().st_ino

    restored = "[core]\n\trepositoryformatversion = 0\n"
    assert fp_mod._write_local_git_config_file(config, restored) is True
    assert config.read_text(encoding="utf-8") == restored
    assert shared.read_text(encoding="utf-8") == ("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n")
    assert config.stat().st_ino != shared.stat().st_ino


@pytest.mark.unit
def test_write_local_git_config_file_fails_closed_on_temp_open_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Temp-inode create failure must return False without touching the destination."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    dest = tmp_path / "config"
    dest.write_text("original\n", encoding="utf-8")

    def _boom(*_args: object, **_kwargs: object) -> int:
        raise OSError("open failed")

    monkeypatch.setattr(fp_mod.os, "open", _boom)
    assert fp_mod._write_local_git_config_file(dest, "restored\n") is False
    assert dest.read_text(encoding="utf-8") == "original\n"
    assert list(tmp_path.glob(".config.*.tmp")) == []


@pytest.mark.unit
def test_write_local_git_config_file_cleans_temp_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed atomic replace must unlink the sibling temp and leave the destination."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    dest = tmp_path / "config"
    dest.write_text("original\n", encoding="utf-8")

    def _boom_replace(self: Path, _target: Path) -> Path:
        raise OSError("replace failed")

    monkeypatch.setattr(fp_mod.Path, "replace", _boom_replace)
    assert fp_mod._write_local_git_config_file(dest, "restored\n") is False
    assert dest.read_text(encoding="utf-8") == "original\n"
    assert list(tmp_path.glob(".config.*.tmp")) == []


@pytest.mark.unit
def test_write_local_git_config_file_fails_closed_when_temp_replaced_with_regular_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6e3DXZ: swapped regular temp inode must not report success."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    dest = tmp_path / "config"
    dest.write_text("original\n", encoding="utf-8")
    real_replace = Path.replace
    held_paths: list[Path] = []

    def _swap_regular_then_replace(self: Path, target: Path) -> Path:
        held = self.with_name(f"{self.name}.held")
        self.rename(held)
        held_paths.append(held)
        self.write_text("evil-config\n", encoding="utf-8")
        return real_replace(self, target)

    monkeypatch.setattr(fp_mod.Path, "replace", _swap_regular_then_replace)
    try:
        assert fp_mod._write_local_git_config_file(dest, "restored\n") is False
        assert dest.read_text(encoding="utf-8") != "restored\n"
    finally:
        for held in held_paths:
            with contextlib.suppress(FileNotFoundError):
                held.unlink()


@pytest.mark.unit
def test_write_local_git_config_file_fails_closed_when_temp_swapped_before_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6e3DXZ: pathname replace must not trust a swapped temp entry.

    After the helper closes the temp fd, a surviving agent can replace that name
    with a symlink (or other content) before ``Path.replace``. Restore must fail
    closed rather than report success while installing untrusted bytes.
    """
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    dest = tmp_path / "config"
    dest.write_text("original\n", encoding="utf-8")
    attacker_payload = tmp_path / "attacker_payload"
    attacker_payload.write_text("evil-config\n", encoding="utf-8")
    real_replace = Path.replace
    held_paths: list[Path] = []

    def _swap_then_replace(self: Path, target: Path) -> Path:
        held = self.with_name(f"{self.name}.held")
        self.rename(held)
        held_paths.append(held)
        self.symlink_to(attacker_payload)
        return real_replace(self, target)

    monkeypatch.setattr(fp_mod.Path, "replace", _swap_then_replace)
    try:
        assert fp_mod._write_local_git_config_file(dest, "restored\n") is False
        # Never accept a regular-file install of the trusted text after a temp swap.
        try:
            mode = dest.lstat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISREG(mode):
            assert dest.read_text(encoding="utf-8") != "restored\n"
    finally:
        for held in held_paths:
            with contextlib.suppress(FileNotFoundError):
                held.unlink()


@pytest.mark.unit
def test_write_local_git_config_file_fails_closed_when_temp_mutated_before_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6e3DXZ: same-inode temp tampering before replace fails closed."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    dest = tmp_path / "config"
    dest.write_text("original\n", encoding="utf-8")
    real_close = fp_mod.os.close
    close_count = {"n": 0}

    def _close_and_tamper(fd: int) -> None:
        real_close(fd)
        close_count["n"] += 1
        if close_count["n"] == 1:
            for tmp in tmp_path.glob(".config.*.tmp"):
                # Longer than ``restored\\n`` so the size guard rejects first.
                tmp.write_text("tampered-bytes\n", encoding="utf-8")

    monkeypatch.setattr(fp_mod.os, "close", _close_and_tamper)
    assert fp_mod._write_local_git_config_file(dest, "restored\n") is False
    assert dest.read_text(encoding="utf-8") == "original\n"


@pytest.mark.unit
def test_write_local_git_config_file_fails_closed_when_temp_inode_replaced_before_verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6e3DXZ: pre-replace verify rejects a different temp inode."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    dest = tmp_path / "config"
    dest.write_text("original\n", encoding="utf-8")
    real_close = fp_mod.os.close
    close_count = {"n": 0}
    held_paths: list[Path] = []

    def _close_and_recreate(fd: int) -> None:
        real_close(fd)
        close_count["n"] += 1
        if close_count["n"] == 1:
            for tmp in tmp_path.glob(".config.*.tmp"):
                # Keep the trusted inode linked so its number is not recycled onto
                # the attacker-controlled replacement (common on local filesystems).
                held = tmp.with_name(f"{tmp.name}.held")
                tmp.rename(held)
                held_paths.append(held)
                tmp.write_text("other-inode\n", encoding="utf-8")

    monkeypatch.setattr(fp_mod.os, "close", _close_and_recreate)
    try:
        assert fp_mod._write_local_git_config_file(dest, "restored\n") is False
        assert dest.read_text(encoding="utf-8") == "original\n"
    finally:
        for held in held_paths:
            with contextlib.suppress(FileNotFoundError):
                held.unlink()


@pytest.mark.unit
def test_write_local_git_config_file_fails_closed_when_temp_becomes_fifo_before_verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6e3DXZ: FIFO planted at the temp path must not hang or succeed."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    dest = tmp_path / "config"
    dest.write_text("original\n", encoding="utf-8")
    real_close = fp_mod.os.close
    close_count = {"n": 0}
    held_paths: list[Path] = []

    def _close_and_fifo(fd: int) -> None:
        real_close(fd)
        close_count["n"] += 1
        if close_count["n"] == 1:
            for tmp in tmp_path.glob(".config.*.tmp"):
                held = tmp.with_name(f"{tmp.name}.held")
                tmp.rename(held)
                held_paths.append(held)
                os.mkfifo(tmp)

    monkeypatch.setattr(fp_mod.os, "close", _close_and_fifo)
    previous = signal.signal(signal.SIGALRM, lambda *_a: (_ for _ in ()).throw(TimeoutError()))
    signal.alarm(2)
    try:
        assert fp_mod._write_local_git_config_file(dest, "restored\n") is False
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        for held in held_paths:
            with contextlib.suppress(FileNotFoundError):
                held.unlink()
        for fifo in tmp_path.glob(".config.*.tmp"):
            with contextlib.suppress(FileNotFoundError, OSError):
                fifo.unlink()
    assert dest.read_text(encoding="utf-8") == "original\n"


@pytest.mark.unit
def test_write_local_git_config_file_fails_closed_when_verify_open_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-replace verify open failures fail closed without renaming onto the dest."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    dest = tmp_path / "config"
    dest.write_text("original\n", encoding="utf-8")
    real_open = fp_mod.os.open
    saw_write = {"n": False}

    def _open_fail_verify(path: str | bytes | os.PathLike[str], flags: int, *args: object) -> int:
        # After the O_EXCL create, the next open is the verify reopen (no O_CREAT).
        # O_RDONLY is 0 on Linux, so detect verify opens by absence of O_CREAT/O_WRONLY.
        if saw_write["n"] and not (flags & os.O_CREAT) and not (flags & os.O_WRONLY):
            raise OSError("verify open failed")
        fd = real_open(path, flags, *args)
        if flags & os.O_CREAT:
            saw_write["n"] = True
        return fd

    monkeypatch.setattr(fp_mod.os, "open", _open_fail_verify)
    assert fp_mod._write_local_git_config_file(dest, "restored\n") is False
    assert dest.read_text(encoding="utf-8") == "original\n"


@pytest.mark.unit
def test_write_local_git_config_file_fails_closed_when_post_replace_lstat_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-replace lstat failure fails closed after a successful pathname replace."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    dest = tmp_path / "config"
    dest.write_text("original\n", encoding="utf-8")
    real_lstat = fp_mod.os.lstat
    real_replace = Path.replace

    def _replace_then_break_lstat(self: Path, target: Path) -> Path:
        result = real_replace(self, target)

        def _boom(path: str | bytes | os.PathLike[str]) -> os.stat_result:
            if Path(path) == target:
                raise OSError("lstat failed")
            return real_lstat(path)

        monkeypatch.setattr(fp_mod.os, "lstat", _boom)
        return result

    monkeypatch.setattr(fp_mod.Path, "replace", _replace_then_break_lstat)
    assert fp_mod._write_local_git_config_file(dest, "restored\n") is False


@pytest.mark.unit
def test_write_local_git_config_file_fails_closed_when_dest_mutated_after_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6e3DXZ: post-replace byte verify rejects same-inode mutation."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    dest = tmp_path / "config"
    dest.write_text("original\n", encoding="utf-8")
    real_replace = Path.replace

    def _replace_then_mutate(self: Path, target: Path) -> Path:
        result = real_replace(self, target)
        # Same length as ``restored\n`` so the byte compare (not size) rejects.
        target.write_text("EVILXXXX\n", encoding="utf-8")
        return result

    monkeypatch.setattr(fp_mod.Path, "replace", _replace_then_mutate)
    assert fp_mod._write_local_git_config_file(dest, "restored\n") is False
    assert dest.read_text(encoding="utf-8") != "restored\n"
