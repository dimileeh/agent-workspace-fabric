"""Planning artifact discovery and safe near-miss recovery helpers."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from awf.common.owned_paths import INTERNAL_PLAN_ARTIFACT_DIR
from awf.control.executor.helpers import _digest_file_if_present
from awf.control.executor.quality_gates import _log

_PLAN_ARTIFACT_NEAR_MISS_GLOB = "ws_*.md"
_PLAN_ARTIFACT_NEAR_MISS_MAX_DISTANCE = 2


def _plan_artifact_candidate_digests(
    worktree_path: Path,
    plan_path: Path,
    *,
    digest_file: Callable[[Path], str | None] = _digest_file_if_present,
) -> dict[Path, str]:
    """Digest direct ignored-plan candidates without changing git dirty semantics."""
    if plan_path.parent.as_posix() != INTERNAL_PLAN_ARTIFACT_DIR:
        return {}

    plan_dir = worktree_path / plan_path.parent
    if not plan_dir.is_dir():
        return {}

    # Refuse to follow a plan directory reached through a symlink anywhere in its
    # path. ``is_dir()`` and ``glob`` both follow symlinks, so a repo that tracks
    # ``docs/awf-plans`` as a link would yield candidates whose lexical paths look
    # like normal in-worktree artifacts while physically living elsewhere. Plain
    # outside-the-worktree containment is not enough: a link to an in-worktree but
    # git-hidden directory (``.git`` or another ignored dir) still resolves under
    # the worktree, yet ``glob`` and the later ``source.replace(target)`` would
    # follow the link and mutate storage the porcelain dirty/changed scope checks
    # never observe -- letting near-miss recovery mark the logical plan path
    # recovered after writing non-artifact storage with no scope evidence. Require
    # the plan dir to be the real directory at its lexical location, i.e. that no
    # symlink was followed when resolving it under the worktree.
    try:
        resolved_worktree = worktree_path.resolve(strict=True)
        resolved_plan_dir = plan_dir.resolve(strict=True)
    except OSError:  # pragma: no cover - plan dir removed between is_dir() and resolve()
        return {}
    if resolved_plan_dir != resolved_worktree / plan_path.parent:
        return {}

    candidates: dict[Path, str] = {}
    for candidate in sorted(plan_dir.glob(_PLAN_ARTIFACT_NEAR_MISS_GLOB)):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        try:
            relative_candidate = candidate.relative_to(worktree_path)
        except ValueError:  # pragma: no cover - glob children always sit under the worktree
            continue
        if relative_candidate.parent != plan_path.parent:
            continue  # pragma: no cover - non-recursive glob yields only direct children
        digest = digest_file(candidate)
        if digest is not None:
            candidates[relative_candidate] = digest
    return candidates


def _changed_plan_artifact_candidates(
    before: Mapping[Path, str],
    after: Mapping[Path, str],
    *,
    required_plan_path: Path,
) -> tuple[Path, ...]:
    changed = [
        path
        for path, digest in after.items()
        if path != required_plan_path and before.get(path) != digest
    ]
    return tuple(sorted(changed))


def _filename_hamming_distance(left: str, right: str) -> int | None:
    if len(left) != len(right):
        return None
    return sum(
        1 for left_char, right_char in zip(left, right, strict=True) if left_char != right_char
    )


class _UnsetFilenameDistance:
    """Sentinel marking that the Hamming distance was not pre-computed."""


_UNSET_FILENAME_DISTANCE = _UnsetFilenameDistance()


def _near_miss_plan_artifact_evidence(
    *,
    candidate: Path,
    required_plan_path: Path,
    reason: str,
    filename_distance: int | None | _UnsetFilenameDistance = _UNSET_FILENAME_DISTANCE,
) -> dict[str, object]:
    distance = (
        _filename_hamming_distance(candidate.name, required_plan_path.name)
        if isinstance(filename_distance, _UnsetFilenameDistance)
        else filename_distance
    )
    evidence: dict[str, object] = {
        "path": candidate.as_posix(),
        "required_path": required_plan_path.as_posix(),
        "reason": reason,
    }
    if distance is not None:
        evidence["filename_hamming_distance"] = distance
    return evidence


def _classify_plan_artifact_near_miss(
    candidate: Path, required_plan_path: Path
) -> tuple[bool, int | None]:
    """Return ``(is_safe, distance)`` so callers can forward the pre-computed distance."""
    distance = _filename_hamming_distance(candidate.name, required_plan_path.name)
    is_safe = distance is not None and 0 < distance <= _PLAN_ARTIFACT_NEAR_MISS_MAX_DISTANCE
    return is_safe, distance


def _recover_plan_artifact_near_miss(
    *,
    worktree_path: Path,
    workspace_id: str,
    required_plan_path: Path,
    required_plan_digest_after: str | None,
    dirty_paths_before_planning: Sequence[Path],
    changed_paths_during_planning: Sequence[Path],
    candidates_before: Mapping[Path, str],
    candidates_after: Mapping[Path, str],
    conformance_report_present: bool,
) -> tuple[bool, list[dict[str, object]]]:
    """Recover a single typo-like ignored plan artifact when the rest is clean."""
    required_default_path = Path(INTERNAL_PLAN_ARTIFACT_DIR) / f"{workspace_id}.md"
    if required_plan_path != required_default_path:
        return False, []

    changed_candidates = _changed_plan_artifact_candidates(
        candidates_before,
        candidates_after,
        required_plan_path=required_plan_path,
    )
    if not changed_candidates:
        return False, []

    # A near-miss recovery presumes the worktree is clean apart from one typoed
    # plan file. If the planning phase also left a conformance report on disk
    # (e.g. a prewritten satisfied JSON), the later success path consumes it via
    # ``_read_text_if_present(report_path) or stdout`` and can short-circuit the
    # conformance loop on a stale report before the compare call produces fresh
    # output. The report lives in the same ignored plan dir, so neither the
    # porcelain dirty diff nor the ``ws_*.md`` candidate snapshot sees it. Refuse
    # the elevated-trust move while a report is present rather than proceed atop
    # an ignored side file the recovery never accounted for.
    if conformance_report_present:
        return (
            False,
            [
                _near_miss_plan_artifact_evidence(
                    candidate=candidate,
                    required_plan_path=required_plan_path,
                    reason="conformance_report_present",
                )
                for candidate in changed_candidates
            ],
        )

    # The caller's clean check is ``after_plan - before_plan``, so any path that
    # was already dirty before planning is subtracted out and treated as clean.
    # In a preserved/resumed workspace that lets the planning agent edit a
    # pre-dirty source file while only writing an ignored near-miss plan: the
    # diff stays empty and the plan-only scope guard would be bypassed by the
    # elevated-trust move. Refuse recovery unless the worktree started clean.
    dirty_baseline_strings = [path.as_posix() for path in dirty_paths_before_planning]
    if dirty_baseline_strings:
        evidence = [
            _near_miss_plan_artifact_evidence(
                candidate=candidate,
                required_plan_path=required_plan_path,
                reason="dirty_baseline_before_planning",
            )
            for candidate in changed_candidates
        ]
        for item in evidence:
            item["dirty_baseline_paths"] = dirty_baseline_strings[:20]
        return False, evidence

    changed_path_strings = [path.as_posix() for path in changed_paths_during_planning]
    if changed_path_strings:
        evidence = [
            _near_miss_plan_artifact_evidence(
                candidate=candidate,
                required_plan_path=required_plan_path,
                reason="planning_changed_other_paths",
            )
            for candidate in changed_candidates
        ]
        for item in evidence:
            item["offending_paths"] = changed_path_strings[:20]
        return False, evidence

    # Key this guard on the required path's *current* presence, not on a stale
    # pre-planning snapshot. A preserved/resumed workspace can carry a plan
    # digest from a prior run; if the planning agent deletes that plan and only
    # a typo sibling remains, the required path is genuinely gone and recovery
    # must proceed. Refuse only when the required plan still exists after
    # planning (``digest_after is not None``) so we never clobber a live plan.
    if required_plan_digest_after is not None:
        return (
            False,
            [
                _near_miss_plan_artifact_evidence(
                    candidate=candidate,
                    required_plan_path=required_plan_path,
                    reason="required_plan_already_existed",
                )
                for candidate in changed_candidates
            ],
        )

    if len(changed_candidates) != 1:
        return (
            False,
            [
                _near_miss_plan_artifact_evidence(
                    candidate=candidate,
                    required_plan_path=required_plan_path,
                    reason="ambiguous_near_miss_candidates",
                )
                for candidate in changed_candidates
            ],
        )

    candidate = changed_candidates[0]
    is_safe, filename_distance = _classify_plan_artifact_near_miss(candidate, required_plan_path)
    if not is_safe:
        return (
            False,
            [
                _near_miss_plan_artifact_evidence(
                    candidate=candidate,
                    required_plan_path=required_plan_path,
                    reason="filename_not_close_enough",
                    filename_distance=filename_distance,
                )
            ],
        )

    source = worktree_path / candidate
    target = worktree_path / required_plan_path
    if target.exists():
        return (
            False,
            [
                _near_miss_plan_artifact_evidence(
                    candidate=candidate,
                    required_plan_path=required_plan_path,
                    reason="required_plan_path_exists",
                )
            ],
        )

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
    except OSError as exc:
        move_evidence = _near_miss_plan_artifact_evidence(
            candidate=candidate,
            required_plan_path=required_plan_path,
            reason="recovery_move_failed",
        )
        move_evidence["error"] = str(exc)
        return False, [move_evidence]

    _log.info(
        "executor.planning_near_miss_plan_artifact_recovered",
        workspace_id=workspace_id,
        required_path=required_plan_path.as_posix(),
        recovered_from=candidate.as_posix(),
    )
    return True, []
