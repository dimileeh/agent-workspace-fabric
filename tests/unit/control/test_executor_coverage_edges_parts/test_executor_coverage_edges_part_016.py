"""Plan artifact digest and near-miss recovery edge tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from awf.control.executor import planning_ops as executor_planning_ops


@pytest.mark.unit
def test_plan_artifact_candidate_digests_skips_non_internal_plan_dir(
    tmp_path: Path,
) -> None:
    """A plan path outside the internal plan dir digests no candidates.

    Only ``docs/awf-plans`` artifacts participate in near-miss recovery; a plan
    path rooted elsewhere must short-circuit before any filesystem scan so an
    unrelated sibling tree is never treated as a recovery source.
    """
    candidates = executor_planning_ops._plan_artifact_candidate_digests(  # noqa: SLF001
        tmp_path,
        Path("some/other/place/ws_main.md"),
    )

    assert candidates == {}


@pytest.mark.unit
def test_plan_artifact_candidate_digests_skips_symlink_and_directory_entries(
    tmp_path: Path,
) -> None:
    """Only real, non-symlink files are digested as recovery candidates.

    A ``ws_*.md`` glob match that is a symlink (``is_symlink``) or a directory
    (``not is_file``) must be skipped: following such an entry during the later
    ``source.replace(target)`` move would mutate storage outside the plain plan
    artifact the scope checks observe. The lone real file is the only candidate.
    """
    worktree = tmp_path / "worktree"
    plan_dir = worktree / "docs" / "awf-plans"
    plan_dir.mkdir(parents=True)
    real = plan_dir / "ws_real.md"
    real.write_text("# real\n", encoding="utf-8")
    (plan_dir / "ws_dir.md").mkdir()  # matches the glob but is not a file
    (plan_dir / "ws_link.md").symlink_to(real)  # matches the glob but is a symlink

    candidates = executor_planning_ops._plan_artifact_candidate_digests(  # noqa: SLF001
        worktree,
        Path("docs/awf-plans/ws_main.md"),
    )

    expected_digest = hashlib.sha256(b"# real\n").hexdigest()
    assert candidates == {Path("docs/awf-plans/ws_real.md"): expected_digest}


@pytest.mark.unit
def test_plan_artifact_candidate_digests_skips_undigestable_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A candidate that cannot be digested (vanished/unreadable) is excluded.

    ``_digest_file_if_present`` returns ``None`` when a file disappears or fails
    to read between the glob and the digest. Such a candidate must be dropped
    rather than recorded with a missing digest, so the near-miss snapshot stays
    faithful to what is actually on disk.
    """
    worktree = tmp_path / "worktree"
    plan_dir = worktree / "docs" / "awf-plans"
    plan_dir.mkdir(parents=True)
    (plan_dir / "ws_real.md").write_text("# real\n", encoding="utf-8")
    monkeypatch.setattr(
        executor_planning_ops,
        "_digest_file_if_present",
        lambda _path: None,
    )

    candidates = executor_planning_ops._plan_artifact_candidate_digests(  # noqa: SLF001
        worktree,
        Path("docs/awf-plans/ws_main.md"),
    )

    assert candidates == {}


@pytest.mark.unit
def test_recover_plan_artifact_near_miss_ignores_non_default_plan_path() -> None:
    """Recovery only applies to the default ``ws_<id>.md`` plan path.

    A profile that overrides ``plan_path`` away from the default takes ownership
    of its own artifact naming, so the typo-recovery heuristic must not fire and
    must yield no evidence.
    """
    recovered, evidence = executor_planning_ops._recover_plan_artifact_near_miss(  # noqa: SLF001
        worktree_path=Path("/nonexistent/worktree"),
        workspace_id="ws_custom",
        required_plan_path=Path("docs/awf-plans/custom-name.md"),
        required_plan_digest_after=None,
        dirty_paths_before_planning=[],
        changed_paths_during_planning=[],
        candidates_before={},
        candidates_after={Path("docs/awf-plans/ws_typoed.md"): "digest"},
        conformance_report_present=False,
    )

    assert recovered is False
    assert evidence == []


@pytest.mark.unit
def test_recover_plan_artifact_near_miss_refuses_when_target_present_on_disk(
    tmp_path: Path,
) -> None:
    """A required plan that already exists on disk blocks the elevated move.

    Even after every other guard passes, a final ``target.exists()`` check
    refuses to clobber a plan file that materialized at the required path,
    surfacing ``required_plan_path_exists`` instead.
    """
    required_plan_path = Path("docs/awf-plans/ws_t.md")
    candidate_path = Path("docs/awf-plans/ws_u.md")  # Hamming distance 1
    target = tmp_path / required_plan_path
    target.parent.mkdir(parents=True)
    target.write_text("# already here\n", encoding="utf-8")

    recovered, evidence = executor_planning_ops._recover_plan_artifact_near_miss(  # noqa: SLF001
        worktree_path=tmp_path,
        workspace_id="ws_t",
        required_plan_path=required_plan_path,
        required_plan_digest_after=None,
        dirty_paths_before_planning=[],
        changed_paths_during_planning=[],
        candidates_before={},
        candidates_after={candidate_path: "digest"},
        conformance_report_present=False,
    )

    assert recovered is False
    assert evidence == [
        {
            "path": "docs/awf-plans/ws_u.md",
            "required_path": "docs/awf-plans/ws_t.md",
            "reason": "required_plan_path_exists",
            "filename_hamming_distance": 1,
        }
    ]
    # The pre-existing plan must be left untouched.
    assert target.read_text(encoding="utf-8") == "# already here\n"


@pytest.mark.unit
def test_recover_plan_artifact_near_miss_reports_failed_move(
    tmp_path: Path,
) -> None:
    """A move that fails (candidate vanished) yields ``recovery_move_failed``.

    When the typo candidate disappears between the snapshot and the rename, the
    ``source.replace(target)`` raises ``OSError``; recovery must report the
    failure with the error string rather than crashing or claiming success.
    """
    required_plan_path = Path("docs/awf-plans/ws_t.md")
    candidate_path = Path("docs/awf-plans/ws_u.md")  # Hamming distance 1
    # The plan dir exists but the candidate source file does not, so the rename
    # raises ``FileNotFoundError`` (a subclass of ``OSError``).
    (tmp_path / required_plan_path.parent).mkdir(parents=True)

    recovered, evidence = executor_planning_ops._recover_plan_artifact_near_miss(  # noqa: SLF001
        worktree_path=tmp_path,
        workspace_id="ws_t",
        required_plan_path=required_plan_path,
        required_plan_digest_after=None,
        dirty_paths_before_planning=[],
        changed_paths_during_planning=[],
        candidates_before={},
        candidates_after={candidate_path: "digest"},
        conformance_report_present=False,
    )

    assert recovered is False
    assert len(evidence) == 1
    item = evidence[0]
    assert item["path"] == "docs/awf-plans/ws_u.md"
    assert item["required_path"] == "docs/awf-plans/ws_t.md"
    assert item["reason"] == "recovery_move_failed"
    assert item["filename_hamming_distance"] == 1
    assert isinstance(item["error"], str) and item["error"]
    # No partial artifact must be left at the required path.
    assert not (tmp_path / required_plan_path).exists()
