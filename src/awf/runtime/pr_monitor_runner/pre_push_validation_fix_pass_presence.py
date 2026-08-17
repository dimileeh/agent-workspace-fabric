"""Salvage presence / tree-entry helpers for pre-push validation fix passes."""

from __future__ import annotations

import re
from collections import Counter

from awf.runtime.pr_monitor_runner.helpers_verdict import (
    _ascii_double_quote_is_delimiter,
    _ascii_single_quote_is_delimiter,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_assigns import (
    _ASSIGN_KEY_SEGMENT as _ASSIGN_KEY_SEGMENT,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_assigns import (
    _CLASS_BINDING_RE as _CLASS_BINDING_RE,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_assigns import (
    _DEF_BINDING_RE as _DEF_BINDING_RE,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_assigns import (
    _FUNCTION_BINDING_RE as _FUNCTION_BINDING_RE,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_assigns import (
    _binding_names_for_line as _binding_names_for_line,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_assigns import (
    _join_incomplete_object_mutation_line as _join_incomplete_object_mutation_line,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_assigns import (
    _join_incomplete_object_mutation_line_covering as _join_incomplete_object_mutation_line_covering,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_assigns import (
    _normalize_assign_binding_name as _normalize_assign_binding_name,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_calls import (
    _call_site_names_for_line as _call_site_names_for_line,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_calls import (
    _candidate_keys_include_call_name as _candidate_keys_include_call_name,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_calls import (
    _is_member_call_continuation as _is_member_call_continuation,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_calls import (
    _join_member_call_continuation_line as _join_member_call_continuation_line,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_context import (  # noqa: F401
    _bytes_unsafe_for_text_merge as _bytes_unsafe_for_text_merge,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_context import (  # noqa: F401
    _delta_brackets_outside_strings as _delta_brackets_outside_strings,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_context import (  # noqa: F401
    _git_mode_file_kind as _git_mode_file_kind,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_context import (  # noqa: F401
    _line_code_without_line_comment as _line_code_without_line_comment,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_context import (
    _line_is_control_flow_change as _line_is_control_flow_change,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_context import (  # noqa: F401
    _merge_file_result_matches_head as _merge_file_result_matches_head,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_context import (  # noqa: F401
    _parse_ls_tree_meta as _parse_ls_tree_meta,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_context import (
    _prefix_leaves_open_disabling_context as _prefix_leaves_open_disabling_context,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_context import (
    _prefix_opens_control_flow_over_suffix as _prefix_opens_control_flow_over_suffix,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_context import (  # noqa: F401
    _raw_blob_from_cat_file_result as _raw_blob_from_cat_file_result,
)

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
# Scalar YAML sequence items (``- name: a``) open an identity scope so sibling
# items do not collapse shared leaves into ``features.enabled``
# (PRRT_kwDOSJAM6s6ZqxYE). Bare keys allow ``-`` like assign bindings so
# ``- feature-name: a`` opens identity (PRRT_kwDOSJAM6s6Zq13_). Requires a
# non-empty same-line scalar; empty ``- nested:`` openers stay on
# ``_YAML_MAPPING_SCOPE_OPENER_RE``. Quoted values may contain ``#``; bare
# values still stop at a ``#`` comment so ``"a#1"`` / ``"a#2"`` stay distinct
# identities (PRRT_kwDOSJAM6s6Zq135).
_YAML_SCALAR_SEQUENCE_ITEM_RE = re.compile(
    r"^[ \t]*-[ \t]+(?:"
    r"(?!(?:else|try|except|finally)[ \t]*:)"
    r"(?P<bare>[A-Za-z_][A-Za-z0-9_-]*)"
    r'|(?P<double>"[^"\n]+")'
    r"|(?P<single>'[^'\n]+')"
    r")[ \t]*:[ \t]*(?P<value>"
    r'"[^"\n]+"'
    r"|'[^'\n]+'"
    r"|[^ \t#\n\"'][^#\n]*?"
    r")[ \t]*(?:#.*)?$"
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
    later real rebind (PRRT_kwDOSJAM6s6ZqSbO). Possessives, contractions, and
    inch marks are not string openers (PRRT_kwDOSJAM6s6Zq7kr). Matches the
    opener/closer vocabulary that ``_prefix_leaves_open_disabling_context``
    already tracks for prepend checks; ``#if`` depth is intentionally omitted
    here (dead-code rebinds stay fail-closed).
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
            if ch == '"' and _ascii_double_quote_is_delimiter(chunk, i, True):
                in_double_string = False
            i += 1
            continue
        if in_single_string:
            ch = chunk[i]
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == "'" and _ascii_single_quote_is_delimiter(chunk, i, True):
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
        if chunk[i] == '"' and _ascii_double_quote_is_delimiter(chunk, i, False):
            in_double_string = True
            i += 1
            continue
        if chunk[i] == "'" and _ascii_single_quote_is_delimiter(chunk, i, False):
            in_single_string = True
            i += 1
            continue
        i += 1
    return in_block_comment, in_triple_double, in_triple_single


def _normalize_yaml_sequence_item_scalar(raw: str) -> str:
    """Strip surrounding quotes from a YAML sequence-item scalar for identity."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _yaml_scalar_sequence_item_scope(raw_line: str) -> tuple[str, str] | None:
    """Return ``(scope_segment, item_key)`` for ``- key: scalar`` lines.

    ``scope_segment`` is ``key.scalar`` so sibling items qualify distinct leaves
    (``features.name.a.enabled`` vs ``features.name.b.enabled``;
    PRRT_kwDOSJAM6s6ZqxYE). ``item_key`` is the mapping key on the sequence
    marker so a same-item nested rebind of that key keeps the opener path
    (``- enabled: true`` then ``enabled: false`` → ``feature.enabled.true``;
    PRRT_kwDOSJAM6s6ZqeWt).
    """
    match = _YAML_SCALAR_SEQUENCE_ITEM_RE.match(raw_line)
    if match is None:
        return None
    raw_key = match.group("bare") or match.group("double") or match.group("single")
    if raw_key is None:  # pragma: no cover - alternation always captures a key group
        return None
    value = _normalize_yaml_sequence_item_scalar(match.group("value"))
    if not value:  # pragma: no cover - value alt requires non-empty scalar text
        return None
    item_key = _normalize_assign_binding_name(raw_key, requote_non_bare=False)
    return (f"{item_key}.{value}", item_key)


def _scoped_binding_key_for_line(
    scope_stack: list[tuple[int, str, str | None]],
    *,
    binding_name: str,
    raw_line: str,
    toml_table_path: str | None,
) -> str:
    """Return the scoped binding key for ``raw_line`` under ``scope_stack``.

    Scalar sequence-item openers (``- name: a``) use the identity segment as the
    leaf so siblings do not share ``features.name`` (PRRT_kwDOSJAM6s6ZqxYE). A
    nested rebind of that item's inline key reuses the opener's identity path
    (PRRT_kwDOSJAM6s6ZqeWt).
    """
    parent_segments = [entry[1] for entry in scope_stack]
    if toml_table_path is not None:
        parent_names = [toml_table_path, *parent_segments]
    else:
        parent_names = parent_segments
    seq_scope = _yaml_scalar_sequence_item_scope(raw_line)
    if seq_scope is not None:
        return _scoped_binding_key(parent_names, seq_scope[0])
    if scope_stack and scope_stack[-1][2] == binding_name:
        # Same-item rebind of the inline sequence-item key: match the opener.
        ancestor = parent_names[:-1]
        return _scoped_binding_key(ancestor, scope_stack[-1][1])
    return _scoped_binding_key(parent_names, binding_name)


def _opens_nested_binding_scope(raw_line: str) -> bool:
    """Return True when ``raw_line`` opens a nestable binding scope.

    Def/class/function openers push scopes for qualified keys
    (``A.ok``; PRRT_kwDOSJAM6s6ZqKN3). YAML/mapping ``key:`` lines with no
    same-line scalar also push so ``feature.enabled`` and ``logging.enabled``
    stay distinct (PRRT_kwDOSJAM6s6ZqZo2), including block-sequence openers
    (``- nested:``; PRRT_kwDOSJAM6s6ZqeWt). Scalar sequence items
    (``- name: a``) also push, with key+value identity so sibling items do not
    share leaf paths (PRRT_kwDOSJAM6s6ZqxYE). Bare ``else:`` / ``try:`` /
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
    if _yaml_scalar_sequence_item_scope(raw_line) is not None:
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


def _binding_span_end_exclusive(lines: list[str], start: int) -> int:
    """Return exclusive end index for the binding span starting at ``start``."""
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
    return end


def _binding_span_at(lines: list[str], start: int) -> tuple[str, ...]:
    """Return opener-plus-body lines for the binding starting at ``start``.

    Continues through blank lines and lines indented strictly deeper than the
    opener so body-only edits (same ``def``/``class``/``function`` line, different
    body) compare as a changed binding (PRRT_kwDOSJAM6s6ZqHvh). Trailing blanks
    after the last body line are dropped for stable comparison.
    """
    return tuple(lines[start : _binding_span_end_exclusive(lines, start)])


def _last_binding_start_indices(text: str) -> dict[str, int]:
    """Map each scoped binding key to the line index of its last opener in ``text``."""
    lines = text.splitlines()
    last_start: dict[str, int] = {}
    # (indent, segment, sequence_item_key|None) for nestable openers.
    scope_stack: list[tuple[int, str, str | None]] = []
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
        scan_line = _join_incomplete_object_mutation_line(lines, idx)
        names = _binding_names_for_line(scan_line)
        if not names:
            continue
        for name in names:
            key = _scoped_binding_key_for_line(
                scope_stack,
                binding_name=name,
                raw_line=scan_line,
                toml_table_path=toml_table_path,
            )
            last_start[key] = idx
        primary = names[0]
        seq_scope = _yaml_scalar_sequence_item_scope(raw_line)
        if seq_scope is not None:
            scope_stack.append((indent, seq_scope[0], seq_scope[1]))
        elif _opens_nested_binding_scope(raw_line):
            scope_stack.append((indent, primary, None))
    return last_start


def _last_binding_spans(text: str) -> dict[str, tuple[str, ...]]:
    """Map each scoped binding key to the span of its last occurrence in ``text``.

    Keys are qualified by enclosing def/class/function scopes (``A.ok``), by
    YAML/mapping openers with no same-line scalar (``feature.enabled``), by
    scalar YAML sequence-item identity (``features.name.a.enabled``;
    PRRT_kwDOSJAM6s6ZqxYE), and by the current TOML ``[table]`` / ``[[array]]``
    path so same-named leaves under different parents do not collide
    (PRRT_kwDOSJAM6s6ZqKN3, PRRT_kwDOSJAM6s6ZqZo2, PRRT_kwDOSJAM6s6ZqpBC). Lines
    that start inside ``/*`` or a triple-quoted string are ignored so docstring
    prose does not invent bindings (PRRT_kwDOSJAM6s6ZqPO9).
    """
    lines = text.splitlines()
    return {
        key: _binding_span_at(lines, start)
        for key, start in _last_binding_start_indices(text).items()
    }


def _tip_extra_line_indices(*, commit_blob: str, head_blob: str) -> set[int]:
    """Return head line indices that are tip-only vs the salvage commit blob.

    Full-line multiset counting keeps same-signature declaration redefinitions
    tip-extra (PRRT_kwDOSJAM6s6ZqDij) and also preserves surplus assignment
    occurrences when an earlier identical copy remains in the salvage blob
    (PRRT_kwDOSJAM6s6ZrFdv). Callers that intersect tip-extra binding keys must
    still require last-binding span inequality so surplus identical assignment
    copies do not look like supersession (PRRT_kwDOSJAM6s6ZqGeU).
    """
    remaining = Counter(commit_blob.splitlines())
    extra: set[int] = set()
    for idx, line in enumerate(head_blob.splitlines()):
        if remaining[line] > 0:
            remaining[line] -= 1
        else:
            extra.add(idx)
    return extra


def _subscript_binding_receiver(name: str) -> str | None:
    """Return the receiver prefix before the first ``[`` in a subscript key."""
    bracket = name.find("[")
    if bracket <= 0:
        return None
    return name[:bracket]


def _binding_key_has_nonliteral_subscript(name: str) -> bool:
    """Return True when ``name`` contains a bare-ident subscript index."""
    return re.search(r"\[[A-Za-z_][A-Za-z0-9_]*\]", name) is not None


def _nonliteral_subscript_shares_receiver(
    *, tip_extra_keys: set[str], candidate_keys: set[str]
) -> bool:
    """Return True when tip-extra ``FLAGS[key]`` shares a salvaged receiver.

    Exact-key intersection cannot relate ``FLAGS[key]`` to salvaged
    ``FLAGS["enabled"]``; fail closed so a computed override cannot keep stale
    FIXED evidence after salvage (PRRT_kwDOSJAM6s6Zv4pe).
    """
    salvaged_receivers: set[str] = set()
    for key in candidate_keys:
        recv = _subscript_binding_receiver(key)
        if recv is not None:
            salvaged_receivers.add(recv)
        elif "[" not in key and key:
            salvaged_receivers.add(key)
    if not salvaged_receivers:
        return False
    for key in tip_extra_keys:
        if not _binding_key_has_nonliteral_subscript(key):
            continue
        recv = _subscript_binding_receiver(key)
        if recv is not None and recv in salvaged_receivers:
            return True
    return False


def _tip_extra_keys_supersede_baseline(
    *, baseline_blob: str, head_blob: str, candidate_keys: set[str]
) -> bool:
    """Return True when tip-extra lines rebind a candidate with a new last span.

    Multiset tip-extra indices alone treat surplus identical assignment copies
    as tip-only; requiring last-binding inequality keeps those retained
    (PRRT_kwDOSJAM6s6ZqGeU) while duplicate-occurrence overrides that change
    the effective final binding still supersede (PRRT_kwDOSJAM6s6ZrFdv).
    Exact-key intersection cannot relate ``FLAGS[key]`` to salvaged
    ``FLAGS["enabled"]``; tip-extra nonliteral subscripts that share a salvaged
    receiver fail closed even when a surplus identical candidate binding made
    exact-key overlap non-empty (PRRT_kwDOSJAM6s6Zv4pe,
    PRRT_kwDOSJAM6s6ZwnzM).
    """
    if not candidate_keys:
        return False
    extra_indices = _tip_extra_line_indices(commit_blob=baseline_blob, head_blob=head_blob)
    if not extra_indices:
        return False
    tip_extra_keys = _scoped_binding_keys_on_lines(text=head_blob, line_indices=extra_indices)
    overlapping = candidate_keys & tip_extra_keys
    if overlapping:
        baseline_spans = _last_binding_spans(baseline_blob)
        head_spans = _last_binding_spans(head_blob)
        if any(baseline_spans.get(key) != head_spans.get(key) for key in overlapping):
            return True
    # Computed index (``FLAGS[key]``) never equals literal salvage keys; reject
    # when the tip-extra subscript shares a salvaged receiver. Run after the
    # exact-key path so surplus identical candidate bindings cannot skip this
    # check (PRRT_kwDOSJAM6s6Zv4pe, PRRT_kwDOSJAM6s6ZwnzM).
    return _nonliteral_subscript_shares_receiver(
        tip_extra_keys=tip_extra_keys,
        candidate_keys=candidate_keys,
    )


def _call_site_name_counts(text: str) -> Counter[str]:
    """Count executable call names in non-comment lines (including nested calls).

    Same-line repeated callees each increment (PRRT_kwDOSJAM6s6ZriaK).
    """
    counts: Counter[str] = Counter()
    in_block_comment = False
    in_triple_double = False
    in_triple_single = False
    for raw_line in text.splitlines():
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
        for name in _call_site_names_for_line(raw_line):
            counts[name] += 1
    return counts


def _salvage_changed_call_names(*, parent_blob: str, commit_blob: str) -> set[str]:
    """Return call names whose occurrence counts differ between parent and salvage.

    Call-only salvage flips (``disable_guard()`` → ``enable_guard()``, or
    ``guard.disable()`` → ``guard.enable()``) leave
    ``_salvage_changed_binding_names`` empty; tip-extra calls that restore the
    prior callee must still supersede (PRRT_kwDOSJAM6s6ZrN5J,
    PRRT_kwDOSJAM6s6ZrSYE). Counts preserve same-line multiplicity so
    ``disable_guard(); disable_guard()`` → ``disable_guard(); enable_guard()``
    treats ``disable_guard`` as changed (PRRT_kwDOSJAM6s6ZriaK).
    """
    parent_counts = _call_site_name_counts(parent_blob)
    commit_counts = _call_site_name_counts(commit_blob)
    return {
        name
        for name in parent_counts.keys() | commit_counts.keys()
        if parent_counts.get(name, 0) != commit_counts.get(name, 0)
    }


def _tip_extra_calls_candidate_keys(
    *,
    baseline_blob: str,
    head_blob: str,
    candidate_keys: set[str],
    receiver_prefix_keys: set[str] | frozenset[str] = frozenset(),
) -> bool:
    """Return True when tip-extra lines call a candidate-bound name.

    Binding-key intersection misses executable overrides such as appending
    ``disable_guard()`` after a salvage that defined and invoked
    ``enable_guard()``, or ``guard.disable()`` after ``guard.enable()``
    (PRRT_kwDOSJAM6s6ZrJ3a, PRRT_kwDOSJAM6s6ZrSYE). Multiline member calls
    (``guard`` then ``.disable()`` on the next line) join continuation lines
    before scanning so the receiver is preserved; unclassified continuations
    fail closed (PRRT_kwDOSJAM6s6ZuG-J, PRRT_kwDOSJAM6s6ZuQ6c). Lines that start inside ``/*`` or a
    triple-quoted string are ignored, matching binding scanners.
    ``receiver_prefix_keys`` enables ``name.*`` matching for call-count
    receivers only (PRRT_kwDOSJAM6s6ZroRa; not binding keys —
    PRRT_kwDOSJAM6s6ZrsE0).
    """
    if not candidate_keys:
        return False
    extra_indices = _tip_extra_line_indices(commit_blob=baseline_blob, head_blob=head_blob)
    if not extra_indices:
        return False
    lines = head_blob.splitlines()
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
        if line_in_non_code or idx not in extra_indices or raw_line.strip() == "":
            continue
        scan_line = raw_line
        stripped = raw_line.lstrip(" \t")
        is_continuation = _is_member_call_continuation(stripped)
        if is_continuation:
            scan_line = _join_member_call_continuation_line(lines, idx)
        names = _call_site_names_for_line(scan_line)
        for name in names:
            if _candidate_keys_include_call_name(
                candidate_keys,
                name,
                receiver_prefix_keys=receiver_prefix_keys,
            ):
                return True
        # Unclassified continuations fail closed (PRRT_kwDOSJAM6s6ZuG-J): no
        # call names after join, or a ``.`` / ``?.`` continuation that never
        # acquired a dotted receiver chain (bare ``.meth()`` leaf). Computed
        # joins may emit a bare receiver without ``.`` in names — those are
        # classified and must not supersede unrelated tips (PRRT_kwDOSJAM6s6ZuQ6c).
        if is_continuation and (
            not names
            or (stripped.startswith((".", "?.")) and not any("." in name for name in names))
        ):
            return True
    return False


def _scoped_binding_keys_on_lines(*, text: str, line_indices: set[int]) -> set[str]:
    """Return scoped binding keys whose opener lines fall in ``line_indices``.

    Lines that start inside ``/*`` or a triple-quoted string are ignored so
    tip-extra docstring prose cannot look like a rebind (PRRT_kwDOSJAM6s6ZqPO9).
    Tip-extra argument lines of a shared ``Object.assign`` / ``defineProperty`` /
    ``defineProperties`` / ``Reflect.set`` opener look back so synthesizable
    ``target.key`` bindings still count (PRRT_kwDOSJAM6s6Zy5DN).
    """
    if not line_indices:
        return set()
    lines = text.splitlines()
    keys: set[str] = set()
    scope_stack: list[tuple[int, str, str | None]] = []
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
        if idx in line_indices:
            scan_line = _join_incomplete_object_mutation_line_covering(lines, idx)
        else:
            scan_line = _join_incomplete_object_mutation_line(lines, idx)
        names = _binding_names_for_line(scan_line)
        if not names:
            continue
        for name in names:
            key = _scoped_binding_key_for_line(
                scope_stack,
                binding_name=name,
                raw_line=scan_line,
                toml_table_path=toml_table_path,
            )
            if idx in line_indices:
                keys.add(key)
        primary = names[0]
        seq_scope = _yaml_scalar_sequence_item_scope(raw_line)
        if seq_scope is not None:
            scope_stack.append((indent, seq_scope[0], seq_scope[1]))
        elif _opens_nested_binding_scope(raw_line):
            scope_stack.append((indent, primary, None))
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


def _is_arrow_function_binding_opener(opener: str) -> bool:
    """Return True when a binding opener's RHS uses an arrow function.

    Callers pass binding-start openers from ``_last_binding_start_indices``.
    ``=>`` distinguishes arrow RHS forms (``const apply = () => {``,
    ``helper = () => {``) from ordinary leaf assigns so tip-extra control-flow
    inside those bodies is not skipped (PRRT_kwDOSJAM6s6ZyaxJ).
    Class headers can contain ``=>`` only in type positions
    (``implements Handler<() => void>``); those must stay on the class path so
    nested-callable exclusion still applies (PRRT_kwDOSJAM6s6Zylhi).
    """
    if _CLASS_BINDING_RE.match(opener) is not None:
        return False
    return "=>" in opener


def _unchanged_nested_callable_body_indices(
    *,
    lines: list[str],
    starts: dict[str, int],
    class_start: int,
    class_end: int,
    changed_keys: set[str],
) -> set[int]:
    """Return body indices of nested callables salvage did not edit.

    Used so class-level control-flow scanning can fail closed on unrecognized
    method regions (JS shorthand) without dropping leaf-assignment salvage when
    an unrelated nested ``def``/``function``/arrow gains tip-extra ``return``
    (PRRT_kwDOSJAM6s6Zvk1G; compare leaf retention in PRRT_kwDOSJAM6s6ZvVZK;
    arrow fields PRRT_kwDOSJAM6s6ZyaxJ).
    """
    excluded: set[int] = set()
    for key, start in starts.items():
        if key in changed_keys:
            continue
        if not (class_start < start < class_end):
            continue
        if start >= len(lines):
            continue
        opener = lines[start]
        if (
            _DEF_BINDING_RE.match(opener) is None
            and _FUNCTION_BINDING_RE.match(opener) is None
            and not _is_arrow_function_binding_opener(opener)
        ):
            continue
        nested_end = _binding_span_end_exclusive(lines, start)
        excluded.update(range(start + 1, nested_end))
    return excluded


def _callable_body_line_indices(
    *,
    lines: list[str],
    starts: dict[str, int],
    key: str,
    changed_keys: set[str],
) -> set[int] | None:
    """Return body indices for a def/function/arrow/class key, or None if not callable.

    Class bodies exclude nested ``def``/``function``/arrow spans whose keys are
    not in ``changed_keys`` (PRRT_kwDOSJAM6s6Zvk1G, PRRT_kwDOSJAM6s6ZyaxJ).
    """
    start = starts.get(key)
    if start is None or start >= len(lines):
        return None
    opener = lines[start]
    end = _binding_span_end_exclusive(lines, start)
    if (
        _DEF_BINDING_RE.match(opener) is not None
        or _FUNCTION_BINDING_RE.match(opener) is not None
        or _is_arrow_function_binding_opener(opener)
    ):
        return set(range(start + 1, end))
    if _CLASS_BINDING_RE.match(opener) is None:
        return None
    class_body = set(range(start + 1, end))
    class_body -= _unchanged_nested_callable_body_indices(
        lines=lines,
        starts=starts,
        class_start=start,
        class_end=end,
        changed_keys=changed_keys,
    )
    return class_body


def _tip_extra_indices_within_body(
    *,
    commit_lines: list[str],
    commit_body: set[int] | None,
    head_lines: list[str],
    head_body: set[int],
) -> set[int]:
    """Tip-extra head body indices vs the same callable's commit body multiset.

    Scoped to one callable so an identical control-flow line elsewhere in the
    salvage blob cannot greedily consume tip-extra budget for a new wrapper
    inside the changed callable (PRRT_kwDOSJAM6s6Zvll3).
    """
    remaining = Counter(commit_lines[i] for i in sorted(commit_body or ()) if i < len(commit_lines))
    extra: set[int] = set()
    for idx in sorted(head_body):
        if idx >= len(head_lines):
            continue
        line = head_lines[idx]
        if remaining[line] > 0:
            remaining[line] -= 1
        else:
            extra.add(idx)
    return extra


def _tip_extra_control_flow_in_changed_callables(
    *, commit_blob: str, head_blob: str, changed_keys: set[str]
) -> bool:
    """Return True when tip-extra control-flow sits in a salvage-changed callable.

    Early ``return`` / ``raise`` / ``throw`` / ordinary ``if`` headers inside a
    ``def``/``function``/arrow whose body the salvage edited can leave the
    salvaged fix unreachable while ``git merge-file`` still equals HEAD. Binding
    and call scanners miss those tip-extra lines (PRRT_kwDOSJAM6s6ZvVZK,
    PRRT_kwDOSJAM6s6ZyaxJ). Changed ``class`` spans also count, minus nested
    ``def``/``function``/arrow bodies whose keys are unchanged, so JS
    method-shorthand edits fail closed without dropping leaf-assignment salvage
    when an unrelated helper gains ``return`` (PRRT_kwDOSJAM6s6Zvk1G). Tip-extra
    is computed per callable body so an identical wrapper line elsewhere in the
    salvage commit cannot steal multiset budget from a new disabling header in
    the changed callable (PRRT_kwDOSJAM6s6Zvll3).
    """
    if not changed_keys:
        return False
    head_lines = head_blob.splitlines()
    commit_lines = commit_blob.splitlines()
    head_starts = _last_binding_start_indices(head_blob)
    commit_starts = _last_binding_start_indices(commit_blob)
    tip_extra_body: set[int] = set()
    for key in changed_keys:
        head_body = _callable_body_line_indices(
            lines=head_lines,
            starts=head_starts,
            key=key,
            changed_keys=changed_keys,
        )
        if not head_body:
            continue
        commit_body = _callable_body_line_indices(
            lines=commit_lines,
            starts=commit_starts,
            key=key,
            changed_keys=changed_keys,
        )
        tip_extra_body |= _tip_extra_indices_within_body(
            commit_lines=commit_lines,
            commit_body=commit_body,
            head_lines=head_lines,
            head_body=head_body,
        )
    if not tip_extra_body:
        return False
    in_block_comment = False
    in_triple_double = False
    in_triple_single = False
    for idx, raw_line in enumerate(head_lines):
        line_in_non_code = in_block_comment or in_triple_double or in_triple_single
        in_block_comment, in_triple_double, in_triple_single = (
            _advance_string_or_block_comment_state(
                raw_line + "\n",
                in_block_comment=in_block_comment,
                in_triple_double=in_triple_double,
                in_triple_single=in_triple_single,
            )
        )
        if line_in_non_code or idx not in tip_extra_body:
            continue
        if _line_is_control_flow_change(raw_line):
            return True
    return False


def _tip_extra_can_supersede_modified_salvage(
    *, parent_blob: str, commit_blob: str, head_blob: str
) -> bool:
    """Return True when tip-only lines rebind a name the salvage changed vs parent.

    Baseline-backed retention uses clean ``git merge-file`` equality with HEAD.
    A tip can keep the salvage hunk and append a later rebinding of the same
    name (``FEATURE_ENABLED = True`` then ``FEATURE_ENABLED = False``, or shell
    ``export`` / ``declare -x`` / ``typeset`` / ``readonly`` forms of the same
    name); with surrounding context merge-file reproduces that tip cleanly, so
    equality alone would retain stale FIXED evidence. Only scoped keys whose last
    binding span (opener plus indented body) changed vs parent count — unrelated
    appends and later hunks stay retained (PRRT_kwDOSJAM6s6Zp_3j,
    PRRT_kwDOSJAM6s6ZqseO, PRRT_kwDOSJAM6s6ZqxX4, PRRT_kwDOSJAM6s6ZrBJF). Tip-extra
    lines use full-line multiset counting so same-signature redefinitions and
    duplicate assignment occurrences stay tip-extra (PRRT_kwDOSJAM6s6ZqDij,
    PRRT_kwDOSJAM6s6ZrFdv); last-binding span inequality then filters surplus
    identical salvage assignment copies (PRRT_kwDOSJAM6s6ZqGeU). Body-only
    declaration edits still count as changed bindings (PRRT_kwDOSJAM6s6ZqHvh).
    Parent-only (deleted) salvage names also count so tip reintroduction
    supersedes (PRRT_kwDOSJAM6s6ZqKGY). Tip-extra binding keys are resolved
    against the full tip blob so an unrelated later ``def ok`` under another
    class does not collide with salvaged ``A.ok`` (PRRT_kwDOSJAM6s6ZqKN3),
    nested YAML ``logging.enabled`` does not collide with salvaged
    ``feature.enabled`` (PRRT_kwDOSJAM6s6ZqZo2), and TOML ``[logging] enabled``
    does not collide with salvaged ``[feature] enabled`` (PRRT_kwDOSJAM6s6ZqpBC).
    Call-only salvage flips (``disable_guard()`` → ``enable_guard()``, or
    ``guard.disable()`` → ``guard.enable()``) leave
    binding diffs empty; tip-extra calls that restore a prior callee or invoke a
    salvage-changed binding name supersede (PRRT_kwDOSJAM6s6ZrN5J,
    PRRT_kwDOSJAM6s6ZrSYE; compare PRRT_kwDOSJAM6s6ZrJ3a). Call candidates are
    only changed bindings ∪ changed call names — not every commit binding — so a
    tip-extra ``helper()`` on an unchanged helper does not drop a still-present
    binding fix (PRRT_kwDOSJAM6s6ZrR2e).     Tip-extra control-flow inside a
    salvage-modified ``def``/``function``/arrow body (early ``return`` /
    ``if (false)``) also supersedes when it would leave the salvaged fix
    unreachable while merge-file still equals HEAD (PRRT_kwDOSJAM6s6ZvVZK,
    PRRT_kwDOSJAM6s6ZyaxJ), including JS class method shorthand under a changed
    ``class`` span (PRRT_kwDOSJAM6s6Zvk1G); per-callable body tip-extra avoids
    identical elsewhere wrappers stealing file-level multiset budget
    (PRRT_kwDOSJAM6s6Zvll3).     Collection mutation helpers (``FLAGS.__setitem__("enabled", False)`` /
    ``FLAGS.update(…)`` / ``FLAGS.clear()``) synthesize subscript keys or fail
    closed when an opaque mutator shares a salvaged subscript receiver
    (PRRT_kwDOSJAM6s6ZwrnH). ``Object.assign(guard, {enabled: false})``
    synthesizes ``guard.enabled``; opaque ``Object.assign(guard, other)`` fails
    closed on a shared salvaged receiver (PRRT_kwDOSJAM6s6Zxwhs).
    ``Object.defineProperty(guard, "enabled", …)`` synthesizes ``guard.enabled``;
    opaque ``Object.defineProperty(guard, key, …)`` fails closed the same way
    (PRRT_kwDOSJAM6s6Zy4pR). ``Object.defineProperties(guard, {enabled: {…}})``
    synthesizes ``guard.enabled``; opaque ``Object.defineProperties(guard, props)``
    fails closed the same way (PRRT_kwDOSJAM6s6ZzifG). ``Reflect.set(guard, "enabled", …)``
    synthesizes ``guard.enabled``; opaque ``Reflect.set(guard, key, …)`` fails closed
    the same way (PRRT_kwDOSJAM6s6ZzN-l). Tip-extra alias assignments of a salvage-changed
    name fail closed (PRRT_kwDOSJAM6s6ZxHGP).
    """
    changed = _salvage_changed_binding_names(parent_blob=parent_blob, commit_blob=commit_blob)
    if _tip_extra_keys_supersede_baseline(
        baseline_blob=commit_blob,
        head_blob=head_blob,
        candidate_keys=changed,
    ):
        return True
    call_names = _salvage_changed_call_names(parent_blob=parent_blob, commit_blob=commit_blob)
    call_candidates = changed | call_names
    if _tip_extra_calls_candidate_keys(
        baseline_blob=commit_blob,
        head_blob=head_blob,
        candidate_keys=call_candidates,
        # Prefix match only call-count keys (``guard.disable``), never scoped
        # binding keys (``feature.enabled``) — PRRT_kwDOSJAM6s6ZrsE0.
        receiver_prefix_keys=call_names,
    ):
        return True
    if _tip_extra_opaque_collection_mutator_shares_receiver(
        baseline_blob=commit_blob,
        head_blob=head_blob,
        candidate_keys=changed,
    ):
        return True
    if _tip_extra_opaque_object_assign_shares_receiver(
        baseline_blob=commit_blob,
        head_blob=head_blob,
        candidate_keys=changed,
    ):
        return True
    if _tip_extra_opaque_object_define_property_shares_receiver(
        baseline_blob=commit_blob,
        head_blob=head_blob,
        candidate_keys=changed,
    ):
        return True
    if _tip_extra_opaque_object_define_properties_shares_receiver(
        baseline_blob=commit_blob,
        head_blob=head_blob,
        candidate_keys=changed,
    ):
        return True
    if _tip_extra_opaque_reflect_set_shares_receiver(
        baseline_blob=commit_blob,
        head_blob=head_blob,
        candidate_keys=changed,
    ):
        return True
    if _tip_extra_aliases_salvaged_candidate(
        baseline_blob=commit_blob,
        head_blob=head_blob,
        candidate_keys=changed,
    ):
        return True
    return _tip_extra_control_flow_in_changed_callables(
        commit_blob=commit_blob,
        head_blob=head_blob,
        changed_keys=changed,
    )


def _suffix_can_supersede_added_salvage(*, salvage: str, head_blob: str) -> bool:
    """Return True when tip-appended content rebinds or calls a name from ``salvage``.

    Uses the same parent-qualified keys as the baseline-backed path so nested
    YAML/TOML/class leaves under different parents do not collide as bare
    ``enabled`` / ``ok`` (PRRT_kwDOSJAM6s6Zq76q; compare PRRT_kwDOSJAM6s6ZqZo2,
    PRRT_kwDOSJAM6s6ZqpBC, PRRT_kwDOSJAM6s6ZqKN3). Tip-extra keys resolve against
    the full tip blob so a same-parent nested rebind under the salvage prefix
    still qualifies. Flat leaf intersection previously discarded retained FIXED
    evidence when an unrelated descendant append shared only the leaf name.
    Tip-extra call sites that invoke a salvage-bound name also supersede:
    ``disable_guard()`` after a salvage that defined and called ``enable_guard()``,
    or ``guard.disable()`` after ``guard.enable()``, produces no binding key, so
    assignment-only matching would retain stale FIXED evidence
    (PRRT_kwDOSJAM6s6ZrJ3a, PRRT_kwDOSJAM6s6ZrSYE).
    Import-only receivers (``from guards import guard`` / ``import guards``) are
    not bindings; union salvage call-site names into the call-candidate set so
    tip ``guard.disable()`` still supersedes (PRRT_kwDOSJAM6s6ZryCh).
    Indirect attribute mutations (``setattr(guard, "enabled", False)`` /
    ``delattr`` / ``object.__setattr__`` / ``guard.__setattr__``) synthesize
    ``guard.enabled`` binding keys so they supersede like a rebind
    (PRRT_kwDOSJAM6s6Zu8Kn). Collection mutation helpers
    (``FLAGS.__setitem__`` / ``FLAGS.update`` / ``FLAGS.clear``) synthesize
    ``FLAGS["key"]`` or fail closed on opaque mutators sharing a salvaged
    subscript receiver (PRRT_kwDOSJAM6s6ZwrnH). ``Object.assign`` object-literal
    keys synthesize ``target.key``; opaque sources fail closed on a shared
    salvaged receiver (PRRT_kwDOSJAM6s6Zxwhs).     ``Object.defineProperty`` string
    property names synthesize ``target.key``; opaque property names fail closed
    on a shared salvaged receiver (PRRT_kwDOSJAM6s6Zy4pR). ``Object.defineProperties``
    object-literal property keys synthesize ``target.key``; opaque descriptor maps
    fail closed on a shared salvaged receiver (PRRT_kwDOSJAM6s6ZzifG). ``Reflect.set``
    string property names synthesize ``target.key``; opaque property names fail closed
    on a shared salvaged receiver (PRRT_kwDOSJAM6s6ZzN-l). Tip-extra alias
    assignments (``const alias = guard``) fail closed so ``alias.enabled = false``
    cannot keep stale salvage (PRRT_kwDOSJAM6s6ZxHGP).
    """
    if len(head_blob) <= len(salvage):
        return False
    salvage_keys = set(_last_binding_spans(salvage))
    if _tip_extra_keys_supersede_baseline(
        baseline_blob=salvage,
        head_blob=head_blob,
        candidate_keys=salvage_keys,
    ):
        return True
    # Added salvage has no parent diff: treat every salvage call name as a
    # candidate (mirrors ``_salvage_changed_call_names`` on the modified path).
    call_names = set(_call_site_name_counts(salvage))
    call_candidates = salvage_keys | call_names
    if _tip_extra_calls_candidate_keys(
        baseline_blob=salvage,
        head_blob=head_blob,
        candidate_keys=call_candidates,
        receiver_prefix_keys=call_names,
    ):
        return True
    if _tip_extra_opaque_collection_mutator_shares_receiver(
        baseline_blob=salvage,
        head_blob=head_blob,
        candidate_keys=salvage_keys,
    ):
        return True
    if _tip_extra_opaque_object_assign_shares_receiver(
        baseline_blob=salvage,
        head_blob=head_blob,
        candidate_keys=salvage_keys,
    ):
        return True
    if _tip_extra_opaque_object_define_property_shares_receiver(
        baseline_blob=salvage,
        head_blob=head_blob,
        candidate_keys=salvage_keys,
    ):
        return True
    if _tip_extra_opaque_object_define_properties_shares_receiver(
        baseline_blob=salvage,
        head_blob=head_blob,
        candidate_keys=salvage_keys,
    ):
        return True
    if _tip_extra_opaque_reflect_set_shares_receiver(
        baseline_blob=salvage,
        head_blob=head_blob,
        candidate_keys=salvage_keys,
    ):
        return True
    return _tip_extra_aliases_salvaged_candidate(
        baseline_blob=salvage,
        head_blob=head_blob,
        candidate_keys=salvage_keys,
    )


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
    comment, triple-quoted string, or ``#if`` region (PRRT_kwDOSJAM6s6ZpaIn),
    while ordinary quoted strings and ``//`` line comments stay opaque so a
    quoted ``/*`` token cannot falsely reject a valid salvage
    (PRRT_kwDOSJAM6s6Zq2m_). It also rejects ordinary control-flow prefixes
    (``if (false)``, open ``{``, Python suite headers) that keep the salvage as
    an exact suffix while preventing execution (PRRT_kwDOSJAM6s6ZtJG5), and
    Python decorator prefixes (``@no_op``) that attach to a salvaged ``def`` /
    ``class`` and can replace it (PRRT_kwDOSJAM6s6ZwrnM).
    Prefix retention with a non-empty append additionally rejects when the
    appended tip-extra lines rebind a **scoped** name bound in the salvage
    (PRRT_kwDOSJAM6s6Zp8jM, PRRT_kwDOSJAM6s6Zq76q) or call a salvage-bound
    name (``disable_guard()`` after ``enable_guard()``, or ``guard.disable()``
    after ``guard.enable()``; PRRT_kwDOSJAM6s6ZrJ3a, PRRT_kwDOSJAM6s6ZrSYE).

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
            head_blob=head_blob,
        )
    # Prepend: salvage remains a line-aligned suffix (not already covered above),
    # and the prepended prefix must not leave an open disabling context or
    # attach ordinary control-flow to the salvage statement
    # (PRRT_kwDOSJAM6s6ZtJG5).
    suffix_idx = len(head_blob) - len(commit_blob)
    if suffix_idx <= 0 or not _line_aligned_at(suffix_idx):
        return False
    prefix = head_blob[:suffix_idx]
    return not (
        _prefix_leaves_open_disabling_context(prefix)
        or _prefix_opens_control_flow_over_suffix(prefix)
    )


# Tip-extra opaque / alias supersession helpers (line-limit decomposition).
# Imported after early helpers above so circular imports resolve.
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_head import (  # noqa: E402, F401
    _commit_changes_present_in_head as _commit_changes_present_in_head,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_opaque import (  # noqa: E402, F401
    _alias_assign_gap_line_is_skippable as _alias_assign_gap_line_is_skippable,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_opaque import (  # noqa: E402, F401
    _join_incomplete_alias_assign_line as _join_incomplete_alias_assign_line,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_opaque import (  # noqa: E402
    _tip_extra_aliases_salvaged_candidate as _tip_extra_aliases_salvaged_candidate,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_opaque import (  # noqa: E402
    _tip_extra_opaque_collection_mutator_shares_receiver as _tip_extra_opaque_collection_mutator_shares_receiver,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_opaque import (  # noqa: E402
    _tip_extra_opaque_object_assign_shares_receiver as _tip_extra_opaque_object_assign_shares_receiver,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_opaque import (  # noqa: E402
    _tip_extra_opaque_object_define_properties_shares_receiver as _tip_extra_opaque_object_define_properties_shares_receiver,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_opaque import (  # noqa: E402
    _tip_extra_opaque_object_define_property_shares_receiver as _tip_extra_opaque_object_define_property_shares_receiver,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_opaque import (  # noqa: E402
    _tip_extra_opaque_reflect_set_shares_receiver as _tip_extra_opaque_reflect_set_shares_receiver,
)
