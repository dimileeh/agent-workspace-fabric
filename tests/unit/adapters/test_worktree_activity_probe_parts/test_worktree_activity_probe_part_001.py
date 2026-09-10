"""Worktree activity probe: tree changes and linked-git-dir watching (issue #932).

"Liveness" for a print-mode agent is "the worktree moved", so the probe answers
one question: has anything under the workspace worktree changed since the last
probe? It excludes nothing the agent could legitimately write (``.git``
included) and, because a linked worktree keeps HEAD/index outside the worktree,
it also watches the resolved git dir's ``HEAD`` / ``index`` / ``logs/HEAD``.

Split out of ``tests/unit/adapters/test_worktree_activity_probe.py`` to keep
each test module under the first-party 1500-line maintainability guardrail.
Continued in ``test_worktree_activity_probe_part_002`` and
``test_worktree_activity_probe_part_003``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
import structlog

from awf.adapters import worktree_activity
from awf.adapters.worktree_activity import WorktreeActivityProbe
from tests.unit.adapters.test_worktree_activity_probe_parts.helpers import _age, _age_tree


@pytest.mark.unit
async def test_quiet_worktree_reports_no_activity(worktree: Path) -> None:
    probe = WorktreeActivityProbe(worktree)
    await probe.prime()

    assert await probe() is False
    assert await probe() is False


@pytest.mark.unit
async def test_modified_file_reports_activity_once(worktree: Path) -> None:
    """A single change is reported once; the baseline then advances."""
    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    (worktree / "README.md").write_text("hello again\n", encoding="utf-8")

    assert await probe() is True
    assert await probe() is False


@pytest.mark.unit
async def test_future_dated_entry_does_not_blind_later_activity(worktree: Path) -> None:
    """A single future-stamped entry must not become a floor no write can clear.

    Tracking the tree as one maximum timestamp meant the first probe adopted the
    future stamp; every later write, stamped with the current clock, landed below
    it and read as idleness — one spurious extension, then an idle kill on a run
    that was still working. That is the #932 defect again.
    """
    future = worktree / "src" / "future.txt"
    future.write_text("from the future\n", encoding="utf-8")
    ahead = time.time() + 3600.0
    os.utime(future, (ahead, ahead))

    probe = WorktreeActivityProbe(worktree)
    assert await probe() is True

    for index in range(3):
        # Vary size as well as content so the test isolates the future-mtime
        # regression from a filesystem's ctime clock granularity.
        suffix = "!" * index
        (worktree / "README.md").write_text(
            f"hello {index}{suffix}\n",
            encoding="utf-8",
        )
        assert await probe() is True
        assert await probe() is False


@pytest.mark.unit
async def test_created_file_in_nested_directory_reports_activity(worktree: Path) -> None:
    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    (worktree / "src" / "nested" / "new_module.py").write_text("y = 2\n", encoding="utf-8")

    assert await probe() is True


@pytest.mark.unit
async def test_permission_change_reports_activity(worktree: Path) -> None:
    """``chmod +x`` moves no timestamp, size or inode — but Git sees the mode."""
    script = worktree / "script.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    _age(script)
    _age(worktree)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    script.chmod(script.stat().st_mode | 0o111)

    assert await probe() is True


@pytest.mark.unit
async def test_timestamp_preserving_rewrite_reports_activity(worktree: Path) -> None:
    """A same-length in-place rewrite with the mtime restored still changed the file.

    A timestamp-preserving formatter (or ``rsync --inplace --times``) leaves the
    path, mtime, size, inode and mode all identical while Git sees new content.
    ``st_ctime_ns`` is what moves — and the process cannot put it back — so it
    belongs in the fingerprint; without it a silent agent doing only such edits
    gets no extension and is killed at the idle deadline.
    """
    target = worktree / "README.md"
    _age(target)
    _age(worktree)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    before = target.stat()
    deadline = time.monotonic() + 1.0
    while True:
        with target.open("r+b") as handle:
            handle.write(b"HELLO\n")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = target.stat()
        if after.st_ctime_ns != before.st_ctime_ns:
            break
        if time.monotonic() >= deadline:
            pytest.fail("filesystem ctime did not advance after rewrite")
        time.sleep(0.001)
    assert (after.st_mtime_ns, after.st_size, after.st_ino, after.st_mode) == (
        before.st_mtime_ns,
        before.st_size,
        before.st_ino,
        before.st_mode,
    )

    assert await probe() is True


@pytest.mark.unit
async def test_deleted_file_reports_activity(worktree: Path) -> None:
    """A delete only bumps the containing directory's mtime — dirs are stat-ed too."""
    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    (worktree / "src" / "nested" / "module.py").unlink()

    assert await probe() is True


@pytest.mark.unit
async def test_linked_worktree_git_dir_head_and_index_are_watched(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """Only Git state moved: the gitfile-resolved git dir must still count."""
    git_dir = tmp_path / "mirror.git" / "worktrees" / "ws_probe"
    (git_dir / "logs").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/awf/ws\n", encoding="utf-8")
    (git_dir / "index").write_bytes(b"DIRC")
    (git_dir / "logs" / "HEAD").write_text("reflog\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(git_dir)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    (git_dir / "index").write_bytes(b"DIRC-updated")
    assert await probe() is True

    (git_dir / "HEAD").write_text("ref: refs/heads/other\n", encoding="utf-8")
    assert await probe() is True

    (git_dir / "logs" / "HEAD").write_text("reflog\nmore\n", encoding="utf-8")
    assert await probe() is True


def _linked_git_dir(tmp_path: Path, worktree: Path, *, head: str) -> Path:
    """Wire ``worktree`` up as a linked worktree of ``tmp_path/mirror.git``."""
    git_dir = tmp_path / "mirror.git" / "worktrees" / "ws_probe"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text(head, encoding="utf-8")
    (git_dir / "index").write_bytes(b"DIRC")
    (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    return git_dir


@pytest.mark.unit
async def test_commit_moving_only_the_branch_ref_reports_activity(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """A commit lands in ``refs/heads/<branch>`` under the *common* git dir.

    With ``core.logAllRefUpdates=false`` no reflog is written, the linked
    worktree's ``HEAD`` keeps naming the same branch, and an index that already
    matched the previous probe (staged during the preceding idle window) does
    not move either — so the three per-worktree files alone would report the
    commit as idleness and the watchdog would kill the agent moments after it
    committed.
    """
    git_dir = _linked_git_dir(tmp_path, worktree, head="ref: refs/heads/awf/ws\n")
    branch_ref = tmp_path / "mirror.git" / "refs" / "heads" / "awf" / "ws"
    branch_ref.parent.mkdir(parents=True)
    branch_ref.write_text("0" * 40 + "\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(tmp_path / "mirror.git")

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    branch_ref.write_text("1" * 40 + "\n", encoding="utf-8")
    assert await probe() is True
    assert (git_dir / "index").read_bytes() == b"DIRC"


@pytest.mark.unit
async def test_packed_branch_ref_absence_stays_a_complete_observation(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """A packed ref has no loose file, and its later appearance is the commit."""
    _linked_git_dir(tmp_path, worktree, head="ref: refs/heads/awf/ws\n")
    _age_tree(worktree)
    _age_tree(tmp_path / "mirror.git")

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    branch_ref = tmp_path / "mirror.git" / "refs" / "heads" / "awf" / "ws"
    branch_ref.parent.mkdir(parents=True)
    branch_ref.write_text("1" * 40 + "\n", encoding="utf-8")
    assert await probe() is True


@pytest.mark.unit
async def test_reftable_commit_moving_only_the_stack_reports_activity(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """Under ``extensions.refStorage=reftable`` there is no loose ref to watch.

    HEAD is a stub naming ``refs/heads/.invalid``, so the resolved branch ref
    never exists and its absence is a complete observation; the ``logs/HEAD``
    reflog and the already-staged index do not move either. This worktree's
    *own* stack does: refs and reflogs that belong to one worktree — HEAD's
    among them — live in ``$GIT_DIR/reftable``, so a commit here rewrites it.
    """
    git_dir = _linked_git_dir(tmp_path, worktree, head="ref: refs/heads/.invalid\n")
    stack = git_dir / "reftable" / "tables.list"
    stack.parent.mkdir(parents=True)
    stack.write_text("0x000000000001.ref\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(tmp_path / "mirror.git")

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    stack.write_text("0x000000000001.ref\n0x000000000002.ref\n", encoding="utf-8")
    assert await probe() is True
    assert (git_dir / "index").read_bytes() == b"DIRC"


@pytest.mark.unit
async def test_fetch_moving_only_fetch_head_reports_activity(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """``git fetch --quiet`` moves nothing in the worktree and no watched ref.

    Its objects and remote-tracking refs land in the shared common dir, but
    ``FETCH_HEAD`` is per-worktree and every fetch rewrites it — so it is the
    one path in this worktree's own Git state that a quiet fetch is guaranteed
    to move. Left out, an interval whose only work was a fetch reads as
    idleness and the watchdog kills an agent that was doing repository work.
    """
    git_dir = _linked_git_dir(tmp_path, worktree, head="ref: refs/heads/awf/ws\n")
    fetch_head = git_dir / "FETCH_HEAD"
    fetch_head.write_text("0" * 40 + "\t\tbranch 'main' of origin\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(tmp_path / "mirror.git")

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    # Rewritten in place: the git dir's own mtime does not move, so only the
    # ``FETCH_HEAD`` watch can see this.
    fetch_head.write_text("1" * 40 + "\t\tbranch 'main' of origin\n", encoding="utf-8")
    assert await probe() is True


@pytest.mark.unit
async def test_new_git_dir_metadata_file_reports_activity(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """The git dir itself is watched, so any metadata file appearing counts.

    ``ORIG_HEAD``, ``MERGE_HEAD``, ``COMMIT_EDITMSG``, a ``rebase-merge`` dir —
    per-worktree Git state with no watch of its own. Creating one moves the
    containing git dir's mtime, which is the cheap way to cover the whole set.
    """
    git_dir = _linked_git_dir(tmp_path, worktree, head="ref: refs/heads/awf/ws\n")
    _age_tree(worktree)
    _age_tree(tmp_path / "mirror.git")

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    (git_dir / "ORIG_HEAD").write_text("1" * 40 + "\n", encoding="utf-8")
    assert await probe() is True


@pytest.mark.unit
async def test_rebase_step_inside_the_linked_git_dir_reports_activity(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """A linked worktree's git dir is walked, not just stat-ed by name.

    Its own mtime moves only when a *direct child* appears or is replaced, so a
    rebase advancing through an already existing ``rebase-merge`` directory —
    ``msgnum`` rewritten in place, a step appended to ``done`` — moved nothing
    the named watches or that mtime could see, while the worktree itself sits
    still between ``git rebase --continue`` invocations. That interval read as
    idleness and the watchdog killed an agent mid-rebase.
    """
    git_dir = _linked_git_dir(tmp_path, worktree, head="ref: refs/heads/awf/ws\n")
    rebase_merge = git_dir / "rebase-merge"
    rebase_merge.mkdir()
    (rebase_merge / "msgnum").write_text("1\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(tmp_path / "mirror.git")

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    # Same length, in place: neither the git dir nor ``rebase-merge`` itself
    # moves, so only walking the git dir can see this.
    (rebase_merge / "msgnum").write_text("2\n", encoding="utf-8")
    assert await probe() is True


@pytest.mark.unit
async def test_vanished_linked_git_dir_still_allows_an_idle_answer(
    tmp_path: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A git dir gone by the time it is stat-ed is walked no further.

    Handing it to the walk anyway would turn its ``FileNotFoundError`` into
    "could not tell" on this scan and every later one — retiring the watchdog
    for the rest of the run — where absence is a complete observation the stat
    already folded in as a stable term.
    """
    git_dir = _linked_git_dir(tmp_path, worktree, head="ref: refs/heads/awf/ws\n")
    _age_tree(worktree)
    _age_tree(tmp_path / "mirror.git")

    real_lstat = Path.lstat

    def _pruned(self: Path) -> os.stat_result:
        if self == git_dir or git_dir in self.parents:
            raise FileNotFoundError(2, "no such file", str(self))
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", _pruned)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False


@pytest.mark.unit
async def test_shared_common_dir_churn_is_not_reported_as_activity(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """The common dir is never walked: its churn is other workspaces' agents.

    One bare mirror backs every worktree of a repo, so objects and remote refs
    landing there say nothing about *this* agent — walking it would report an
    idle run as alive whenever a neighbouring workspace fetched, and its object
    store would burn the walk budget. Only this worktree's own paths under it
    are watched by name.
    """
    _linked_git_dir(tmp_path, worktree, head="ref: refs/heads/awf/ws\n")
    common_dir = tmp_path / "mirror.git"
    (common_dir / "objects" / "pack").mkdir(parents=True)
    _age_tree(worktree)
    _age_tree(common_dir)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    (common_dir / "objects" / "pack" / "pack-neighbour.pack").write_bytes(b"PACK")
    (common_dir / "refs" / "remotes" / "origin").mkdir(parents=True)
    (common_dir / "refs" / "remotes" / "origin" / "main").write_text(
        "1" * 40 + "\n",
        encoding="utf-8",
    )
    assert await probe() is False


@pytest.mark.unit
async def test_shared_reftable_stack_churn_is_not_reported_as_activity(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """The common ``reftable/tables.list`` is repository-wide, not this worktree's.

    Under the reftable backend every ref transaction in *any* worktree of the
    repo — a neighbouring workspace committing, tagging, or AWF fetching into
    the shared mirror — rewrites that one file. Watching it would let unrelated
    parallel workspaces keep suppressing this idle agent's timeout until the
    wall cap, which is exactly the coupling the rest of the common dir is kept
    out of the scan to avoid.
    """
    _linked_git_dir(tmp_path, worktree, head="ref: refs/heads/.invalid\n")
    common_dir = tmp_path / "mirror.git"
    stack = common_dir / "reftable" / "tables.list"
    stack.parent.mkdir(parents=True)
    stack.write_text("0x000000000001.ref\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(common_dir)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    stack.write_text("0x000000000001.ref\n0x000000000002.ref\n", encoding="utf-8")
    assert await probe() is False


@pytest.mark.unit
async def test_absent_reftable_stack_stays_a_complete_observation(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """A files-backend repo has no ``reftable`` dir, and must still answer idle."""
    _linked_git_dir(tmp_path, worktree, head="ref: refs/heads/awf/ws\n")
    _age_tree(worktree)
    _age_tree(tmp_path / "mirror.git")

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False


@pytest.mark.unit
@pytest.mark.parametrize("head", ["1" * 40 + "\n", "ref:\n", "ref: refs/heads/awf/ws\n"])
async def test_head_without_a_resolvable_branch_ref_still_answers_idle(
    tmp_path: Path,
    worktree: Path,
    head: str,
) -> None:
    """A detached or empty HEAD names no ref — a complete observation, not ``None``.

    The last case keeps a *named* branch whose common dir cannot be reached
    through a ``commondir`` that is simply absent: the git dir is then the
    common one, exactly as for a plain checkout behind a ``.git`` symlink.
    """
    git_dir = tmp_path / "mirror.git" / "worktrees" / "ws_probe"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text(head, encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(tmp_path / "mirror.git")

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    (git_dir / "HEAD").write_text("ref: refs/heads/other\n", encoding="utf-8")
    assert await probe() is True


@pytest.mark.unit
@pytest.mark.parametrize("commondir", ["\n", "{absolute}\n"])
async def test_commondir_forms_resolve_to_the_same_branch_ref(
    tmp_path: Path,
    worktree: Path,
    commondir: str,
) -> None:
    """An empty ``commondir`` falls back to the git dir; an absolute one is used as is."""
    git_dir = tmp_path / "mirror.git" / "worktrees" / "ws_probe"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/awf/ws\n", encoding="utf-8")
    common = tmp_path / "mirror.git" if "{absolute}" in commondir else git_dir
    (git_dir / "commondir").write_text(
        commondir.format(absolute=tmp_path / "mirror.git"),
        encoding="utf-8",
    )
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(tmp_path / "mirror.git")

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    branch_ref = common / "refs" / "heads" / "awf" / "ws"
    branch_ref.parent.mkdir(parents=True)
    branch_ref.write_text("1" * 40 + "\n", encoding="utf-8")
    assert await probe() is True


@pytest.mark.unit
@pytest.mark.parametrize("denied_name", ["HEAD", "commondir"])
async def test_unreadable_ref_resolution_input_reports_unknown(
    tmp_path: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
    denied_name: str,
) -> None:
    """Which branch a commit lands in is unknowable, so the scan is incomplete.

    Falling back to "no branch ref" would drop the one path a reflog-less commit
    moves from an otherwise complete-looking fingerprint — an idle kill of an
    agent mid-commit.
    """
    git_dir = _linked_git_dir(tmp_path, worktree, head="ref: refs/heads/awf/ws\n")
    _age_tree(worktree)
    _age_tree(tmp_path / "mirror.git")

    denied = git_dir / denied_name
    if denied_name == "HEAD":
        real_read_head_at = worktree_activity._read_head_at

        def _deny_head(root: worktree_activity._PinnedDirectory) -> str | None:
            if root.path / "HEAD" == denied:
                raise PermissionError(13, "read denied", str(denied))
            return real_read_head_at(root)

        monkeypatch.setattr(worktree_activity, "_read_head_at", _deny_head)
    else:
        real_read_text = Path.read_text

        def _deny_commondir(self: Path, *args: object, **kwargs: object) -> str:
            if self == denied:
                raise PermissionError(13, "read denied", str(denied))
            return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "read_text", _deny_commondir)

    probe = WorktreeActivityProbe(worktree)
    with structlog.testing.capture_logs() as captured:
        assert await probe() is None

    unreadable = [
        entry
        for entry in captured
        if entry.get("event") == "agent.worktree_activity.git_pointer_unreadable"
    ]
    assert len(unreadable) == 1
    assert unreadable[0]["path"] == str(denied)


@pytest.mark.unit
async def test_relative_gitdir_pointer_resolves_against_the_worktree(
    tmp_path: Path,
    worktree: Path,
) -> None:
    git_dir = tmp_path / "relative.git"
    git_dir.mkdir()
    (git_dir / "index").write_bytes(b"DIRC")
    relative = os.path.relpath(git_dir, worktree)
    (worktree / ".git").write_text(f"gitdir: {relative}\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(git_dir)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    (git_dir / "index").write_bytes(b"DIRC-updated")
    assert await probe() is True


@pytest.mark.unit
async def test_unreadable_git_dir_metadata_reports_unknown(
    tmp_path: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A metadata path that cannot be stat-ed leaves the scan incomplete.

    HEAD / index / logs/HEAD live outside a linked worktree, so they are the
    only place Git-only activity shows up. Folding an unreadable one in as a
    stable ``(path, None)`` term claimed a complete scan while blind to every
    later commit or index write — consecutive fingerprints matched and the
    watchdog would idle-kill a run that was still working.
    """
    git_dir = tmp_path / "mirror.git" / "worktrees" / "ws_probe"
    git_dir.mkdir(parents=True)
    (git_dir / "index").write_bytes(b"DIRC")
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(git_dir)

    real_metadata_stat_at = worktree_activity._metadata_stat_at
    denied = git_dir / "index"

    def _deny_one(watched: worktree_activity._WatchedPath) -> os.stat_result | None:
        if watched.path == denied:
            raise PermissionError("lstat denied")
        return real_metadata_stat_at(watched)

    monkeypatch.setattr(worktree_activity, "_metadata_stat_at", _deny_one)

    probe = WorktreeActivityProbe(worktree)
    with structlog.testing.capture_logs() as captured:
        assert await probe() is None
        assert await probe() is None

    unreadable = [
        entry
        for entry in captured
        if entry.get("event") == "agent.worktree_activity.metadata_unreadable"
    ]
    assert len(unreadable) == 2
    assert unreadable[0]["path"] == str(denied)


@pytest.mark.unit
async def test_absent_git_dir_metadata_still_allows_an_idle_answer(
    tmp_path: Path,
    worktree: Path,
) -> None:
    """A metadata path that is simply not there is a complete observation.

    ``logs/HEAD`` only exists once a reflog does, so treating its absence as
    "could not tell" would wedge the probe into answering ``None`` forever and
    the idle watchdog would never fire at all. Its later appearance is activity.
    """
    git_dir = tmp_path / "mirror.git" / "worktrees" / "ws_probe"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/awf/ws\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(git_dir)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    (git_dir / "logs").mkdir()
    (git_dir / "logs" / "HEAD").write_text("reflog\n", encoding="utf-8")
    assert await probe() is True


@pytest.mark.unit
async def test_unreadable_gitfile_pointer_reports_unknown(
    tmp_path: Path,
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``.git`` pointer this process cannot read leaves the scan incomplete.

    The control plane and the agent run as different users, so the pointer may
    be unreadable here while the agent keeps committing through it. Treating
    that like a plain (non-linked) checkout drops HEAD / index / logs/HEAD from
    the scan and still claims a complete fingerprint — Git-only activity would
    then leave consecutive fingerprints equal and authorise an idle kill.
    """
    git_dir = tmp_path / "mirror.git" / "worktrees" / "ws_probe"
    git_dir.mkdir(parents=True)
    (git_dir / "index").write_bytes(b"DIRC")
    gitfile = worktree / ".git"
    gitfile.write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    _age_tree(worktree)
    _age_tree(git_dir)

    real_read_text = Path.read_text

    def _deny_gitfile(self: Path, *args: object, **kwargs: object) -> str:
        if self == gitfile:
            raise PermissionError("read denied")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _deny_gitfile)

    probe = WorktreeActivityProbe(worktree)
    with structlog.testing.capture_logs() as captured:
        assert await probe() is None
        assert await probe() is None

    unreadable = [
        entry
        for entry in captured
        if entry.get("event") == "agent.worktree_activity.git_pointer_unreadable"
    ]
    assert len(unreadable) == 2
    assert unreadable[0]["path"] == str(gitfile)


@pytest.mark.unit
@pytest.mark.parametrize("gitfile_body", ["", "gitdir:\n", "not a gitfile\n"])
async def test_unusable_gitfile_falls_back_to_the_worktree_walk(
    worktree: Path,
    gitfile_body: str,
) -> None:
    (worktree / ".git").write_text(gitfile_body, encoding="utf-8")
    _age_tree(worktree)

    probe = WorktreeActivityProbe(worktree)
    await probe.prime()
    assert await probe() is False

    (worktree / "README.md").write_text("changed\n", encoding="utf-8")
    assert await probe() is True
