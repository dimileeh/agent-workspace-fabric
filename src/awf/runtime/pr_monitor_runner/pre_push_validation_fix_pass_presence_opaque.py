"""Tip-extra opaque mutation / alias supersession helpers for salvage presence."""

from __future__ import annotations

import re

# Helpers defined earlier in presence.py; imported after those defs exist via
# deferred import from presence (see bottom of the early section there).
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence import (
    _advance_string_or_block_comment_state as _advance_string_or_block_comment_state,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence import (
    _subscript_binding_receiver as _subscript_binding_receiver,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence import (
    _tip_extra_line_indices as _tip_extra_line_indices,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_assigns import (
    _update_mutation_args_fully_synthesizable as _update_mutation_args_fully_synthesizable,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_assigns import (
    _update_mutation_binding_names as _update_mutation_binding_names,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_calls import (
    _call_site_names_for_line as _call_site_names_for_line,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_calls import (
    _executable_call_scan_text as _executable_call_scan_text,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_calls import (
    _is_member_call_continuation as _is_member_call_continuation,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_calls import (
    _join_member_call_continuation_line as _join_member_call_continuation_line,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_context import (
    _line_code_without_line_comment as _line_code_without_line_comment,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_object_mut import (
    _join_incomplete_object_mutation_line_covering as _join_incomplete_object_mutation_line_covering,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_object_mut import (
    _object_assign_call_targets as _object_assign_call_targets,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_object_mut import (
    _object_assign_call_unclosed as _object_assign_call_unclosed,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_object_mut import (
    _object_define_properties_call_targets as _object_define_properties_call_targets,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_object_mut import (
    _object_define_properties_call_unclosed as _object_define_properties_call_unclosed,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_object_mut import (
    _object_define_property_call_targets as _object_define_property_call_targets,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_object_mut import (
    _object_define_property_call_unclosed as _object_define_property_call_unclosed,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_object_mut import (
    _reflect_set_call_targets as _reflect_set_call_targets,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_object_mut import (
    _reflect_set_call_unclosed as _reflect_set_call_unclosed,
)

# Tip-extra mapping mutators whose arguments may be opaque (``update(other)``,
# ``clear()``, ``popitem()``). Call names do not match ``FLAGS["enabled"]``;
# fail closed when the receiver equals a salvaged subscript receiver
# (PRRT_kwDOSJAM6s6ZwrnH). Literal-key ``__setitem__`` / kwargs ``update`` /
# dict-literal ``update`` are handled by binding synthesis instead so
# ``FLAGS.__setitem__("other", …)`` / ``FLAGS.update(other=False)`` do not
# drop unrelated salvage (PRRT_kwDOSJAM6s6ZxeRb); only non-synthesizable
# ``update`` forms stay opaque, including mixed opaque+kwargs
# (``FLAGS.update(other_flags, other=False)``; PRRT_kwDOSJAM6s6Zxt0p).
_OPAQUE_COLLECTION_MUTATOR_METHODS = frozenset({"update", "clear", "popitem"})

# Tip-extra ``const alias = guard`` / ``alias = guard`` where ``guard`` is a
# salvaged candidate (or receiver of ``guard.enabled`` / ``FLAGS["enabled"]``).
# Exact-key intersection only sees ``alias`` / ``alias.enabled``, so fail closed
# on the aliasing assignment itself (PRRT_kwDOSJAM6s6ZxHGP).
_TIP_EXTRA_ALIAS_ASSIGN_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_]))"
    r"(?:(?:const|let|var)[ \t]+)?"
    r"[A-Za-z_][A-Za-z0-9_]*"
    r"[ \t]*=[ \t]*"
    r"([A-Za-z_][A-Za-z0-9_]*)"
    r"(?![A-Za-z0-9_.\[\(])"
    # JS ``const fn = FEATURE_ENABLED =>`` is an arrow param, not an alias RHS
    # (PRRT_kwDOSJAM6s6ZtZ_2 vs alias retain PRRT_kwDOSJAM6s6ZxHGP).
    r"(?![ \t]*=>)"
)

# Assignment whose RHS was split to a following line (``const alias =`` alone).
# Same-line ``_TIP_EXTRA_ALIAS_ASSIGN_RE`` cannot see the salvaged receiver
# (PRRT_kwDOSJAM6s6ZxhFW).
_INCOMPLETE_ALIAS_ASSIGN_LHS_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_]))"
    r"(?:(?:const|let|var)[ \t]+)?"
    r"[A-Za-z_][A-Za-z0-9_]*"
    r"[ \t]*=[ \t]*$"
)


def _alias_assign_rhs_incomplete(raw_line: str) -> bool:
    """Return True when ``raw_line`` is an assign whose RHS is still pending.

    Uses comment-stripped text (strings kept) so ``msg = "…"`` is not treated
    as incomplete after string blanking would leave a trailing ``=``.
    """
    code = _line_code_without_line_comment(raw_line).rstrip()
    return bool(_INCOMPLETE_ALIAS_ASSIGN_LHS_RE.search(code))


def _alias_assign_gap_line_is_skippable(stripped: str) -> bool:
    """Return True when ``stripped`` is only a blank / comment gap before an RHS.

    Whole-line ``/* … */`` must be skipped like ``//`` / ``#``; otherwise look-ahead
    joins the comment as the RHS and tip-extra alias matching misses the salvaged
    receiver (PRRT_kwDOSJAM6s6Zxt0u).
    """
    if stripped == "" or stripped.startswith("//") or stripped.startswith("#"):
        return True
    if stripped.startswith("/*") and "*/" in stripped:
        after = stripped.split("*/", 1)[1].strip()
        return after == ""
    return False


def _join_incomplete_alias_assign_line(lines: list[str], idx: int) -> str:
    """Join ``lines[idx]`` with the next executable RHS when assign ends at ``=``.

    Formatters commonly split ``const alias = guard`` across lines. Per-line
    scanning then never matches ``_TIP_EXTRA_ALIAS_ASSIGN_RE``, so tip-extra
    ``alias.enabled = false`` after salvage would keep stale FIXED evidence
    (PRRT_kwDOSJAM6s6ZxhFW). Skip blank / line-comment / whole-line ``/* … */``
    gaps between ``=`` and the RHS (including multi-line block comments;
    PRRT_kwDOSJAM6s6Zxt0u); stop at the first non-skipped line.
    """
    raw_line = lines[idx]
    if not _alias_assign_rhs_incomplete(raw_line):
        return raw_line
    parts: list[str] = [raw_line.rstrip()]
    j = idx + 1
    while j < len(lines):
        nxt = lines[j]
        nxt_stripped = nxt.strip()
        if _alias_assign_gap_line_is_skippable(nxt_stripped):
            j += 1
            continue
        # Multi-line block comment opened on this gap line — skip through ``*/``.
        if nxt_stripped.startswith("/*") and "*/" not in nxt_stripped:
            j += 1
            while j < len(lines):
                close_line = lines[j]
                j += 1
                if "*/" not in close_line:
                    continue
                after = close_line.split("*/", 1)[1].strip()
                if after == "":
                    break
                parts.append(after)
                break
            else:
                break
            if len(parts) > 1:
                break
            continue
        parts.append(nxt.lstrip(" \t"))
        break
    if len(parts) == 1:
        return raw_line
    joined = parts[0]
    for part in parts[1:]:
        joined = joined.rstrip() + " " + part.lstrip(" \t")
    return joined


def _salvaged_alias_reference_names(candidate_keys: set[str]) -> set[str]:
    """Return names a tip may alias or mutate to reach salvaged bindings.

    Includes bare candidate keys, every dotted path prefix through the full key
    (``config`` and ``config.guard`` from ``config.guard.enabled`` — not only
    the root, so opaque ``Object.assign(config.guard, …)`` fails closed;
    PRRT_kwDOSJAM6s6ZyGqh), and subscript receivers (``FLAGS`` from
    ``FLAGS["enabled"]``) so ``const alias = guard`` fails closed
    (PRRT_kwDOSJAM6s6ZxHGP).
    """
    names: set[str] = set()
    for key in candidate_keys:
        if not key:
            continue
        recv = _subscript_binding_receiver(key)
        if recv is not None:
            names.add(recv)
            continue
        if "." in key:
            parts = key.split(".")
            for i in range(1, len(parts) + 1):
                names.add(".".join(parts[:i]))
            continue
        if "[" not in key:
            names.add(key)
    return names


def _tip_extra_aliases_salvaged_candidate(
    *, baseline_blob: str, head_blob: str, candidate_keys: set[str]
) -> bool:
    """Return True when tip-extra assigns an alias of a salvaged candidate name.

    After added-file salvage sets ``guard.enabled = true``, a descendant can
    append ``const alias = guard; alias.enabled = false;``. Tip scanners emit
    only ``alias`` / ``alias.enabled``, which never intersect salvaged ``guard``
    keys, and call checks find nothing — fail closed on the aliasing assignment
    so stale FIXED evidence cannot resolve (PRRT_kwDOSJAM6s6ZxHGP). Multiline
    ``const alias =\\n  guard`` is joined before matching so formatters cannot
    bypass the same check (PRRT_kwDOSJAM6s6ZxhFW); an assign whose RHS remains
    incomplete after look-ahead also fails closed.
    """
    refs = _salvaged_alias_reference_names(candidate_keys)
    if not refs:
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
        scan_line = _join_incomplete_alias_assign_line(lines, idx)
        # RHS still missing after continuation look-ahead — cannot prove the
        # tip does not alias a salvaged receiver; fail closed.
        if _alias_assign_rhs_incomplete(scan_line):
            return True
        scan = _executable_call_scan_text(scan_line)
        for match in _TIP_EXTRA_ALIAS_ASSIGN_RE.finditer(scan):
            if match.group(1) in refs:
                return True
    return False


def _salvaged_subscript_receivers(candidate_keys: set[str]) -> set[str]:
    """Return receivers of subscript (or bare) candidate binding keys."""
    receivers: set[str] = set()
    for key in candidate_keys:
        recv = _subscript_binding_receiver(key)
        if recv is not None:
            receivers.add(recv)
    return receivers


def _tip_extra_opaque_collection_mutator_shares_receiver(
    *, baseline_blob: str, head_blob: str, candidate_keys: set[str]
) -> bool:
    """Return True when tip-extra opaque mapping mutators share a salvage receiver.

    ``FLAGS.update(other_flags)`` / ``FLAGS.clear()`` emit call names that never
    equal ``FLAGS["enabled"]``; fail closed when the call receiver matches a
    salvaged subscript receiver (PRRT_kwDOSJAM6s6ZwrnH). Kwargs and dict-literal
    ``update`` forms synthesize subscript keys instead, so they are not opaque
    and unrelated keys keep salvage like ``__setitem__("other", …)``
    (PRRT_kwDOSJAM6s6ZxeRb). Mixed opaque+synthesizable forms
    (``FLAGS.update(other_flags, other=False)``) still fail closed
    (PRRT_kwDOSJAM6s6Zxt0p).
    """
    receivers = _salvaged_subscript_receivers(candidate_keys)
    if not receivers:
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
        if _is_member_call_continuation(stripped):
            scan_line = _join_member_call_continuation_line(lines, idx)
        for name in _call_site_names_for_line(scan_line):
            if "." not in name:
                continue
            method = name.rsplit(".", 1)[-1]
            if method not in _OPAQUE_COLLECTION_MUTATOR_METHODS:
                continue
            # Binding synthesis already understands kwargs / dict-literal
            # ``update``; do not fail closed and drop unrelated salvage.
            # Require fully synthesizable args so mixed opaque+kwargs forms
            # like ``FLAGS.update(other_flags, other=False)`` still fail closed
            # (PRRT_kwDOSJAM6s6Zxt0p).
            if (
                method == "update"
                and _update_mutation_binding_names(scan_line)
                and _update_mutation_args_fully_synthesizable(scan_line)
            ):
                continue
            receiver = name[: -(len(method) + 1)]
            if receiver in receivers:
                return True
    return False


def _tip_extra_opaque_object_assign_shares_receiver(
    *, baseline_blob: str, head_blob: str, candidate_keys: set[str]
) -> bool:
    """Return True when tip-extra opaque ``Object.assign`` shares a salvage receiver.

    ``Object.assign(guard, other)`` emits only ``Object`` / ``Object.assign`` call
    names, which never equal salvaged ``guard.enabled``. Fail closed when the
    target argument matches a salvaged receiver (dotted / subscript / bare) and
    sources are not fully synthesizable object literals
    (PRRT_kwDOSJAM6s6Zxwhs). Literal-key forms synthesize ``guard.enabled`` via
    binding names instead, so unrelated keys keep salvage like
    ``Object.assign(guard, {other: false})``. Multiline
    ``Object.assign(\\n  guard,\\n  …)`` joins continued argument lists before
    scanning, looking back from tip-extra argument lines when the shared opener
    is not tip-extra (PRRT_kwDOSJAM6s6Zy5DN); an opener that remains unclosed
    with no parseable target after look-ahead also fails closed
    (PRRT_kwDOSJAM6s6Zyo4_).
    """
    receivers = _salvaged_alias_reference_names(candidate_keys)
    if not receivers:
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
        scan_line = _join_incomplete_object_mutation_line_covering(lines, idx)
        targets = _object_assign_call_targets(scan_line)
        # Opener with no parseable target after join (still unclosed) — cannot
        # prove the tip does not mutate a salvaged receiver.
        if not targets and _object_assign_call_unclosed(scan_line):
            return True
        for target, fully_synthesizable in targets:
            if fully_synthesizable:
                continue
            if target in receivers:
                return True
    return False


def _tip_extra_opaque_object_define_property_shares_receiver(
    *, baseline_blob: str, head_blob: str, candidate_keys: set[str]
) -> bool:
    """Return True when tip-extra opaque ``defineProperty`` shares a salvage receiver.

    ``Object.defineProperty(guard, key, …)`` emits only ``Object`` /
    ``Object.defineProperty`` call names, which never equal salvaged
    ``guard.enabled``. Fail closed when the target argument matches a salvaged
    receiver and the property name is not a string literal
    (PRRT_kwDOSJAM6s6Zy4pR). Literal property forms synthesize ``guard.enabled``
    via binding names instead, so unrelated keys keep salvage like
    ``Object.defineProperty(guard, "other", …)``. Multiline
    ``Object.defineProperty(\\n  guard,\\n  …)`` joins continued argument lists
    before scanning, looking back from tip-extra argument lines when the shared
    opener is not tip-extra (PRRT_kwDOSJAM6s6Zy5DN); an opener that remains
    unclosed with no parseable target after look-ahead also fails closed.
    """
    receivers = _salvaged_alias_reference_names(candidate_keys)
    if not receivers:
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
        scan_line = _join_incomplete_object_mutation_line_covering(lines, idx)
        targets = _object_define_property_call_targets(scan_line)
        if not targets and _object_define_property_call_unclosed(scan_line):
            return True
        for target, fully_synthesizable in targets:
            if fully_synthesizable:
                continue
            if target in receivers:
                return True
    return False


def _tip_extra_opaque_object_define_properties_shares_receiver(
    *, baseline_blob: str, head_blob: str, candidate_keys: set[str]
) -> bool:
    """Return True when tip-extra opaque ``defineProperties`` shares a salvage receiver.

    ``Object.defineProperties(guard, props)`` emits only ``Object`` /
    ``Object.defineProperties`` call names, which never equal salvaged
    ``guard.enabled``. Fail closed when the target argument matches a salvaged
    receiver and the descriptors map is not a plain object literal
    (PRRT_kwDOSJAM6s6ZzifG). Literal property forms synthesize ``guard.enabled``
    via binding names instead, so unrelated keys keep salvage like
    ``Object.defineProperties(guard, {other: {…}})``. Multiline
    ``Object.defineProperties(\\n  guard,\\n  …)`` joins continued argument lists
    before scanning, looking back from tip-extra argument lines when the shared
    opener is not tip-extra; an opener that remains unclosed with no parseable
    target after look-ahead also fails closed.
    """
    receivers = _salvaged_alias_reference_names(candidate_keys)
    if not receivers:
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
        scan_line = _join_incomplete_object_mutation_line_covering(lines, idx)
        targets = _object_define_properties_call_targets(scan_line)
        if not targets and _object_define_properties_call_unclosed(scan_line):
            return True
        for target, fully_synthesizable in targets:
            if fully_synthesizable:
                continue
            if target in receivers:
                return True
    return False


def _tip_extra_opaque_reflect_set_shares_receiver(
    *, baseline_blob: str, head_blob: str, candidate_keys: set[str]
) -> bool:
    """Return True when tip-extra opaque ``Reflect.set`` shares a salvage receiver.

    ``Reflect.set(guard, key, …)`` emits only ``Reflect`` / ``Reflect.set`` call
    names, which never equal salvaged ``guard.enabled``. Fail closed when the
    target argument matches a salvaged receiver and the property name is not a
    string literal (PRRT_kwDOSJAM6s6ZzN-l). Literal property forms synthesize
    ``guard.enabled`` via binding names instead, so unrelated keys keep salvage
    like ``Reflect.set(guard, "other", …)``. Multiline
    ``Reflect.set(\\n  guard,\\n  …)`` joins continued argument lists before
    scanning, looking back from tip-extra argument lines when the shared opener
    is not tip-extra; an opener that remains unclosed with no parseable target
    after look-ahead also fails closed.
    """
    receivers = _salvaged_alias_reference_names(candidate_keys)
    if not receivers:
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
        scan_line = _join_incomplete_object_mutation_line_covering(lines, idx)
        targets = _reflect_set_call_targets(scan_line)
        if not targets and _reflect_set_call_unclosed(scan_line):
            return True
        for target, fully_synthesizable in targets:
            if fully_synthesizable:
                continue
            if target in receivers:
                return True
    return False
