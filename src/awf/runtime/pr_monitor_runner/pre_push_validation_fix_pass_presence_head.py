"""Commit-change presence check against HEAD for pre-push salvage reuse."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.path_helpers import _changed_paths_from_name_only_z
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
    _git_env_for_merge_safety_object_lookup,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence import (
    _added_salvage_blob_retained as _added_salvage_blob_retained,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence import (
    _tip_extra_can_supersede_modified_salvage as _tip_extra_can_supersede_modified_salvage,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_context import (
    _bytes_unsafe_for_text_merge as _bytes_unsafe_for_text_merge,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_context import (
    _git_mode_file_kind as _git_mode_file_kind,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_context import (
    _merge_file_result_matches_head as _merge_file_result_matches_head,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_context import (
    _parse_ls_tree_meta as _parse_ls_tree_meta,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_context import (
    _raw_blob_from_cat_file_result as _raw_blob_from_cat_file_result,
)
from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError


async def _commit_changes_present_in_head(
    self: Any,
    *,
    worktree_path: Path,
    commit: str,
    head: str,
    baseline: str | None = None,
) -> bool:
    """Return True when ``commit``'s changes vs ``baseline`` still appear in ``head``.

    Ancestry alone accepts a descendant that reverts ``commit``'s content. Salvage
    reuse therefore requires either an identical tree at ``head``, or that
    **every** path changed by ``commit`` vs ``baseline`` still retains the
    salvaged patch at ``head`` — not necessarily a byte-identical tree entry
    (mode+type+OID). A later tip may edit a different hunk of the same file
    (OID differs) while the salvage hunk remains applied; that must still count
    as present (PRRT_kwDOSJAM6s6ZmWRh). Retention for blobs with a baseline is
    checked via a clean 3-way ``git merge-file`` of parent/head/commit whose
    result equals head, then rejecting tip-only lines that rebind a name the
    salvage changed vs parent (appended ``FEATURE_ENABLED = False`` after a
    False→True salvage; PRRT_kwDOSJAM6s6Zp_3j). A no-baseline addition (new path)
    cannot use that
    3-way model; retain when the salvage blob remains a line-boundary-aligned
    prefix or suffix of the tip blob so append/prepend keep evidence while
    mid-line modifications, mid-file disabling wrappers (``#if 0`` / comments /
    strings), prepended unterminated wrappers that leave salvage as a suffix
    (PRRT_kwDOSJAM6s6ZpaIn), appended rebinding of salvage names
    (PRRT_kwDOSJAM6s6Zp8jM), and overwrites fail closed (PRRT_kwDOSJAM6s6Zm0PC,
    PRRT_kwDOSJAM6s6Zm6F1, PRRT_kwDOSJAM6s6ZpQKt). ``baseline``
    defaults to the tip's
    first parent; callers that retain a failed-run tip must pass the invocation
    start SHA so a multi-commit salvage (H1 fix + H2 unrelated) is checked as
    the full ``start..tip`` delta — otherwise a later tip that reverts H1 while
    preserving H2 falsely retains evidence (PRRT_kwDOSJAM6s6ZmG-B). A deleted
    path is an empty entry: it counts as present only when the baseline still
    had the path and ``head`` remains absent (both-missing bogus lookups fail
    closed). A third-content overwrite (A→B salvage, later tip to C) must fail
    closed even though C≠A — otherwise a no-change FIXED retry can reuse stale
    salvage after B is gone. Mode-only salvage (e.g. chmod +x) that a later tip
    reverts must likewise fail closed, because Git stores mode separately from
    the object id. Partial or full reverts and revert-then-unrelated tips fail
    closed. Root commits and unresolved objects also fail closed.
    """
    git_env = _git_env_for_merge_safety_object_lookup()

    async def _rev_parse(ref: str) -> str:
        result = await self._deps.runner.run(
            git_worktree_command(worktree_path, "rev-parse", ref),
            env=git_env,
        )
        return result.stdout.strip() if result.ok else ""

    async def _tree_entry_at(ref: str, path: str) -> str | None:
        # Compare mode + type + object id. Missing path → empty token so absence
        # compares equal across refs. Lookup failure → ``None`` so callers fail
        # closed instead of treating errors as absence (PRRT_kwDOSJAM6s6ZoduB).
        # ``ls-tree`` lines are ``<mode> SP <type> SP <object> TAB <file>``; keep
        # metadata only. Diff-derived paths are literal filenames; without
        # ``--literal-pathspecs`` a name like ``:(literal)foo`` is pathspec magic
        # and resolves to ``foo``, so a tip that reverts the magic path while
        # leaving ``foo`` unchanged falsely retains salvage (PRRT_kwDOSJAM6s6ZmirW).
        result = await self._deps.runner.run(
            git_worktree_command(
                worktree_path,
                "--literal-pathspecs",
                "ls-tree",
                "-z",
                ref,
                "--",
                path,
            ),
            env=git_env,
        )
        if not result.ok:
            return None
        entry = result.stdout.strip()
        if not entry:
            return ""
        raw = entry.split("\0", 1)[0].strip()
        if not raw:
            return ""
        meta_token: str = raw.partition("\t")[0]
        return meta_token

    async def _blob_raw(oid: str) -> bytes | None:
        result = await self._deps.runner.run(
            git_worktree_command(worktree_path, "cat-file", "blob", oid),
            env=git_env,
        )
        return _raw_blob_from_cat_file_result(
            ok=result.ok,
            stdout=result.stdout,
            stdout_bytes=result.stdout_bytes,
        )

    async def _salvage_entry_retained(
        *,
        parent_entry: str,
        commit_entry: str,
        head_entry: str,
    ) -> bool:
        # Fast path: identical tree entry (mode+type+OID) is definitely retained.
        if head_entry == commit_entry:
            return True
        if not head_entry:
            return False
        commit_meta = _parse_ls_tree_meta(commit_entry)
        head_meta = _parse_ls_tree_meta(head_entry)
        if commit_meta is None or head_meta is None:
            return False
        commit_mode, commit_type, commit_oid = commit_meta
        head_mode, head_type, head_oid = head_meta
        if commit_type != head_type:
            return False
        # Non-blob types have no merge-file patch model; require exact equality.
        if commit_type != "blob":
            return False

        parent_meta = _parse_ls_tree_meta(parent_entry) if parent_entry else None
        # Mode retention: when salvage changed mode (or added the path), HEAD must
        # still carry the salvage mode. Content-only salvage may tolerate later
        # same-kind mode bits (e.g. chmod ±x) without forcing full mode equality.
        if (parent_meta is None or parent_meta[0] != commit_mode) and head_mode != commit_mode:
            return False
        # File kind must still match even for content-only salvage. Git stores
        # symlink targets as blobs, so a tip can replace a regular file whose
        # content is a pathname with a same-OID symlink and falsely pass the
        # OID fast path below (PRRT_kwDOSJAM6s6Znm-O).
        if _git_mode_file_kind(head_mode) != _git_mode_file_kind(commit_mode):
            return False

        if commit_oid == head_oid:
            return True

        if parent_meta is None:
            # Addition without a baseline blob: later tips may append/prepend
            # while leaving the added bytes intact (OID changes). Exact OID is
            # sufficient but not required — require line-boundary-aligned
            # prefix or suffix retention of the salvage blob; suffix also
            # rejects open disabling wrappers, and prefix+append rejects
            # rebinding of salvage names
            # (PRRT_kwDOSJAM6s6Zm0PC, PRRT_kwDOSJAM6s6Zm6F1,
            # PRRT_kwDOSJAM6s6ZpQKt, PRRT_kwDOSJAM6s6ZpaIn,
            # PRRT_kwDOSJAM6s6Zp8jM).
            head_raw = await _blob_raw(head_oid)
            commit_raw = await _blob_raw(commit_oid)
            if head_raw is None or commit_raw is None:
                return False
            # Same unsafe-text gate as the baseline path: containment is not
            # trustworthy once decode(replace) may have collapsed distinct
            # invalid bytes (exact OID already failed above).
            if _bytes_unsafe_for_text_merge(head_raw) or _bytes_unsafe_for_text_merge(commit_raw):
                return False
            return _added_salvage_blob_retained(
                commit_blob=commit_raw.decode("utf-8"),
                head_blob=head_raw.decode("utf-8"),
            )

        _, parent_type, parent_oid = parent_meta
        # Mode-only salvage (same blob as baseline): content is retained once mode
        # checks passed.
        if commit_oid == parent_oid:
            return True
        if parent_type != "blob":
            return False

        parent_raw = await _blob_raw(parent_oid)
        head_raw = await _blob_raw(head_oid)
        commit_raw = await _blob_raw(commit_oid)
        if parent_raw is None or head_raw is None or commit_raw is None:
            return False
        # CommandResult decodes as UTF-8 with replace. NUL and *invalid* UTF-8
        # cannot be round-tripped safely through merge-file — require exact OID
        # equality. Distinct invalid-byte blobs all collapse to the same U+FFFD
        # text, so merge-file would falsely prove retention. Intentional U+FFFD
        # in valid UTF-8 is retained; detect lossy decode via strict UTF-8 on
        # raw bytes (PRRT_kwDOSJAM6s6ZnK_D).
        if any(_bytes_unsafe_for_text_merge(raw) for raw in (parent_raw, head_raw, commit_raw)):
            return False

        # Honor TMPDIR (do not hardcode /tmp). Creation/write I/O must fail
        # closed as False so FIXED evidence checking cannot crash the fix cycle
        # (PRRT_kwDOSJAM6s6ZoX2i). ignore_cleanup_errors keeps a successful
        # retention result from being rewritten by cleanup-only OSError.
        try:
            with tempfile.TemporaryDirectory(
                prefix="awf-salvage-merge-",
                ignore_cleanup_errors=True,
            ) as tmp:
                tmp_dir = Path(tmp)
                base_path = tmp_dir / "base"
                ours_path = tmp_dir / "ours"
                theirs_path = tmp_dir / "theirs"
                base_path.write_bytes(parent_raw)
                ours_path.write_bytes(head_raw)
                theirs_path.write_bytes(commit_raw)
                merge_result = await self._deps.runner.run(
                    git_worktree_command(
                        worktree_path,
                        "merge-file",
                        "-p",
                        str(ours_path),
                        str(base_path),
                        str(theirs_path),
                    ),
                    env=git_env,
                )
                # Exit 0 ⇒ clean merge; result must equal HEAD (salvage ⊆ head).
                if not merge_result.ok:
                    return False
                if not _merge_file_result_matches_head(
                    head_raw=head_raw,
                    stdout=merge_result.stdout,
                    stdout_bytes=merge_result.stdout_bytes,
                ):
                    return False
                # Clean merge can still keep the salvage hunk while a later tip
                # appends a rebinding of a salvage-changed name; reject that
                # supersession (added-file path already does via
                # ``_suffix_can_supersede_added_salvage``; PRRT_kwDOSJAM6s6Zp_3j).
                return not _tip_extra_can_supersede_modified_salvage(
                    parent_blob=parent_raw.decode("utf-8"),
                    commit_blob=commit_raw.decode("utf-8"),
                    head_blob=head_raw.decode("utf-8"),
                )
        except OSError:
            return False

    commit_sha = commit.strip()
    head_sha = head.strip()
    if not commit_sha or not head_sha:
        return False
    if commit_sha.lower() == head_sha.lower():
        return True

    commit_tree = await _rev_parse(f"{commit_sha}^{{tree}}")
    head_tree = await _rev_parse(f"{head_sha}^{{tree}}")
    if not commit_tree or not head_tree:
        return False
    if commit_tree.lower() == head_tree.lower():
        return True

    baseline_sha = (baseline or "").strip()
    if baseline_sha:
        # Resolve through rev-parse so abbreviated / symbolic baselines compare
        # as full object ids against commit/head trees.
        parent = await _rev_parse(baseline_sha)
    else:
        parent = await _rev_parse(f"{commit_sha}^")
    if not parent:
        return False
    if parent.lower() == commit_sha.lower():
        return False
    parent_tree = await _rev_parse(f"{parent}^{{tree}}")
    if not parent_tree:
        return False
    if parent_tree.lower() == head_tree.lower():
        return False

    paths_result = await self._deps.runner.run(
        git_worktree_command(
            worktree_path,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-z",
            "-r",
            parent,
            commit_sha,
        ),
        env=git_env,
    )
    if not paths_result.ok:
        return False
    # ``-z`` preserves pathname bytes (including newlines). Without it, Git
    # C-quotes such names; ``splitlines()`` then feeds the quoted spelling to
    # ``ls-tree``, both lookups miss, and empty==empty falsely retains salvage
    # (PRRT_kwDOSJAM6s6ZmCZz). Prefer raw ``stdout_bytes``: the runner's
    # ``stdout`` string is UTF-8-decoded with ``errors="replace"``, which
    # rewrites invalid-UTF-8 pathnames to U+FFFD and makes ``ls-tree`` miss
    # (PRRT_kwDOSJAM6s6ZmviP).
    try:
        if paths_result.stdout_bytes is not None:
            paths = _changed_paths_from_name_only_z(paths_result.stdout_bytes)
        else:
            paths = _changed_paths_from_name_only_z(paths_result.stdout or "")
    except ProtectedScopeDiffError:
        return False
    if not paths:
        return False

    for path in paths:
        # Distinguish deletions with full baseline/commit/head entries. Empty
        # commit+head is retained salvage only when the baseline still had a
        # concrete entry; bogus/C-quoted paths miss baseline and commit alike;
        # any re-add at head fails closed (PRRT_kwDOSJAM6s6ZmEAd / ZmEG6).
        # Lookup errors (``None``) also fail closed — never treat a failed
        # ``ls-tree`` as genuine absence (PRRT_kwDOSJAM6s6ZoduB).
        parent_entry = await _tree_entry_at(parent, path)
        commit_entry = await _tree_entry_at(commit_sha, path)
        head_entry = await _tree_entry_at(head_sha, path)
        if parent_entry is None or commit_entry is None or head_entry is None:
            return False
        if not commit_entry:
            if not parent_entry or head_entry:
                return False
            continue
        if not await _salvage_entry_retained(
            parent_entry=parent_entry,
            commit_entry=commit_entry,
            head_entry=head_entry,
        ):
            return False
    return True
