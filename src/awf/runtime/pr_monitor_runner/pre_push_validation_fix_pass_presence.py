"""Salvage presence / tree-entry helpers for pre-push validation fix passes."""

from __future__ import annotations

import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.path_helpers import _changed_paths_from_name_only_z
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
    _git_env_for_merge_safety_object_lookup,
)
from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError

# Binding targets that an appended tip can rebind to supersede added salvage.
# Optional YAML block-sequence marker (``- ``) before the key so list-item
# mappings bind like nested leaves (PRRT_kwDOSJAM6s6ZqeWt).
# Optional shell ``export `` / ``declare … `` / ``typeset … `` before the key so
# ``export FEATURE_ENABLED=true`` and ``declare -x FEATURE_ENABLED=true`` bind
# like bare ``FEATURE_ENABLED=true`` (PRRT_kwDOSJAM6s6ZqseO,
# PRRT_kwDOSJAM6s6ZqxX4). Declaration forms (``export class`` / ``export const``)
# still match earlier patterns.
# Bare keys allow ``-`` so TOML / YAML hyphenated names bind
# (``feature-enabled = true``; PRRT_kwDOSJAM6s6Zqip3).
# TOML dotted keys join bare or quoted segments with ``.``
# (``feature.enabled`` / ``site."google.com"``; PRRT_kwDOSJAM6s6Zql88).
_ASSIGN_KEY_SEGMENT = r'(?:[A-Za-z_][A-Za-z0-9_-]*|"[^"\n]+"|\'[^\'\n]+\')'
_ASSIGN_BINDING_RE = re.compile(
    r"(?m)^[ \t]*(?:-[ \t]+)?(?:export[ \t]+|(?:declare|typeset)(?:[ \t]+-[A-Za-z]+)*[ \t]+)?(?:"
    # Dotted TOML keys (≥1 ``.``): require the full path before ``=`` / ``:``
    # so ``feature.enabled = true`` binds as ``feature.enabled``, not nothing.
    rf"({_ASSIGN_KEY_SEGMENT}(?:\.{_ASSIGN_KEY_SEGMENT})+)"
    r"(?:"
    r"(?:[ \t]*:[ \t]*[^=\n]+)?[ \t]*=(?!=)"
    r"|"
    r"[ \t]*:="
    r"|"
    r"[ \t]*:[ \t]*(?!=)"
    r")"
    r"|"
    r"([A-Za-z_][A-Za-z0-9_-]*)"
    r"(?:"
    r"(?:[ \t]*:[ \t]*[^=\n]+)?[ \t]*=(?!=)"  # `name =` / `name: T =`
    r"|"
    r"[ \t]*:="  # `name :=`
    r"|"
    # YAML / mapping ``name: value`` (no equals). Must not steal ``name :=``
    # (handled above) or typed assignments (first alt). Plain ``name:`` with an
    # empty / scalar value still counts so config overrides fail closed
    # (PRRT_kwDOSJAM6s6ZqNAk).
    r"[ \t]*:[ \t]*(?!=)"
    r")"
    r"|"
    # Quoted JSON/YAML mapping keys (``"feature-enabled": …`` / ``'k': …``)
    # and quoted TOML keys (``"feature-enabled" = …``). Include the surrounding
    # quotes in the capture so a TOML key whose name contains ``.`` (``"a.b"``)
    # stays one segment and does not collapse with dotted ``a.b``; YAML/JSON
    # ``:`` bindings strip without re-quoting so the same spellings still match
    # (PRRT_kwDOSJAM6s6ZqQfh, PRRT_kwDOSJAM6s6Zqip3, PRRT_kwDOSJAM6s6ZqoYV,
    # PRRT_kwDOSJAM6s6ZqtHj).
    r'("[^"\n]+")[ \t]*(?::[ \t]*(?!=)|=(?!=))'
    r"|"
    r"('[^'\n]+')[ \t]*(?::[ \t]*(?!=)|=(?!=))"
    r")"
)
_ASSIGN_KEY_SEGMENT_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_-]*|"[^"\n]+"|\'[^\'\n]+\')')
_BARE_ASSIGN_KEY_SEGMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
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
# YAML / mapping ``key:`` (or quoted ``"key":`` / ``'key':``) with no same-line
# scalar — only optional whitespace and a ``#`` comment. These open an
# indentation scope so nested leaves qualify as ``parent.leaf`` rather than
# colliding as bare ``leaf`` across unrelated mappings (PRRT_kwDOSJAM6s6ZqZo2).
# Optional ``- `` covers block-sequence mapping openers (``- nested:``;
# PRRT_kwDOSJAM6s6ZqeWt). Bare Python control-flow headers (``else:`` / ``try:``
# / ``except:`` / ``finally:``) are excluded so tip rebinds under those blocks
# stay bare keys and can supersede salvage; quoted ``"else":`` still nests
# (PRRT_kwDOSJAM6s6Zqeen).
_YAML_MAPPING_SCOPE_OPENER_RE = re.compile(
    r"^[ \t]*(?:-[ \t]+)?(?:"
    r"(?!(?:else|try|except|finally)[ \t]*:)"
    r"[A-Za-z_][A-Za-z0-9_]*"
    r'|"[^"\n]+"'
    r"|'[^'\n]+'"
    r")[ \t]*:[ \t]*(?:#.*)?$"
)
# TOML ``[table]`` / ``[[array.table]]`` headers replace the current table scope
# so leaves under different tables qualify distinctly (``feature.enabled`` vs
# ``logging.enabled``; PRRT_kwDOSJAM6s6ZqpBC). Key path reuses assign segments
# (bare / quoted / dotted). Closing brackets must match opener count.
_TOML_TABLE_HEADER_RE = re.compile(
    r"^[ \t]*(\[{1,2})[ \t]*"
    rf"({_ASSIGN_KEY_SEGMENT}(?:\.{_ASSIGN_KEY_SEGMENT})*)"
    r"[ \t]*(\]{1,2})[ \t]*(?:#.*)?$"
)


def _advance_string_or_block_comment_state(
    chunk: str,
    *,
    in_block_comment: bool,
    in_triple_double: bool,
    in_triple_single: bool,
) -> tuple[bool, bool, bool]:
    """Advance ``/*`` / triple-quote state through ``chunk`` (may include newlines).

    Binding scanners use this so Google-style docstring prose
    (``timeout: Seconds…``) is not treated as a YAML-style rebind
    (PRRT_kwDOSJAM6s6ZqPO9). Ordinary ``"..."`` / ``'...'`` strings (with ``\\``
    escapes) and ``#`` / ``//`` line comments are opaque so a URL/glob or
    comment containing ``/*`` / nested quotes cannot open state and hide a
    later real rebind (PRRT_kwDOSJAM6s6ZqSbO). Matches the opener/closer
    vocabulary that ``_prefix_leaves_open_disabling_context`` already tracks
    for prepend checks; ``#if`` depth is intentionally omitted here (dead-code
    rebinds stay fail-closed).
    """
    i = 0
    n = len(chunk)
    in_double_string = False
    in_single_string = False
    while i < n:
        if in_block_comment:
            if chunk.startswith("*/", i):
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_triple_double:
            if chunk.startswith('"""', i):
                in_triple_double = False
                i += 3
                continue
            i += 1
            continue
        if in_triple_single:
            if chunk.startswith("'''", i):
                in_triple_single = False
                i += 3
                continue
            i += 1
            continue
        if in_double_string:
            ch = chunk[i]
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                in_double_string = False
            i += 1
            continue
        if in_single_string:
            ch = chunk[i]
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == "'":
                in_single_string = False
            i += 1
            continue
        # Line comments are opaque: do not treat ``/*`` / quotes inside them as
        # openers (PRRT_kwDOSJAM6s6ZqSbO).
        if chunk.startswith("//", i):
            while i < n and chunk[i] != "\n":
                i += 1
            continue
        if chunk[i] == "#":
            while i < n and chunk[i] != "\n":
                i += 1
            continue
        if chunk.startswith("/*", i):
            in_block_comment = True
            i += 2
            continue
        if chunk.startswith('"""', i):
            in_triple_double = True
            i += 3
            continue
        if chunk.startswith("'''", i):
            in_triple_single = True
            i += 3
            continue
        if chunk[i] == '"':
            in_double_string = True
            i += 1
            continue
        if chunk[i] == "'":
            in_single_string = True
            i += 1
            continue
        i += 1
    return in_block_comment, in_triple_double, in_triple_single


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


def _format_normalized_assign_key_segment(segment: str, *, requote_non_bare: bool = True) -> str:
    """Return ``segment`` bare when valid; otherwise keep a quoted form.

    Bare TOML/YAML key segments round-trip without quotes. For TOML ``=``
    bindings, segments that contain ``.`` or other non-bare characters must
    stay quoted so joining with ``.`` does not invent extra path boundaries
    (PRRT_kwDOSJAM6s6ZqoYV). YAML/JSON ``:`` bindings pass
    ``requote_non_bare=False`` because ``"a.b"`` and ``a.b`` are one key
    (PRRT_kwDOSJAM6s6ZqtHj).
    """
    if _BARE_ASSIGN_KEY_SEGMENT_RE.fullmatch(segment):
        return segment
    if not requote_non_bare:
        return segment
    if '"' not in segment:
        return f'"{segment}"'
    return f"'{segment}'"


def _normalize_assign_binding_name(name: str, *, requote_non_bare: bool = True) -> str:
    """Strip redundant quotes from key segments for stable comparison.

    ``feature.enabled``, ``feature."enabled"``, and ``"feature".enabled`` all
    normalize to ``feature.enabled`` so mixed spellings supersede each other
    (PRRT_kwDOSJAM6s6Zql88). For TOML ``=`` bindings, segments that are not
    valid bare keys (for example ``google.com``) keep quotes after normalize so
    ``site."google.com"`` stays distinct from ``site.google.com``, and a quoted
    key ``"a.b"`` stays distinct from dotted ``a.b`` (PRRT_kwDOSJAM6s6ZqoYV).
    YAML/JSON ``:`` bindings set ``requote_non_bare=False`` so quote-only
    rebinds of the same key still intersect (PRRT_kwDOSJAM6s6ZqtHj).
    Non-segment names are unchanged.
    """
    if "." not in name and name[:1] not in "\"'":
        return name
    segments: list[str] = []
    pos = 0
    length = len(name)
    while pos < length:
        if segments:
            if name[pos] != ".":
                return name
            pos += 1
        match = _ASSIGN_KEY_SEGMENT_RE.match(name, pos)
        if match is None:
            return name
        raw = match.group(1)
        if raw.startswith('"') or raw.startswith("'"):
            segments.append(raw[1:-1])
        else:
            segments.append(raw)
        pos = match.end()
    if pos != length or not segments:
        return name
    return ".".join(
        _format_normalized_assign_key_segment(segment, requote_non_bare=requote_non_bare)
        for segment in segments
    )


def _binding_name_for_line(raw_line: str) -> str | None:
    """Return the binding name on ``raw_line``, or None when it is not a binding.

    Pure ``#`` / ``//`` comment lines are skipped so commented rebinds do not
    count; ``#define`` / ``# define`` remain bindings (whitespace between ``#``
    and ``define`` is allowed, matching open-``#if`` scanning;
    PRRT_kwDOSJAM6s6Zp_sv). Callers must also skip lines that start inside an
    open ``/*`` or triple-quoted string so docstring prose is not treated as a
    YAML-style rebind (PRRT_kwDOSJAM6s6ZqPO9).
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
            for group in match.groups():
                if group:
                    # Colon-only YAML/JSON matches have no ``=`` in the binder
                    # span (value text is not captured). Those must not re-quote
                    # non-bare segments: ``"a.b"`` and ``a.b`` are one key
                    # (PRRT_kwDOSJAM6s6ZqtHj). TOML/assign ``=`` keeps quotes.
                    requote_non_bare = pattern is not _ASSIGN_BINDING_RE or "=" in match.group(0)
                    return _normalize_assign_binding_name(group, requote_non_bare=requote_non_bare)
            return None
    return None


def _binding_names(text: str) -> set[str]:
    """Return names bound by assignments / defs / defines in ``text``.

    Used to detect when appended tip content rebinds a name from an added salvage
    blob (``FEATURE_ENABLED = True`` then ``FEATURE_ENABLED = False``, shell
    ``export FEATURE_ENABLED=true`` / ``declare -x FEATURE_ENABLED=true`` then a
    later false rebind, YAML ``feature_enabled: true`` then ``feature_enabled:
    false``, quoted JSON ``"feature-enabled": true`` then ``"feature-enabled":
    false``, TOML ``feature-enabled = true`` / ``"feature-enabled" = true``, or
    TOML dotted ``feature.enabled = true`` / ``feature."enabled" = true``), which
    keeps a line-aligned prefix while superseding the fix (PRRT_kwDOSJAM6s6Zp8jM,
    PRRT_kwDOSJAM6s6ZqseO, PRRT_kwDOSJAM6s6ZqxX4, PRRT_kwDOSJAM6s6ZqNAk,
    PRRT_kwDOSJAM6s6ZqQfh, PRRT_kwDOSJAM6s6Zqip3, PRRT_kwDOSJAM6s6Zql88). Lines
    that start inside ``/*`` or a triple-quoted string are skipped so Google-style
    docstring Args prose cannot falsely supersede (PRRT_kwDOSJAM6s6ZqPO9).
    """
    names: set[str] = set()
    in_block_comment = False
    in_triple_double = False
    in_triple_single = False
    for raw_line in text.splitlines():
        if not (in_block_comment or in_triple_double or in_triple_single):
            name = _binding_name_for_line(raw_line)
            if name is not None:
                names.add(name)
        in_block_comment, in_triple_double, in_triple_single = (
            _advance_string_or_block_comment_state(
                raw_line + "\n",
                in_block_comment=in_block_comment,
                in_triple_double=in_triple_double,
                in_triple_single=in_triple_single,
            )
        )
    return names


def _is_declaration_opener_line(raw_line: str) -> bool:
    """Return True for def/class/function/let/const/var/#define opener lines.

    Same-signature redefinitions reuse identical opener text, so tip-extra
    detection needs multiset counting for these lines. Assignments are excluded:
    value rebinds already change line text, and surplus copies of salvage
    assignment text in unrelated hunks must not look like supersession
    (PRRT_kwDOSJAM6s6ZqGeU).
    """
    stripped = raw_line.lstrip(" \t")
    if stripped.startswith("//"):
        return False
    if stripped.startswith("#") and _DEFINE_DIRECTIVE_LINE_RE.match(stripped) is None:
        return False
    for pattern in (
        _DEFINE_BINDING_RE,
        _DEF_BINDING_RE,
        _CLASS_BINDING_RE,
        _FUNCTION_BINDING_RE,
        _LET_CONST_BINDING_RE,
    ):
        if pattern.match(raw_line) is not None:
            return True
    return False


def _opens_nested_binding_scope(raw_line: str) -> bool:
    """Return True when ``raw_line`` opens a nestable binding scope.

    Def/class/function openers push scopes for qualified keys
    (``A.ok``; PRRT_kwDOSJAM6s6ZqKN3). YAML/mapping ``key:`` lines with no
    same-line scalar also push so ``feature.enabled`` and ``logging.enabled``
    stay distinct (PRRT_kwDOSJAM6s6ZqZo2), including block-sequence openers
    (``- nested:``; PRRT_kwDOSJAM6s6ZqeWt). Bare ``else:`` / ``try:`` /
    ``except:`` / ``finally:`` do not push (PRRT_kwDOSJAM6s6Zqeen). Assignments
    with values, ``#define``, and ``let``/``const``/``var`` bind a name but do
    not push. TOML ``[table]`` / ``[[array]]`` headers are not nestable indent
    openers; callers track them via ``_toml_table_header_path``
    (PRRT_kwDOSJAM6s6ZqpBC).
    """
    stripped = raw_line.lstrip(" \t")
    if stripped.startswith("//"):
        return False
    if stripped.startswith("#"):
        return False
    for pattern in (_DEF_BINDING_RE, _CLASS_BINDING_RE, _FUNCTION_BINDING_RE):
        if pattern.match(raw_line) is not None:
            return True
    return _YAML_MAPPING_SCOPE_OPENER_RE.match(raw_line) is not None


def _toml_table_header_path(raw_line: str) -> str | None:
    """Return normalized TOML table path for a ``[table]`` / ``[[array]]`` line.

    Matching opener/closer bracket counts are required so ``[a]]`` / ``[[a]``
    do not invent a table scope. The path is normalized like assign keys
    (``feature.sub`` / ``"feature"`` → ``feature``) so leaves qualify as
    ``feature.enabled`` under both spellings (PRRT_kwDOSJAM6s6ZqpBC).
    """
    match = _TOML_TABLE_HEADER_RE.match(raw_line)
    if match is None:
        return None
    opener, raw_path, closer = match.group(1), match.group(2), match.group(3)
    if len(opener) != len(closer):
        return None
    return _normalize_assign_binding_name(raw_path)


def _line_indent(raw_line: str) -> int:
    """Return leading space/tab count for ``raw_line``."""
    return len(raw_line) - len(raw_line.lstrip(" \t"))


def _scoped_binding_key(scope_names: list[str], name: str) -> str:
    """Qualify ``name`` with enclosing scope names (``A.ok``), or return bare."""
    if not scope_names:
        return name
    return ".".join((*scope_names, name))


def _binding_span_at(lines: list[str], start: int) -> tuple[str, ...]:
    """Return opener-plus-body lines for the binding starting at ``start``.

    Continues through blank lines and lines indented strictly deeper than the
    opener so body-only edits (same ``def``/``class``/``function`` line, different
    body) compare as a changed binding (PRRT_kwDOSJAM6s6ZqHvh). Trailing blanks
    after the last body line are dropped for stable comparison.
    """
    opener = lines[start]
    opener_indent = _line_indent(opener)
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() == "":
            end += 1
            continue
        if _line_indent(line) <= opener_indent:
            break
        end += 1
    while end > start + 1 and lines[end - 1].strip() == "":
        end -= 1
    return tuple(lines[start:end])


def _last_binding_spans(text: str) -> dict[str, tuple[str, ...]]:
    """Map each scoped binding key to the span of its last occurrence in ``text``.

    Keys are qualified by enclosing def/class/function scopes (``A.ok``), by
    YAML/mapping openers with no same-line scalar (``feature.enabled``), and by
    the current TOML ``[table]`` / ``[[array]]`` path so same-named leaves under
    different parents do not collide (PRRT_kwDOSJAM6s6ZqKN3,
    PRRT_kwDOSJAM6s6ZqZo2, PRRT_kwDOSJAM6s6ZqpBC). Lines that start inside ``/*``
    or a triple-quoted string are ignored so docstring prose does not invent
    bindings (PRRT_kwDOSJAM6s6ZqPO9).
    """
    lines = text.splitlines()
    last_start: dict[str, int] = {}
    # (indent, name) stack for nestable declaration openers.
    scope_stack: list[tuple[int, str]] = []
    # Current TOML table path (replaced by each table/array-table header).
    toml_table_path: str | None = None
    in_block_comment = False
    in_triple_double = False
    in_triple_single = False
    for idx, raw_line in enumerate(lines):
        line_in_non_code = in_block_comment or in_triple_double or in_triple_single
        in_block_comment, in_triple_double, in_triple_single = (
            _advance_string_or_block_comment_state(
                raw_line + "\n",
                in_block_comment=in_block_comment,
                in_triple_double=in_triple_double,
                in_triple_single=in_triple_single,
            )
        )
        if line_in_non_code or raw_line.strip() == "":
            continue
        table_path = _toml_table_header_path(raw_line)
        if table_path is not None:
            toml_table_path = table_path
            scope_stack.clear()
            continue
        indent = _line_indent(raw_line)
        while scope_stack and scope_stack[-1][0] >= indent:
            scope_stack.pop()
        name = _binding_name_for_line(raw_line)
        if name is None:
            continue
        scope_names = ([toml_table_path] if toml_table_path is not None else []) + [
            entry[1] for entry in scope_stack
        ]
        key = _scoped_binding_key(scope_names, name)
        last_start[key] = idx
        if _opens_nested_binding_scope(raw_line):
            scope_stack.append((indent, name))
    return {key: _binding_span_at(lines, start) for key, start in last_start.items()}


def _tip_extra_line_indices(*, commit_blob: str, head_blob: str) -> set[int]:
    """Return head line indices that are tip-only vs the salvage commit blob.

    Declaration openers use multiset counting so same-signature redefinitions
    remain tip-extra (PRRT_kwDOSJAM6s6ZqDij); other lines use set difference so
    surplus salvage assignment copies are not tip-extra (PRRT_kwDOSJAM6s6ZqGeU).
    """
    commit_lines = commit_blob.splitlines()
    commit_set = set(commit_lines)
    opener_remaining = Counter(line for line in commit_lines if _is_declaration_opener_line(line))
    extra: set[int] = set()
    for idx, line in enumerate(head_blob.splitlines()):
        if _is_declaration_opener_line(line):
            if opener_remaining[line] > 0:
                opener_remaining[line] -= 1
            else:
                extra.add(idx)
        elif line not in commit_set:
            extra.add(idx)
    return extra


def _scoped_binding_keys_on_lines(*, text: str, line_indices: set[int]) -> set[str]:
    """Return scoped binding keys whose opener lines fall in ``line_indices``.

    Lines that start inside ``/*`` or a triple-quoted string are ignored so
    tip-extra docstring prose cannot look like a rebind (PRRT_kwDOSJAM6s6ZqPO9).
    """
    if not line_indices:
        return set()
    lines = text.splitlines()
    keys: set[str] = set()
    scope_stack: list[tuple[int, str]] = []
    toml_table_path: str | None = None
    in_block_comment = False
    in_triple_double = False
    in_triple_single = False
    for idx, raw_line in enumerate(lines):
        line_in_non_code = in_block_comment or in_triple_double or in_triple_single
        in_block_comment, in_triple_double, in_triple_single = (
            _advance_string_or_block_comment_state(
                raw_line + "\n",
                in_block_comment=in_block_comment,
                in_triple_double=in_triple_double,
                in_triple_single=in_triple_single,
            )
        )
        if line_in_non_code or raw_line.strip() == "":
            continue
        table_path = _toml_table_header_path(raw_line)
        if table_path is not None:
            toml_table_path = table_path
            scope_stack.clear()
            continue
        indent = _line_indent(raw_line)
        while scope_stack and scope_stack[-1][0] >= indent:
            scope_stack.pop()
        name = _binding_name_for_line(raw_line)
        if name is None:
            continue
        scope_names = ([toml_table_path] if toml_table_path is not None else []) + [
            entry[1] for entry in scope_stack
        ]
        key = _scoped_binding_key(scope_names, name)
        if idx in line_indices:
            keys.add(key)
        if _opens_nested_binding_scope(raw_line):
            scope_stack.append((indent, name))
    return keys


def _salvage_changed_binding_names(*, parent_blob: str, commit_blob: str) -> set[str]:
    """Return scoped keys whose last binding span differs between parent and salvage.

    Spans include declaration bodies, not only opener lines, so body-only
    function/class edits count as changed bindings (PRRT_kwDOSJAM6s6ZqHvh).
    Keys are scope-qualified so ``A.ok`` and ``C.ok`` stay distinct
    (PRRT_kwDOSJAM6s6ZqKN3). Parent-only names (deleted by salvage) also count
    so a tip that reintroduces them can supersede (PRRT_kwDOSJAM6s6ZqKGY).
    """
    parent_spans = _last_binding_spans(parent_blob)
    commit_spans = _last_binding_spans(commit_blob)
    return {
        name
        for name in parent_spans.keys() | commit_spans.keys()
        if parent_spans.get(name) != commit_spans.get(name)
    }


def _tip_extra_can_supersede_modified_salvage(
    *, parent_blob: str, commit_blob: str, head_blob: str
) -> bool:
    """Return True when tip-only lines rebind a name the salvage changed vs parent.

    Baseline-backed retention uses clean ``git merge-file`` equality with HEAD.
    A tip can keep the salvage hunk and append a later rebinding of the same
    name (``FEATURE_ENABLED = True`` then ``FEATURE_ENABLED = False``, or shell
    ``export`` / ``declare -x`` / ``typeset`` forms of the same name); with
    surrounding context merge-file reproduces that tip cleanly, so equality
    alone would retain stale FIXED evidence. Only scoped keys whose last binding
    span (opener plus indented body) changed vs parent count — unrelated appends
    and later hunks stay retained (PRRT_kwDOSJAM6s6Zp_3j, PRRT_kwDOSJAM6s6ZqseO,
    PRRT_kwDOSJAM6s6ZqxX4). Tip-extra lines use
    set difference except for declaration openers, which need multiset counting
    so same-signature redefinitions are not dropped (PRRT_kwDOSJAM6s6ZqDij);
    full-line multiset would over-reject surplus salvage assignment copies
    (PRRT_kwDOSJAM6s6ZqGeU). Body-only declaration edits still count as changed
    bindings (PRRT_kwDOSJAM6s6ZqHvh). Parent-only (deleted) salvage names also
    count so tip reintroduction supersedes (PRRT_kwDOSJAM6s6ZqKGY).     Tip-extra binding keys are resolved against the full tip blob so an
    unrelated later ``def ok`` under another class does not collide with salvaged
    ``A.ok`` (PRRT_kwDOSJAM6s6ZqKN3), nested YAML ``logging.enabled`` does not
    collide with salvaged ``feature.enabled`` (PRRT_kwDOSJAM6s6ZqZo2), and
    TOML ``[logging] enabled`` does not collide with salvaged
    ``[feature] enabled`` (PRRT_kwDOSJAM6s6ZqpBC).
    """
    changed = _salvage_changed_binding_names(parent_blob=parent_blob, commit_blob=commit_blob)
    if not changed:
        return False
    extra_indices = _tip_extra_line_indices(commit_blob=commit_blob, head_blob=head_blob)
    if not extra_indices:
        return False
    tip_extra_keys = _scoped_binding_keys_on_lines(text=head_blob, line_indices=extra_indices)
    return bool(changed & tip_extra_keys)


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
