"""Salvage presence / tree-entry helpers for pre-push validation fix passes."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.path_helpers import _changed_paths_from_name_only_z
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
    _git_env_for_merge_safety_object_lookup,
)
from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError

# Binding targets that an appended tip can rebind to supersede added salvage.
_ASSIGN_BINDING_RE = re.compile(
    r"(?m)^[ \t]*([A-Za-z_][A-Za-z0-9_]*)"
    r"(?:"
    r"(?:[ \t]*:[ \t]*[^=\n]+)?[ \t]*=(?!=)"  # `name =` / `name: T =`
    r"|"
    r"[ \t]*:="  # `name :=`
    r")"
)
_DEF_BINDING_RE = re.compile(r"(?m)^[ \t]*(?:async[ \t]+)?def[ \t]+([A-Za-z_][A-Za-z0-9_]*)")
_CLASS_BINDING_RE = re.compile(
    r"(?m)^[ \t]*(?:export[ \t]+(?:default[ \t]+)?)?class[ \t]+([A-Za-z_][A-Za-z0-9_]*)"
)
_FUNCTION_BINDING_RE = re.compile(
    r"(?m)^[ \t]*(?:export[ \t]+(?:default[ \t]+)?)?(?:async[ \t]+)?function[ \t]+([A-Za-z_][A-Za-z0-9_]*)"
)
_LET_CONST_BINDING_RE = re.compile(
    r"(?m)^[ \t]*(?:export[ \t]+)?(?:const|let|var)[ \t]+([A-Za-z_][A-Za-z0-9_]*)"
)
_DEFINE_BINDING_RE = re.compile(r"(?m)^[ \t]*#[ \t]*define[ \t]+([A-Za-z_][A-Za-z0-9_]*)")
# ``#`` lines that are not ``#define`` / ``# define`` are comments / other
# directives; spaced form must match the same whitespace rule as open-``#if``
# scanning (PRRT_kwDOSJAM6s6Zp_sv).
_DEFINE_DIRECTIVE_LINE_RE = re.compile(r"#[ \t]*define\b")


def _parse_ls_tree_meta(entry: str) -> tuple[str, str, str] | None:
    """Parse ``mode type oid`` from an ``ls-tree`` metadata token."""
    mode, sep, rest = entry.partition(" ")
    if not sep:
        return None
    obj_type, sep, oid = rest.partition(" ")
    if not sep or not mode or not obj_type or not oid or " " in oid:
        return None
    return mode, obj_type, oid


def _git_mode_file_kind(mode: str) -> str:
    """Return the Git tree-entry kind encoded in ``mode``.

    Regular files share kind ``file`` whether or not the executable bit is set
    (``100644`` / ``100755``). Symlinks (``120000``) and gitlinks (``160000``)
    are distinct kinds. Unknown modes compare as themselves so mismatched
    unknowns fail closed.
    """
    if mode.startswith("100"):
        return "file"
    if mode == "120000":
        return "symlink"
    if mode == "160000":
        return "gitlink"
    return mode


def _bytes_unsafe_for_text_merge(raw: bytes) -> bool:
    """Return True when merge-file / string containment cannot be trusted.

    NUL breaks merge-file. Invalid UTF-8 collapses under ``decode(replace)`` to
    U+FFFD, so distinct invalid blobs can falsely look retained. Intentional
    U+FFFD in valid UTF-8 is fine — detect lossy decode via strict UTF-8 on
    raw bytes, not via the decoded character (PRRT_kwDOSJAM6s6ZnK_D).
    """
    if b"\0" in raw:
        return True
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _raw_blob_from_cat_file_result(
    *, ok: bool, stdout: str, stdout_bytes: bytes | None
) -> bytes | None:
    """Resolve cat-file blob bytes, preferring raw capture over decoded stdout.

    Without ``stdout_bytes``, intentional U+FFFD cannot be distinguished from
    ``decode(replace)`` artifacts — fail closed (``None``) when the decoded
    text contains NUL or U+FFFD.
    """
    if not ok:
        return None
    if stdout_bytes is not None:
        return stdout_bytes
    if "\0" in stdout or "\ufffd" in stdout:
        return None
    return stdout.encode("utf-8")


def _merge_file_result_matches_head(
    *, head_raw: bytes, stdout: str, stdout_bytes: bytes | None
) -> bool:
    """Return True when merge-file stdout equals the HEAD blob bytes."""
    if stdout_bytes is not None:
        return stdout_bytes == head_raw
    return stdout == head_raw.decode("utf-8")


def _prefix_leaves_open_disabling_context(prefix: str) -> bool:
    """Return True when ``prefix`` ends inside an open comment/string/#if region.

    Suffix (prepend) salvage retention must reject tips that place the salvage
    under an unterminated ``/*``, triple-quoted string, or ``#if`` / ``#ifdef`` /
    ``#ifndef`` — those keep a line-aligned suffix while disabling the fix
    (PRRT_kwDOSJAM6s6ZpaIn). Hash-line bodies are still scanned for trailing
    ``/*`` / triple-quotes (``#endif /*``, ``#define X /*``), and closing a
    multi-line ``*/`` keeps line-start so a same-line ``#if`` is not missed
    (PRRT_kwDOSJAM6s6ZpdMC). Closed wrappers and plain header lines return False.
    """
    in_block_comment = False
    in_triple_double = False
    in_triple_single = False
    if_depth = 0
    at_line_start = True
    i = 0
    n = len(prefix)
    while i < n:
        ch = prefix[i]
        if in_block_comment:
            if ch == "*" and i + 1 < n and prefix[i + 1] == "/":
                in_block_comment = False
                i += 2
                # Keep ``at_line_start`` from a prior newline inside the comment
                # so a same-line ``#if`` after ``*/`` is still seen
                # (PRRT_kwDOSJAM6s6ZpdMC).
                continue
            if ch == "\n":
                at_line_start = True
            i += 1
            continue
        if in_triple_double:
            if prefix.startswith('"""', i):
                in_triple_double = False
                i += 3
                continue
            if ch == "\n":
                at_line_start = True
            i += 1
            continue
        if in_triple_single:
            if prefix.startswith("'''", i):
                in_triple_single = False
                i += 3
                continue
            if ch == "\n":
                at_line_start = True
            i += 1
            continue
        if prefix.startswith("/*", i):
            in_block_comment = True
            i += 2
            at_line_start = False
            continue
        if prefix.startswith('"""', i):
            in_triple_double = True
            i += 3
            at_line_start = False
            continue
        if prefix.startswith("'''", i):
            in_triple_single = True
            i += 3
            at_line_start = False
            continue
        if at_line_start and ch == "#":
            # Skip whitespace between ``#`` and the directive keyword.
            j = i + 1
            while j < n and prefix[j] in " \t":
                j += 1
            rest = prefix[j:]
            matched_directive = False
            # Check longer ``ifdef`` / ``ifndef`` / ``endif`` before bare ``if``.
            for keyword, depth_delta in (
                ("ifdef", 1),
                ("ifndef", 1),
                ("endif", -1),
                ("if", 1),
            ):
                if not rest.startswith(keyword):
                    continue
                after = j + len(keyword)
                if after < n and (prefix[after].isalnum() or prefix[after] == "_"):
                    continue
                if depth_delta < 0:
                    if_depth = max(0, if_depth + depth_delta)
                else:
                    if_depth += depth_delta
                # Advance past the keyword; scan the rest of the line normally
                # so trailing ``/*`` / triple-quotes still open disabling context
                # (PRRT_kwDOSJAM6s6ZpdMC).
                i = after
                matched_directive = True
                break
            if not matched_directive:
                # Non-directive hash line (e.g. ``#define X /*``): skip ``#``
                # and keep scanning the body for openers.
                i += 1
            at_line_start = False
            continue
        if ch == "\n":
            at_line_start = True
            i += 1
            continue
        if not ch.isspace():
            at_line_start = False
        i += 1
    return in_block_comment or in_triple_double or in_triple_single or if_depth > 0


def _binding_name_for_line(raw_line: str) -> str | None:
    """Return the binding name on ``raw_line``, or None when it is not a binding.

    Pure ``#`` / ``//`` comment lines are skipped so commented rebinds do not
    count; ``#define`` / ``# define`` remain bindings (whitespace between ``#``
    and ``define`` is allowed, matching open-``#if`` scanning;
    PRRT_kwDOSJAM6s6Zp_sv).
    """
    stripped = raw_line.lstrip(" \t")
    if stripped.startswith("//"):
        return None
    if stripped.startswith("#") and _DEFINE_DIRECTIVE_LINE_RE.match(stripped) is None:
        return None
    for pattern in (
        _DEFINE_BINDING_RE,
        _DEF_BINDING_RE,
        _CLASS_BINDING_RE,
        _FUNCTION_BINDING_RE,
        _LET_CONST_BINDING_RE,
        _ASSIGN_BINDING_RE,
    ):
        match = pattern.match(raw_line)
        if match is not None:
            return match.group(1)
    return None


def _binding_names(text: str) -> set[str]:
    """Return names bound by assignments / defs / defines in ``text``.

    Used to detect when appended tip content rebinds a name from an added salvage
    blob (``FEATURE_ENABLED = True`` then ``FEATURE_ENABLED = False``), which
    keeps a line-aligned prefix while superseding the fix (PRRT_kwDOSJAM6s6Zp8jM).
    """
    names: set[str] = set()
    for raw_line in text.splitlines():
        name = _binding_name_for_line(raw_line)
        if name is not None:
            names.add(name)
    return names


def _salvage_changed_binding_names(*, parent_blob: str, commit_blob: str) -> set[str]:
    """Return names whose last binding line differs between parent and salvage."""
    parent_last: dict[str, str] = {}
    for raw_line in parent_blob.splitlines():
        name = _binding_name_for_line(raw_line)
        if name is not None:
            parent_last[name] = raw_line
    changed: set[str] = set()
    commit_last: dict[str, str] = {}
    for raw_line in commit_blob.splitlines():
        name = _binding_name_for_line(raw_line)
        if name is not None:
            commit_last[name] = raw_line
    for name, line in commit_last.items():
        if parent_last.get(name) != line:
            changed.add(name)
    return changed


def _tip_extra_can_supersede_modified_salvage(
    *, parent_blob: str, commit_blob: str, head_blob: str
) -> bool:
    """Return True when tip-only lines rebind a name the salvage changed vs parent.

    Baseline-backed retention uses clean ``git merge-file`` equality with HEAD.
    A tip can keep the salvage hunk and append a later rebinding of the same
    name (``FEATURE_ENABLED = True`` then ``FEATURE_ENABLED = False``); with
    surrounding context merge-file reproduces that tip cleanly, so equality
    alone would retain stale FIXED evidence. Only names whose last binding line
    changed vs parent count — unrelated appends and later hunks stay retained
    (PRRT_kwDOSJAM6s6Zp_3j).
    """
    changed = _salvage_changed_binding_names(parent_blob=parent_blob, commit_blob=commit_blob)
    if not changed:
        return False
    commit_lines = set(commit_blob.splitlines())
    extra_lines = [line for line in head_blob.splitlines() if line not in commit_lines]
    if not extra_lines:
        return False
    return bool(changed & _binding_names("\n".join(extra_lines) + "\n"))


def _suffix_can_supersede_added_salvage(*, salvage: str, suffix: str) -> bool:
    """Return True when ``suffix`` rebinds a name bound in ``salvage``."""
    if not suffix:
        return False
    salvage_names = _binding_names(salvage)
    if not salvage_names:
        return False
    return bool(salvage_names & _binding_names(suffix))


def _added_salvage_blob_retained(*, commit_blob: str, head_blob: str) -> bool:
    """Return True when an added salvage blob remains applied in ``head_blob``.

    Empty-base ``git merge-file`` conflicts on benign append/prepend, so additions
    use contiguous retention instead. Raw ``commit_blob in head_blob`` is too weak:
    commenting out an added call (``enable_guard()`` → ``# enable_guard()``) still
    contains the salvage bytes as a mid-line substring and would reuse stale
    evidence (PRRT_kwDOSJAM6s6Zm6F1). A mid-file whole-line occurrence is also too
    weak: nesting the salvage under ``#if 0`` / a multiline comment / string keeps
    line-boundary alignment while disabling the fix (PRRT_kwDOSJAM6s6ZpQKt).
    Retain only a line-boundary-aligned **prefix** (append / exact) or **suffix**
    (prepend): the match must start at file start or after a newline, and if the
    salvage lacks a trailing newline it must end at EOF or before a newline.
    Suffix retention additionally rejects a prepend that leaves an open block
    comment, triple-quoted string, or ``#if`` region (PRRT_kwDOSJAM6s6ZpaIn).
    Prefix retention with a non-empty append additionally rejects when the
    appended suffix rebinds a name bound in the salvage
    (PRRT_kwDOSJAM6s6Zp8jM).

    An empty salvage blob (new empty file) is a vacuous substring of every tip;
    retain only when the tip blob is also exactly empty (PRRT_kwDOSJAM6s6ZpEZh).
    """
    if not commit_blob:
        return not head_blob

    def _line_aligned_at(idx: int) -> bool:
        if idx < 0 or idx + len(commit_blob) > len(head_blob):
            return False
        if head_blob[idx : idx + len(commit_blob)] != commit_blob:
            return False
        if not (idx == 0 or head_blob[idx - 1] == "\n"):
            return False
        end = idx + len(commit_blob)
        return commit_blob.endswith("\n") or end == len(head_blob) or head_blob[end] == "\n"

    # Append / exact: salvage remains a line-aligned prefix of the tip. Exact
    # match retains; a longer tip retains only when the appended suffix cannot
    # supersede (rebind) names from the salvage (PRRT_kwDOSJAM6s6Zp8jM).
    if _line_aligned_at(0):
        if len(head_blob) == len(commit_blob):
            return True
        return not _suffix_can_supersede_added_salvage(
            salvage=commit_blob,
            suffix=head_blob[len(commit_blob) :],
        )
    # Prepend: salvage remains a line-aligned suffix (not already covered above),
    # and the prepended prefix must not leave an open disabling context.
    suffix_idx = len(head_blob) - len(commit_blob)
    if suffix_idx <= 0 or not _line_aligned_at(suffix_idx):
        return False
    return not _prefix_leaves_open_disabling_context(head_blob[:suffix_idx])


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
