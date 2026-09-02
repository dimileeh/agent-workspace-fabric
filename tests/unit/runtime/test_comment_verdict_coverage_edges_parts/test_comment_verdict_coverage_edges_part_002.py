"""Focused branch coverage for provider-neutral comment verdict helpers (part 2)."""

from __future__ import annotations

import errno
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult
from awf.runtime.pr_monitor_runner import comment_verdict_residue
from tests.unit.runtime.test_comment_verdict_coverage_edges_parts._helpers import (
    init_git_worktree,
    init_git_worktree_with_dirty_submodule,
)

_init_git_worktree = init_git_worktree
_init_git_worktree_with_dirty_submodule = init_git_worktree_with_dirty_submodule


@pytest.mark.unit
def test_hash_untracked_residue_paths_distinguishes_ambiguous_byte_boundaries(
    tmp_path: Path,
) -> None:
    """Per-file digests must not collide when bytes span former path delimiters.

    Production regression for PRRT_kwDOSJAM6s6eRK93: concatenating path\\0bytes\\0
    across files let (a=X, b=Y+"\\0b\\0"+Z) hash identically to (a=X+"\\0b\\0"+Y, b=Z).
    """
    worktree = tmp_path / "ws_untracked_boundary"
    worktree.mkdir()
    file_a = worktree / "a"
    file_b = worktree / "b"
    x = b"X"
    y = b"Y"
    z = b"Z"
    boundary = b"\0b\0"

    file_a.write_bytes(x)
    file_b.write_bytes(y + boundary + z)
    split_fp = comment_verdict_residue._hash_untracked_residue_paths(
        worktree_path=worktree,
        paths=["a", "b"],
        untracked={"a", "b"},
    )

    file_a.write_bytes(x + boundary + y)
    file_b.write_bytes(z)
    merged_fp = comment_verdict_residue._hash_untracked_residue_paths(
        worktree_path=worktree,
        paths=["a", "b"],
        untracked={"a", "b"},
    )

    assert split_fp is not None and merged_fp is not None
    assert split_fp != merged_fp


@pytest.mark.unit
def test_hash_untracked_residue_paths_distinguishes_regular_file_from_symlink(
    tmp_path: Path,
) -> None:
    """Regular-file bytes must not collide with symlink link-text fingerprints.

    Production regression for PRRT_kwDOSJAM6s6eRq4q: a regular file whose
    contents are ``symlink:foo`` hashed identically to a symlink pointing at
    ``foo``, letting correction residue attribution accept a non-FIXED verdict
    after rollback.
    """
    worktree = tmp_path / "ws_untracked_regular_symlink"
    worktree.mkdir()
    (worktree / "src").mkdir()
    path = "src/link"
    candidate = worktree / "src" / "link"

    candidate.write_bytes(b"symlink:foo")
    regular_fp = comment_verdict_residue._hash_untracked_residue_paths(
        worktree_path=worktree,
        paths=[path],
        untracked={path},
    )

    candidate.unlink()
    candidate.symlink_to("foo")
    symlink_fp = comment_verdict_residue._hash_untracked_residue_paths(
        worktree_path=worktree,
        paths=[path],
        untracked={path},
    )

    assert regular_fp is not None and symlink_fp is not None
    assert regular_fp != symlink_fp


@pytest.mark.unit
async def test_correction_residue_fingerprint_tracked_symlink_identity_not_target(
    tmp_path: Path,
) -> None:
    """Tracked worktree symlinks must be fingerprinted via readlink, never followed.

    ``git hash-object --path`` follows symlinks; a correction that typechanges a
    dirty tracked file to ``/dev/zero`` would hang the residue probe (5081034196).
    """
    worktree = tmp_path / "ws_tracked_symlink_residue"
    worktree.mkdir()
    _init_git_worktree(worktree)
    target = worktree / "src" / "x.py"
    dest_a = worktree / "target_a.txt"
    dest_b = worktree / "target_b.txt"
    dest_a.write_text("shared-payload\n", encoding="utf-8")
    dest_b.write_text("shared-payload\n", encoding="utf-8")

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout=" T src/x.py\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    target.write_text("base\n-edited\n", encoding="utf-8")
    content_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_tracked_symlink_residue",
        worktree_path=worktree,
    )

    target.unlink()
    target.symlink_to(dest_a)
    dest_a_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_tracked_symlink_residue",
        worktree_path=worktree,
    )
    target.unlink()
    target.symlink_to(dest_b)
    dest_b_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_tracked_symlink_residue",
        worktree_path=worktree,
    )

    assert content_fp is not None and content_fp != ""
    assert dest_a_fp is not None and dest_b_fp is not None
    assert content_fp != dest_a_fp
    assert dest_a_fp != dest_b_fp

    target.unlink()
    target.symlink_to("/dev/zero")
    zero_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_tracked_symlink_residue",
        worktree_path=worktree,
    )
    assert zero_fp is not None and zero_fp != ""
    assert zero_fp != dest_a_fp


@pytest.mark.unit
async def test_correction_residue_fingerprint_unreadable_tracked_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreadable tracked worktree blob must fail closed, not hash a collision-prone marker.

    Production regression for PRRT_kwDOSJAM6s6ePBHr: when attempt 0 leaves a dirty tracked
    file the control-plane user cannot read, ``git status`` still reports it while
    ``_git_worktree_blob_sha`` returns None; hashing ``<missing>`` collides before and
    after a correction edits the file but cannot stage it.
    """
    worktree = tmp_path / "ws_unreadable_tracked"
    worktree.mkdir()
    _init_git_worktree(worktree)
    target = worktree / "src" / "x.py"
    target.write_text("base\n-edited\n", encoding="utf-8")

    real_blob_sha = comment_verdict_residue._git_worktree_blob_sha

    def _unreadable_blob(**kwargs: object) -> str | None:
        path = kwargs.get("path")
        if path == "src/x.py":
            return None
        return real_blob_sha(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(comment_verdict_residue, "_git_worktree_blob_sha", _unreadable_blob)

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout=" M src/x.py\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_unreadable_tracked",
        worktree_path=worktree,
    )
    assert start_fp is None

    target.write_text("base\n-correction\n", encoding="utf-8")
    correction_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_unreadable_tracked",
        worktree_path=worktree,
    )
    assert correction_fp is None
    assert comment_verdict_residue._correction_authored_mutation_vs_start(
        attempt_start_head="abc123",
        pre_sink_head="abc123",
        correction_start_residue_fp=start_fp,
        pre_sink_residue_fp=correction_fp,
    )


@pytest.mark.unit
async def test_correction_unreadable_baseline_rejects_clean_post_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreadable correction-start must fail closed when post-sink reads clean.

    Production regression for PRRT_kwDOSJAM6s6eU900: when attempt-0 leaves unreadable
    dirty residue, a correction that removes it yields an empty pre-sink fingerprint;
    without fail-closed baseline handling, non-FIXED verdicts could resolve after rollback.
    """
    worktree = tmp_path / "ws_unreadable_baseline_clean_post"
    worktree.mkdir()
    _init_git_worktree(worktree)
    target = worktree / "src" / "x.py"
    target.write_text("base\n-edited\n", encoding="utf-8")

    real_blob_sha = comment_verdict_residue._git_worktree_blob_sha
    call_count = {"n": 0}

    def _unreadable_then_clean_blob(**kwargs: object) -> str | None:
        path = kwargs.get("path")
        if path == "src/x.py":
            call_count["n"] += 1
            if call_count["n"] <= 1:
                return None
        return real_blob_sha(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        comment_verdict_residue, "_git_worktree_blob_sha", _unreadable_then_clean_blob
    )

    status_outputs = iter([" M src/x.py\n", ""])

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout=next(status_outputs), stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_unreadable_baseline_clean_post",
        worktree_path=worktree,
    )
    assert start_fp is None

    target.write_text("base\n", encoding="utf-8")
    pre_sink_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_unreadable_baseline_clean_post",
        worktree_path=worktree,
    )
    assert pre_sink_fp == ""
    assert comment_verdict_residue._correction_authored_mutation_vs_start(
        attempt_start_head="abc123",
        pre_sink_head="abc123",
        correction_start_residue_fp=start_fp,
        pre_sink_residue_fp=pre_sink_fp,
    )
    assert comment_verdict_residue._stranded_residue_is_correction_mutation(
        correction_start_residue_fp=start_fp,
        post_residue_fp=pre_sink_fp,
    )


@pytest.mark.unit
async def test_correction_residue_fingerprint_stat_failure_not_misclassified_as_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Permission/stat failures must not fingerprint as tracked deletions.

    Bugbot review 5082437263: ``exists()`` returns False for ENOENT and for
    permission errors alike. When ``hash-object`` fails on an indexed but
    unreadable path, both correction probes must fail closed rather than share
    a stable ``<deleted>`` marker.
    """
    worktree = tmp_path / "ws_stat_failure_not_deletion"
    worktree.mkdir()
    _init_git_worktree(worktree)
    target = worktree / "src" / "x.py"
    target.write_text("base\n-edited\n", encoding="utf-8")

    real_blob_sha = comment_verdict_residue._git_worktree_blob_sha
    real_lstat = Path.lstat

    def _unreadable_blob(**kwargs: object) -> str | None:
        path = kwargs.get("path")
        if path == "src/x.py":
            return None
        return real_blob_sha(**kwargs)  # type: ignore[arg-type]

    def _permission_denied_lstat(self: Path) -> object:
        if self == target:
            raise PermissionError(13, "Permission denied", str(self))
        return real_lstat(self)

    monkeypatch.setattr(comment_verdict_residue, "_git_worktree_blob_sha", _unreadable_blob)
    monkeypatch.setattr(Path, "lstat", _permission_denied_lstat)

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout=" M src/x.py\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_stat_failure_not_deletion",
        worktree_path=worktree,
    )
    assert start_fp is None

    target.write_text("base\n-correction\n", encoding="utf-8")
    correction_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_stat_failure_not_deletion",
        worktree_path=worktree,
    )
    assert correction_fp is None
    assert comment_verdict_residue._correction_authored_mutation_vs_start(
        attempt_start_head="abc123",
        pre_sink_head="abc123",
        correction_start_residue_fp=start_fp,
        pre_sink_residue_fp=correction_fp,
    )


@pytest.mark.unit
async def test_correction_residue_fingerprint_tracked_deletion_is_fingerprintable(
    tmp_path: Path,
) -> None:
    """Tracked worktree deletions must fingerprint stably, not fail closed as unreadable.

    Production regression for PRRT_kwDOSJAM6s6eP-gA: ``git diff --name-only`` lists deleted
    paths while ``hash-object --path`` returns None, so identical attempt-0 delete residue
    must not poison correction attribution.
    """
    worktree = tmp_path / "ws_tracked_deletion"
    worktree.mkdir()
    _init_git_worktree(worktree)
    target = worktree / "src" / "x.py"
    target.unlink()

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout=" D src/x.py\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_tracked_deletion",
        worktree_path=worktree,
    )
    repeat_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_tracked_deletion",
        worktree_path=worktree,
    )

    assert start_fp is not None and start_fp != ""
    assert start_fp == repeat_fp
    assert not comment_verdict_residue._correction_authored_mutation_vs_start(
        attempt_start_head="abc123",
        pre_sink_head="abc123",
        correction_start_residue_fp=start_fp,
        pre_sink_residue_fp=repeat_fp,
    )


@pytest.mark.unit
async def test_correction_residue_fingerprint_gitlink_inner_uncommitted_edit_changes_fp(
    tmp_path: Path,
) -> None:
    """Uncommitted edits inside a submodule must change the gitlink fingerprint.

    Production regression for PRRT_kwDOSJAM6s6eR-GB: HEAD-only gitlink identity collides
    when attempt 0 leaves inner dirty files and the correction rewrites them without
    committing a new submodule SHA.
    """
    worktree = tmp_path / "ws_gitlink_inner_dirty"
    worktree.mkdir()
    subprocess.run(["git", "init"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    submodule = worktree / "sub"
    submodule.mkdir()
    subprocess.run(["git", "init"], cwd=submodule, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=submodule,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=submodule,
        check=True,
        capture_output=True,
    )
    (submodule / "file.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=submodule, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "sub init"], cwd=submodule, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "submodule", "add", "./sub", "sub"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "add sub"], cwd=worktree, check=True, capture_output=True
    )
    (submodule / "file.txt").write_text("attempt0\n", encoding="utf-8")

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout=" M sub\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_gitlink_inner_dirty",
        worktree_path=worktree,
    )
    (submodule / "file.txt").write_text("correction\n", encoding="utf-8")
    correction_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_gitlink_inner_dirty",
        worktree_path=worktree,
    )

    assert start_fp is not None and start_fp != ""
    assert correction_fp is not None and correction_fp != ""
    assert start_fp != correction_fp
    assert comment_verdict_residue._correction_authored_mutation_vs_start(
        attempt_start_head="abc123",
        pre_sink_head="abc123",
        correction_start_residue_fp=start_fp,
        pre_sink_residue_fp=correction_fp,
    )


@pytest.mark.unit
async def test_correction_residue_fingerprint_dirty_gitlink_is_fingerprintable(
    tmp_path: Path,
) -> None:
    """Dirty tracked submodules must fingerprint via checked-out HEAD, not fail closed.

    Production regression for PRRT_kwDOSJAM6s6eRyfx: ``hash-object --path`` cannot hash
    gitlink directories, so identical attempt-0 submodule residue must not poison correction
    attribution when the correction makes no further change.
    """
    worktree = tmp_path / "ws_dirty_gitlink"
    worktree.mkdir()
    _init_git_worktree_with_dirty_submodule(worktree)

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout=" M sub\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_dirty_gitlink",
        worktree_path=worktree,
    )
    repeat_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_dirty_gitlink",
        worktree_path=worktree,
    )

    assert start_fp is not None and start_fp != ""
    assert start_fp == repeat_fp
    assert not comment_verdict_residue._correction_authored_mutation_vs_start(
        attempt_start_head="abc123",
        pre_sink_head="abc123",
        correction_start_residue_fp=start_fp,
        pre_sink_residue_fp=repeat_fp,
    )


@pytest.mark.unit
async def test_correction_residue_fingerprint_dirty_gitlink_with_ignore_submodules_config(
    tmp_path: Path,
) -> None:
    """Submodule residue must be detected even when ``diff.ignoreSubmodules=all``.

    Production regression for PRRT_kwDOSJAM6s6eSzCJ: repo config suppresses gitlink
    changes from ``git status``/``git diff`` unless probes pass
    ``--ignore-submodules=none``.
    """
    worktree = tmp_path / "ws_dirty_gitlink_ignore_submodules"
    worktree.mkdir()
    _init_git_worktree_with_dirty_submodule(worktree)
    subprocess.run(
        ["git", "config", "diff.ignoreSubmodules", "all"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        proc = subprocess.run(cmd, capture_output=True, check=False)
        return CommandResult(
            returncode=proc.returncode,
            stdout=proc.stdout.decode("utf-8", errors="replace"),
            stderr=proc.stderr.decode("utf-8", errors="replace"),
        )

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_dirty_gitlink_ignore_submodules",
        worktree_path=worktree,
    )
    repeat_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_dirty_gitlink_ignore_submodules",
        worktree_path=worktree,
    )

    assert start_fp is not None and start_fp != ""
    assert start_fp == repeat_fp
    assert not comment_verdict_residue._correction_authored_mutation_vs_start(
        attempt_start_head="abc123",
        pre_sink_head="abc123",
        correction_start_residue_fp=start_fp,
        pre_sink_residue_fp=repeat_fp,
    )


@pytest.mark.unit
async def test_correction_residue_fingerprint_unreadable_untracked_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreadable untracked residue must fail closed, not hash a collision-prone marker.

    Production regression for PRRT_kwDOSJAM6s6eN7wf: when attempt 0 leaves a mode-000
    untracked file and the correction rewrites it while permissions stay unreadable,
    both probes would hash identical ``<missing>`` markers and accept a non-FIXED verdict.
    """
    worktree = tmp_path / "ws_unreadable_untracked"
    worktree.mkdir()
    (worktree / "src").mkdir()
    target = worktree / "src" / "secret.py"
    target.write_text("attempt0\n", encoding="utf-8")

    real_open = Path.open

    def _permission_denied_open(self: Path, *args: object, **kwargs: object) -> object:
        if self == target and args and args[0] == "rb":
            raise PermissionError(13, "Permission denied", str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _permission_denied_open)

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout="?? src/secret.py\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_unreadable_untracked",
        worktree_path=worktree,
    )
    assert start_fp is None

    target.write_text("correction\n", encoding="utf-8")
    correction_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_unreadable_untracked",
        worktree_path=worktree,
    )
    assert correction_fp is None
    assert comment_verdict_residue._correction_authored_mutation_vs_start(
        attempt_start_head="abc123",
        pre_sink_head="abc123",
        correction_start_residue_fp=start_fp,
        pre_sink_residue_fp=correction_fp,
    )


@pytest.mark.unit
def test_git_index_blob_sha_returns_none_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()

    def _fail(**_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=(), returncode=1, stdout=b"", stderr=b"")

    monkeypatch.setattr(comment_verdict_residue, "_run_git_bytes", _fail)
    assert (
        comment_verdict_residue._git_index_blob_sha(
            worktree_path=worktree,
            path="src/x.py",
            git_env={},
        )
        is None
    )


@pytest.mark.unit
def test_git_index_blob_sha_resolves_stage_like_filenames(
    tmp_path: Path,
) -> None:
    """Filenames like ``0:x`` must not be parsed as Git's ``:<stage>:<path>`` syntax.

    Production regression for PRRT_kwDOSJAM6s6eQcs6: ``:{path}`` for ``0:x`` becomes
    ``:0:x`` (stage 0 of ``x``), returning None and colliding fingerprints.
    """
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _init_git_worktree(worktree)
    stage_like = worktree / "0:x"
    stage_like.write_text("stage-like\n", encoding="utf-8")
    subprocess.run(["git", "add", "0:x"], cwd=worktree, check=True, capture_output=True)
    expected = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", ":0:./0:x"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert (
        comment_verdict_residue._git_index_blob_sha(
            worktree_path=worktree,
            path="0:x",
            git_env={},
        )
        == expected
    )


@pytest.mark.unit
def test_git_submodule_worktree_commit_returns_none_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "sub").mkdir()

    def _fail(**_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=(), returncode=1, stdout=b"", stderr=b"")

    monkeypatch.setattr(comment_verdict_residue, "_run_git_bytes", _fail)
    assert (
        comment_verdict_residue._git_submodule_worktree_commit(
            worktree_path=worktree,
            path="sub",
            git_env={},
        )
        is None
    )


@pytest.mark.unit
def test_git_submodule_worktree_commit_oserror_on_git_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "sub").mkdir()
    real_exists = Path.exists

    def _exists(self: Path) -> bool:
        if self.name == ".git" and self.parent.name == "sub":
            raise OSError(errno.EACCES, "denied")
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", _exists)
    assert (
        comment_verdict_residue._git_submodule_worktree_commit(
            worktree_path=worktree,
            path="sub",
            git_env={},
        )
        is None
    )


@pytest.mark.unit
def test_git_submodule_worktree_commit_rev_parse_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _init_git_worktree_with_dirty_submodule(worktree)
    real_run = comment_verdict_residue._run_git_bytes

    def _run(**kwargs: object) -> subprocess.CompletedProcess[bytes]:
        args = kwargs.get("args", ())
        if args and args[0] == "rev-parse":
            return subprocess.CompletedProcess(args=(), returncode=1, stdout=b"", stderr=b"err")
        return real_run(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(comment_verdict_residue, "_run_git_bytes", _run)
    assert (
        comment_verdict_residue._git_submodule_worktree_commit(
            worktree_path=worktree,
            path="sub",
            git_env={},
        )
        is None
    )


@pytest.mark.unit
def test_git_submodule_worktree_commit_empty_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _init_git_worktree_with_dirty_submodule(worktree)
    real_run = comment_verdict_residue._run_git_bytes

    def _run(**kwargs: object) -> subprocess.CompletedProcess[bytes]:
        args = kwargs.get("args", ())
        if args and args[0] == "rev-parse":
            return subprocess.CompletedProcess(args=(), returncode=0, stdout=b"  \n", stderr=b"")
        return real_run(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(comment_verdict_residue, "_run_git_bytes", _run)
    assert (
        comment_verdict_residue._git_submodule_worktree_commit(
            worktree_path=worktree,
            path="sub",
            git_env={},
        )
        is None
    )


@pytest.mark.unit
def test_git_submodule_worktree_commit_inner_residue_probe_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _init_git_worktree_with_dirty_submodule(worktree)

    def _fail_inner(**_kwargs: object) -> tuple[str | None, str | None]:
        return None, None

    monkeypatch.setattr(
        comment_verdict_residue,
        "_hash_tracked_residue_staged_and_unstaged",
        _fail_inner,
    )
    assert (
        comment_verdict_residue._git_submodule_worktree_commit(
            worktree_path=worktree,
            path="sub",
            git_env={},
        )
        is None
    )


@pytest.mark.unit
def test_git_submodule_worktree_commit_status_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _init_git_worktree_with_dirty_submodule(worktree)
    real_run = comment_verdict_residue._run_git_bytes

    def _run(**kwargs: object) -> subprocess.CompletedProcess[bytes]:
        args = kwargs.get("args", ())
        if args and "status" in args:
            return subprocess.CompletedProcess(args=(), returncode=1, stdout=b"", stderr=b"err")
        return real_run(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(comment_verdict_residue, "_run_git_bytes", _run)
    assert (
        comment_verdict_residue._git_submodule_worktree_commit(
            worktree_path=worktree,
            path="sub",
            git_env={},
        )
        is None
    )


@pytest.mark.unit
def test_git_submodule_worktree_commit_hashes_inner_untracked(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _init_git_worktree_with_dirty_submodule(worktree)
    (worktree / "sub" / "new_untracked.py").write_text("payload\n", encoding="utf-8")

    baseline = comment_verdict_residue._git_submodule_worktree_commit(
        worktree_path=worktree,
        path="sub",
        git_env={},
    )
    (worktree / "sub" / "new_untracked.py").write_text("changed\n", encoding="utf-8")
    changed = comment_verdict_residue._git_submodule_worktree_commit(
        worktree_path=worktree,
        path="sub",
        git_env={},
    )

    assert baseline is not None and baseline != ""
    assert changed is not None and changed != ""
    assert baseline != changed


@pytest.mark.unit
def test_hash_tracked_residue_diffs_gitlink_submodule_commit_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _init_git_worktree_with_dirty_submodule(worktree)
    monkeypatch.setattr(
        comment_verdict_residue,
        "_git_submodule_worktree_commit",
        lambda **_kwargs: None,
    )
    assert (
        comment_verdict_residue._hash_tracked_residue_diffs(
            worktree_path=worktree,
            git_env={},
            cached=False,
        )
        is None
    )


@pytest.mark.unit
def test_hash_tracked_residue_diffs_unreadable_worktree_blob(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _init_git_worktree(worktree)
    target = worktree / "src" / "x.py"
    target.write_text("dirty\n", encoding="utf-8")

    monkeypatch.setattr(
        comment_verdict_residue,
        "_git_worktree_blob_sha",
        lambda **_kwargs: None,
    )
    assert (
        comment_verdict_residue._hash_tracked_residue_diffs(
            worktree_path=worktree,
            git_env={},
            cached=False,
        )
        is None
    )


@pytest.mark.unit
def test_hash_tracked_residue_diffs_missing_index_blob_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _init_git_worktree(worktree)
    (worktree / "src" / "x.py").write_text("dirty\n", encoding="utf-8")

    monkeypatch.setattr(
        comment_verdict_residue,
        "_git_worktree_blob_sha",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        comment_verdict_residue,
        "_git_index_blob_sha",
        lambda **_kwargs: None,
    )
    assert (
        comment_verdict_residue._hash_tracked_residue_diffs(
            worktree_path=worktree,
            git_env={},
            cached=False,
        )
        is None
    )


@pytest.mark.unit
def test_git_submodule_worktree_commit_unreadable_inner_untracked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _init_git_worktree_with_dirty_submodule(worktree)
    unreadable = worktree / "sub" / "secret.txt"
    unreadable.write_text("secret\n", encoding="utf-8")

    real_open = Path.open

    def _permission_denied_open(self: Path, *args: object, **kwargs: object) -> object:
        if self == unreadable and args and args[0] == "rb":
            raise PermissionError(13, "Permission denied", str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _permission_denied_open)
    assert (
        comment_verdict_residue._git_submodule_worktree_commit(
            worktree_path=worktree,
            path="sub",
            git_env={},
        )
        is None
    )


@pytest.mark.unit
def test_git_worktree_blob_sha_symlink_hash_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "src").mkdir()
    target = worktree / "target.txt"
    target.write_text("payload\n", encoding="utf-8")
    symlink = worktree / "src" / "link"
    symlink.symlink_to(target)

    real_run = comment_verdict_residue._run_git_bytes

    def _fail_stdin(**kwargs: object) -> subprocess.CompletedProcess[bytes]:
        args = kwargs.get("args", ())
        if args and "hash-object" in args and "--stdin" in args:
            return subprocess.CompletedProcess(args=(), returncode=1, stdout=b"", stderr=b"")
        return real_run(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(comment_verdict_residue, "_run_git_bytes", _fail_stdin)
    assert (
        comment_verdict_residue._git_worktree_blob_sha(
            worktree_path=worktree,
            path="src/link",
            git_env={},
        )
        is None
    )


@pytest.mark.unit
def test_git_worktree_blob_sha_readlink_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    link_path = worktree / "src" / "link"
    link_path.parent.mkdir(parents=True)
    link_path.symlink_to("target")

    real_readlink = Path.readlink

    def _raise_readlink(self: Path) -> Path:
        if self == link_path:
            raise OSError("readlink failed")
        return real_readlink(self)

    monkeypatch.setattr(Path, "readlink", _raise_readlink)
    assert (
        comment_verdict_residue._git_worktree_blob_sha(
            worktree_path=worktree,
            path="src/link",
            git_env={},
        )
        is None
    )


@pytest.mark.unit
def test_git_worktree_blob_sha_regular_file_hash_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "src").mkdir()
    (worktree / "src" / "x.py").write_text("payload\n", encoding="utf-8")

    def _fail(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=(), returncode=1, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _fail)
    assert (
        comment_verdict_residue._git_worktree_blob_sha(
            worktree_path=worktree,
            path="src/x.py",
            git_env={},
        )
        is None
    )


@pytest.mark.unit
def test_git_worktree_blob_sha_regular_file_avoids_path_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production regression for PRRT_kwDOSJAM6s6eSHjC: clean filters must not run during hash."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _init_git_worktree(worktree)
    target = worktree / "src" / "x.py"
    target.write_text("edited\n", encoding="utf-8")

    captured: list[tuple[str, ...]] = []
    real_run = subprocess.run

    def _capture(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if args and "hash-object" in args[0]:
            captured.append(tuple(args[0][-2:]))  # type: ignore[arg-type]
        return real_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "run", _capture)
    sha = comment_verdict_residue._git_worktree_blob_sha(
        worktree_path=worktree,
        path="src/x.py",
        git_env={},
    )
    assert sha is not None
    assert captured == [("hash-object", "--stdin")]


@pytest.mark.unit
def test_git_worktree_blob_sha_regular_file_streams_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6eSPQL: pass a file handle to hash-object instead of fh.read()."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _init_git_worktree(worktree)
    target = worktree / "src" / "x.py"
    payload = b"x" * 131072
    target.write_bytes(payload)

    captured_stdin: list[object] = []
    real_run = subprocess.run

    def _capture_stdin(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured_stdin.append(kwargs.get("stdin"))
        return real_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "run", _capture_stdin)
    sha = comment_verdict_residue._git_worktree_blob_sha(
        worktree_path=worktree,
        path="src/x.py",
        git_env={},
    )
    assert sha is not None
    assert len(captured_stdin) == 1
    stdin_obj = captured_stdin[0]
    assert hasattr(stdin_obj, "read")
    expected = (
        subprocess.run(
            ["git", "hash-object", "--stdin"],
            input=payload,
            capture_output=True,
            check=True,
            cwd=worktree,
        )
        .stdout.decode()
        .strip()
    )
    assert sha == expected


@pytest.mark.unit
def test_git_index_mode_returns_none_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()

    def _fail(**_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=(), returncode=1, stdout=b"", stderr=b"")

    monkeypatch.setattr(comment_verdict_residue, "_run_git_bytes", _fail)
    assert (
        comment_verdict_residue._git_index_mode(
            worktree_path=worktree,
            path="src/x.py",
            git_env={},
        )
        is None
    )


@pytest.mark.unit
def test_git_index_mode_returns_none_on_empty_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()

    def _empty(**_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=(), returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(comment_verdict_residue, "_run_git_bytes", _empty)
    assert (
        comment_verdict_residue._git_index_mode(
            worktree_path=worktree,
            path="src/x.py",
            git_env={},
        )
        is None
    )


@pytest.mark.unit
def test_git_worktree_mode_lstat_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    target = worktree / "src" / "x.py"
    target.parent.mkdir(parents=True)
    target.write_text("payload\n", encoding="utf-8")

    real_lstat = Path.lstat

    def _raise_lstat(self: Path, *args: object, **kwargs: object) -> object:
        if self == target:
            raise OSError("lstat failed")
        return real_lstat(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "lstat", _raise_lstat)
    assert (
        comment_verdict_residue._git_worktree_mode(
            worktree_path=worktree,
            path="src/x.py",
        )
        is None
    )


@pytest.mark.unit
def test_git_worktree_mode_non_regular_returns_none(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "src").mkdir()
    assert (
        comment_verdict_residue._git_worktree_mode(
            worktree_path=worktree,
            path="src",
        )
        is None
    )


@pytest.mark.unit
def test_hash_tracked_residue_diffs_protected_scope_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError

    worktree = tmp_path / "wt"
    worktree.mkdir()

    def _name_only(**_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=(),
            returncode=0,
            stdout=b"protected/path\x00",
            stderr=b"",
        )

    def _protected_scope(_stdout: bytes) -> list[str]:
        raise ProtectedScopeDiffError("blocked")

    monkeypatch.setattr(comment_verdict_residue, "_run_git_bytes", _name_only)
    monkeypatch.setattr(
        comment_verdict_residue,
        "_changed_paths_from_name_only_z",
        _protected_scope,
    )
    assert (
        comment_verdict_residue._hash_tracked_residue_diffs(
            worktree_path=worktree,
            git_env={},
            cached=False,
        )
        is None
    )
