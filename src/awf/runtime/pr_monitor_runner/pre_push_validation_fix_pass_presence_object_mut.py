"""Object.assign / defineProperty(ies) / Reflect.set salvage mutation scanners."""

from __future__ import annotations

import re

# Imported late-safe shared fragments/helpers from assigns (defined before this
# module is loaded via deferred import at the bottom of assigns).
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_assigns import (
    _SETATTR_OBJ as _SETATTR_OBJ,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_assigns import (
    _UPDATE_DICT_LITERAL_RE as _UPDATE_DICT_LITERAL_RE,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_assigns import (
    _helper_keyword_executable as _helper_keyword_executable,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_assigns import (
    _split_top_level_call_args as _split_top_level_call_args,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_assigns import (
    _update_call_argument_span as _update_call_argument_span,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_calls import (
    _executable_call_scan_text as _executable_call_scan_text,
)

# JS ``Object.assign(guard, {enabled: false})`` mutates attributes without an
# equals-style binding; call names are only ``Object`` / ``Object.assign``, which
# never intersect salvaged ``guard.enabled`` (PRRT_kwDOSJAM6s6Zxwhs).
_INLINE_OBJECT_ASSIGN_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_]))"
    r"(?:[A-Za-z_][A-Za-z0-9_]*\.)*Object\.assign"
    r"[ \t]*\("
)
_OBJECT_ASSIGN_TARGET_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_]))"
    r"(?:[A-Za-z_][A-Za-z0-9_]*\.)*Object\.assign"
    rf"[ \t]*\([ \t]*({_SETATTR_OBJ})"
    r"(?=[ \t]*[,)])"
)
_OBJECT_LITERAL_KEY_ENTRY_RE = re.compile(
    r"^(?:"
    r'"([^"\n]+)"'
    r"|'([^'\n]+)'"
    r"|([A-Za-z_][A-Za-z0-9_]*)"
    r")"
    r"(?:[ \t]*:|[ \t]*$)"
)
# JS ``Object.defineProperty(guard, "enabled", {value: false})`` mutates an
# attribute without an equals-style binding; call names are only ``Object`` /
# ``Object.defineProperty``, which never intersect salvaged ``guard.enabled``
# (PRRT_kwDOSJAM6s6Zy4pR).
_INLINE_OBJECT_DEFINE_PROPERTY_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_]))"
    r"(?:[A-Za-z_][A-Za-z0-9_]*\.)*Object\.defineProperty"
    r"[ \t]*\("
)
_OBJECT_DEFINE_PROPERTY_TARGET_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_]))"
    r"(?:[A-Za-z_][A-Za-z0-9_]*\.)*Object\.defineProperty"
    rf"[ \t]*\([ \t]*({_SETATTR_OBJ})"
    r"(?=[ \t]*[,)])"
)
_OBJECT_DEFINE_PROPERTY_PROP_LIT_RE = re.compile(r'^(?:"([^"\n]+)"|\'([^\'\n]+)\')$')
# JS ``Object.defineProperties(guard, {enabled: {value: false}})`` mutates
# attributes without an equals-style binding; call names are only ``Object`` /
# ``Object.defineProperties``, which never intersect salvaged ``guard.enabled``
# (PRRT_kwDOSJAM6s6ZzifG).
_INLINE_OBJECT_DEFINE_PROPERTIES_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_]))"
    r"(?:[A-Za-z_][A-Za-z0-9_]*\.)*Object\.defineProperties"
    r"[ \t]*\("
)
_OBJECT_DEFINE_PROPERTIES_TARGET_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_]))"
    r"(?:[A-Za-z_][A-Za-z0-9_]*\.)*Object\.defineProperties"
    rf"[ \t]*\([ \t]*({_SETATTR_OBJ})"
    r"(?=[ \t]*[,)])"
)
# JS ``Reflect.set(guard, "enabled", false)`` mutates an attribute without an
# equals-style binding; call names are only ``Reflect`` / ``Reflect.set``, which
# never intersect salvaged ``guard.enabled`` (PRRT_kwDOSJAM6s6ZzN-l).
_INLINE_REFLECT_SET_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_]))"
    r"(?:[A-Za-z_][A-Za-z0-9_]*\.)*Reflect\.set"
    r"[ \t]*\("
)
_REFLECT_SET_TARGET_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_]))"
    r"(?:[A-Za-z_][A-Za-z0-9_]*\.)*Reflect\.set"
    rf"[ \t]*\([ \t]*({_SETATTR_OBJ})"
    r"(?=[ \t]*[,)])"
)


def _object_literal_entry_key(entry: str) -> str | None:
    """Return a synthesizable object-literal key, or None when opaque."""
    stripped = entry.strip()
    if not stripped or stripped.startswith("...") or stripped.startswith("["):
        return None
    match = _OBJECT_LITERAL_KEY_ENTRY_RE.match(stripped)
    if match is None:
        return None
    return match.group(1) or match.group(2) or match.group(3)


def _object_assign_source_fully_synthesizable(arg: str) -> bool:
    """Return True when one ``Object.assign`` source is a plain object literal."""
    stripped = arg.strip()
    dict_match = _UPDATE_DICT_LITERAL_RE.match(stripped)
    if dict_match is None:
        return False
    body = dict_match.group(1)
    if not body.strip():
        return True
    for entry in _split_top_level_call_args(body):
        if _object_literal_entry_key(entry) is None:
            return False
    return True


def _object_assign_target_and_args(
    raw_line: str, *, match_start: int
) -> tuple[str, list[str] | None] | None:
    """Return ``(target, source_args)`` for an ``Object.assign`` call.

    ``source_args`` is ``None`` when the argument list is unclosed (fail closed
    on a shared salvaged receiver). Returns ``None`` when no target can be read.
    """
    target_match = _OBJECT_ASSIGN_TARGET_RE.match(raw_line, match_start)
    if target_match is None:
        return None
    target = target_match.group(1)
    open_paren = raw_line.find("(", match_start)
    if open_paren < 0:  # pragma: no cover — INLINE/TARGET patterns already require '('
        return None
    args = _update_call_argument_span(raw_line, open_paren)
    if args is None:
        return target, None
    parts = _split_top_level_call_args(args)
    if not parts:  # pragma: no cover — TARGET match implies at least the receiver arg
        return target, None
    return target, parts[1:]


def _object_assign_call_targets(
    raw_line: str,
) -> tuple[tuple[str, bool], ...]:
    """Return ``(target, sources_fully_synthesizable)`` for each Object.assign.

    Unclosed or opaque sources report ``sources_fully_synthesizable=False`` so
    tip-extra fail-closed can drop stale salvage (PRRT_kwDOSJAM6s6Zxwhs).
    """
    stripped = raw_line.lstrip(" \t")
    if stripped.startswith("//") or stripped.startswith("#"):
        return ()
    scan = _executable_call_scan_text(raw_line)
    out: list[tuple[str, bool]] = []
    for match in _INLINE_OBJECT_ASSIGN_RE.finditer(raw_line):
        if not _helper_keyword_executable(
            raw_line=raw_line,
            scan=scan,
            match_start=match.start(),
            tokens=("assign",),
        ):
            continue
        parsed = _object_assign_target_and_args(raw_line, match_start=match.start())
        if parsed is None:
            continue
        target, sources = parsed
        if sources is None:
            out.append((target, False))
            continue
        fully = all(_object_assign_source_fully_synthesizable(src) for src in sources)
        out.append((target, fully))
    return tuple(out)


def _object_assign_call_unclosed(raw_line: str) -> bool:
    """Return True when an executable ``Object.assign(`` lacks a closing ``)``.

    Formatters split ``Object.assign(guard, {enabled: false})`` across lines;
    per-line scanners then see no target on the opener and no mutation on
    continuations (PRRT_kwDOSJAM6s6Zyo4_).
    """
    stripped = raw_line.lstrip(" \t")
    if stripped.startswith("//") or stripped.startswith("#"):
        return False
    scan = _executable_call_scan_text(raw_line)
    for match in _INLINE_OBJECT_ASSIGN_RE.finditer(raw_line):
        if not _helper_keyword_executable(
            raw_line=raw_line,
            scan=scan,
            match_start=match.start(),
            tokens=("assign",),
        ):
            continue
        open_paren = raw_line.find("(", match.start())
        if open_paren < 0:  # pragma: no cover — INLINE pattern already requires '('
            continue
        if _update_call_argument_span(raw_line, open_paren) is None:
            return True
    return False


def _object_assign_join_gap_skippable(stripped: str) -> bool:
    """Return True when ``stripped`` is only blank / comment between call lines."""
    if stripped == "" or stripped.startswith("//") or stripped.startswith("#"):
        return True
    if stripped.startswith("/*") and "*/" in stripped:
        after = stripped.split("*/", 1)[1].strip()
        return after == ""
    return False


def _join_incomplete_object_assign_line(lines: list[str], idx: int) -> str:
    """Join ``lines[idx]`` with following lines until ``Object.assign(…)`` closes.

    Tip-extra scanners must see the target and sources together; otherwise
    multiline assigns retain stale salvage (PRRT_kwDOSJAM6s6Zyo4_). Skip blank /
    line-comment / whole-line ``/* … */`` gaps. Stop at the first join that
    closes every ``Object.assign`` on the opener, or at EOF (caller fail-closes).
    """
    raw_line = lines[idx]
    if not _object_assign_call_unclosed(raw_line):
        return raw_line
    joined = raw_line.rstrip()
    j = idx + 1
    while j < len(lines):
        nxt = lines[j]
        nxt_stripped = nxt.strip()
        if _object_assign_join_gap_skippable(nxt_stripped):
            j += 1
            continue
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
                joined = f"{joined} {after}"
                break
            else:
                break
            if not _object_assign_call_unclosed(joined):
                break
            continue
        joined = f"{joined} {nxt.lstrip(' \t')}"
        j += 1
        if not _object_assign_call_unclosed(joined):
            break
    return joined


def _object_assign_mutation_args_fully_synthesizable(raw_line: str) -> bool:
    """Return True when every ``Object.assign`` source arg is a plain object literal.

    Mixed forms such as ``Object.assign(guard, {other: false}, extra)`` still
    synthesize literal keys, but the opaque mapping can overwrite other salvaged
    attributes — those must fail closed (PRRT_kwDOSJAM6s6Zxwhs).
    """
    calls = _object_assign_call_targets(raw_line)
    if not calls:
        return False
    return all(fully for _target, fully in calls)


def _object_assign_mutation_binding_names(raw_line: str) -> tuple[str, ...]:
    """Return ``target.key`` keys mutated by ``Object.assign`` object literals.

    Tip-extra ``Object.assign(guard, {enabled: false})`` must supersede salvage
    of ``guard.enabled``; assign and call scanners alone leave the salvage
    retained (PRRT_kwDOSJAM6s6Zxwhs). Opaque sources synthesize nothing here and
    are handled by tip-extra receiver fail-closed.
    """
    stripped = raw_line.lstrip(" \t")
    if stripped.startswith("//") or stripped.startswith("#"):
        return ()
    scan = _executable_call_scan_text(raw_line)
    names: list[str] = []
    for match in _INLINE_OBJECT_ASSIGN_RE.finditer(raw_line):
        if not _helper_keyword_executable(
            raw_line=raw_line,
            scan=scan,
            match_start=match.start(),
            tokens=("assign",),
        ):
            continue
        parsed = _object_assign_target_and_args(raw_line, match_start=match.start())
        if parsed is None:
            continue
        target, sources = parsed
        if sources is None:
            continue
        for source in sources:
            dict_match = _UPDATE_DICT_LITERAL_RE.match(source.strip())
            if dict_match is None:
                continue
            body = dict_match.group(1)
            if not body.strip():
                continue
            for entry in _split_top_level_call_args(body):
                key = _object_literal_entry_key(entry)
                if not key:
                    continue
                binding = f"{target}.{key}"
                if binding not in names:
                    names.append(binding)
    return tuple(names)


def _object_define_property_literal_key(prop_arg: str) -> str | None:
    """Return a string-literal property name, or None when opaque."""
    match = _OBJECT_DEFINE_PROPERTY_PROP_LIT_RE.match(prop_arg.strip())
    if match is None:
        return None
    return match.group(1) or match.group(2)


def _object_define_property_target_and_args(
    raw_line: str, *, match_start: int
) -> tuple[str, list[str] | None] | None:
    """Return ``(target, remaining_args)`` for an ``Object.defineProperty`` call.

    ``remaining_args`` is ``None`` when the argument list is unclosed (fail closed
    on a shared salvaged receiver). Returns ``None`` when no target can be read.
    """
    target_match = _OBJECT_DEFINE_PROPERTY_TARGET_RE.match(raw_line, match_start)
    if target_match is None:
        return None
    target = target_match.group(1)
    open_paren = raw_line.find("(", match_start)
    if open_paren < 0:  # pragma: no cover — INLINE/TARGET patterns already require '('
        return None
    args = _update_call_argument_span(raw_line, open_paren)
    if args is None:
        return target, None
    parts = _split_top_level_call_args(args)
    if not parts:  # pragma: no cover — TARGET match implies at least the receiver arg
        return target, None
    return target, parts[1:]


def _object_define_property_call_targets(
    raw_line: str,
) -> tuple[tuple[str, bool], ...]:
    """Return ``(target, prop_fully_synthesizable)`` for each defineProperty.

    Unclosed or opaque property names report ``prop_fully_synthesizable=False`` so
    tip-extra fail-closed can drop stale salvage (PRRT_kwDOSJAM6s6Zy4pR).
    """
    stripped = raw_line.lstrip(" \t")
    if stripped.startswith("//") or stripped.startswith("#"):
        return ()
    scan = _executable_call_scan_text(raw_line)
    out: list[tuple[str, bool]] = []
    for match in _INLINE_OBJECT_DEFINE_PROPERTY_RE.finditer(raw_line):
        if not _helper_keyword_executable(
            raw_line=raw_line,
            scan=scan,
            match_start=match.start(),
            tokens=("defineProperty",),
        ):
            continue
        parsed = _object_define_property_target_and_args(raw_line, match_start=match.start())
        if parsed is None:
            continue
        target, rest = parsed
        if rest is None or not rest:
            out.append((target, False))
            continue
        fully = _object_define_property_literal_key(rest[0]) is not None
        out.append((target, fully))
    return tuple(out)


def _object_define_property_call_unclosed(raw_line: str) -> bool:
    """Return True when an executable ``Object.defineProperty(`` lacks ``)``.

    Formatters split ``Object.defineProperty(guard, "enabled", {…})`` across
    lines; per-line scanners then see no target on the opener and no mutation on
    continuations (PRRT_kwDOSJAM6s6Zy4pR).
    """
    stripped = raw_line.lstrip(" \t")
    if stripped.startswith("//") or stripped.startswith("#"):
        return False
    scan = _executable_call_scan_text(raw_line)
    for match in _INLINE_OBJECT_DEFINE_PROPERTY_RE.finditer(raw_line):
        if not _helper_keyword_executable(
            raw_line=raw_line,
            scan=scan,
            match_start=match.start(),
            tokens=("defineProperty",),
        ):
            continue
        open_paren = raw_line.find("(", match.start())
        if open_paren < 0:  # pragma: no cover — INLINE pattern already requires '('
            continue
        if _update_call_argument_span(raw_line, open_paren) is None:
            return True
    return False


def _join_incomplete_object_define_property_line(lines: list[str], idx: int) -> str:
    """Join ``lines[idx]`` with following lines until ``defineProperty(…)`` closes.

    Tip-extra scanners must see the target and property together; otherwise
    multiline defines retain stale salvage (PRRT_kwDOSJAM6s6Zy4pR).
    """
    raw_line = lines[idx]
    if not _object_define_property_call_unclosed(raw_line):
        return raw_line
    joined = raw_line.rstrip()
    j = idx + 1
    while j < len(lines):
        nxt = lines[j]
        nxt_stripped = nxt.strip()
        if _object_assign_join_gap_skippable(nxt_stripped):
            j += 1
            continue
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
                joined = f"{joined} {after}"
                break
            else:
                break
            if not _object_define_property_call_unclosed(joined):
                break
            continue
        joined = f"{joined} {nxt.lstrip(' \t')}"
        j += 1
        if not _object_define_property_call_unclosed(joined):
            break
    return joined


def _object_define_properties_target_and_args(
    raw_line: str, *, match_start: int
) -> tuple[str, list[str] | None] | None:
    """Return ``(target, remaining_args)`` for an ``Object.defineProperties`` call.

    ``remaining_args`` is ``None`` when the argument list is unclosed (fail closed
    on a shared salvaged receiver). Returns ``None`` when no target can be read.
    """
    target_match = _OBJECT_DEFINE_PROPERTIES_TARGET_RE.match(raw_line, match_start)
    if target_match is None:
        return None
    target = target_match.group(1)
    open_paren = raw_line.find("(", match_start)
    if open_paren < 0:  # pragma: no cover — INLINE/TARGET patterns already require '('
        return None
    args = _update_call_argument_span(raw_line, open_paren)
    if args is None:
        return target, None
    parts = _split_top_level_call_args(args)
    if not parts:  # pragma: no cover — TARGET match implies at least the receiver arg
        return target, None
    return target, parts[1:]


def _object_define_properties_call_targets(
    raw_line: str,
) -> tuple[tuple[str, bool], ...]:
    """Return ``(target, props_fully_synthesizable)`` for each defineProperties.

    Unclosed or opaque descriptor maps report ``props_fully_synthesizable=False``
    so tip-extra fail-closed can drop stale salvage (PRRT_kwDOSJAM6s6ZzifG).
    """
    stripped = raw_line.lstrip(" \t")
    if stripped.startswith("//") or stripped.startswith("#"):
        return ()
    scan = _executable_call_scan_text(raw_line)
    out: list[tuple[str, bool]] = []
    for match in _INLINE_OBJECT_DEFINE_PROPERTIES_RE.finditer(raw_line):
        if not _helper_keyword_executable(
            raw_line=raw_line,
            scan=scan,
            match_start=match.start(),
            tokens=("defineProperties",),
        ):
            continue
        parsed = _object_define_properties_target_and_args(raw_line, match_start=match.start())
        if parsed is None:
            continue
        target, rest = parsed
        if rest is None or not rest:
            out.append((target, False))
            continue
        # Single descriptors map (like Object.assign sources).
        fully = all(_object_assign_source_fully_synthesizable(src) for src in rest)
        out.append((target, fully))
    return tuple(out)


def _object_define_properties_call_unclosed(raw_line: str) -> bool:
    """Return True when an executable ``Object.defineProperties(`` lacks ``)``.

    Formatters split ``Object.defineProperties(guard, {enabled: {…}})`` across
    lines; per-line scanners then see no target on the opener and no mutation on
    continuations (PRRT_kwDOSJAM6s6ZzifG).
    """
    stripped = raw_line.lstrip(" \t")
    if stripped.startswith("//") or stripped.startswith("#"):
        return False
    scan = _executable_call_scan_text(raw_line)
    for match in _INLINE_OBJECT_DEFINE_PROPERTIES_RE.finditer(raw_line):
        if not _helper_keyword_executable(
            raw_line=raw_line,
            scan=scan,
            match_start=match.start(),
            tokens=("defineProperties",),
        ):
            continue
        open_paren = raw_line.find("(", match.start())
        if open_paren < 0:  # pragma: no cover — INLINE pattern already requires '('
            continue
        if _update_call_argument_span(raw_line, open_paren) is None:
            return True
    return False


def _join_incomplete_object_define_properties_line(lines: list[str], idx: int) -> str:
    """Join ``lines[idx]`` with following lines until ``defineProperties(…)`` closes.

    Tip-extra scanners must see the target and descriptors together; otherwise
    multiline defines retain stale salvage (PRRT_kwDOSJAM6s6ZzifG).
    """
    raw_line = lines[idx]
    if not _object_define_properties_call_unclosed(raw_line):
        return raw_line
    joined = raw_line.rstrip()
    j = idx + 1
    while j < len(lines):
        nxt = lines[j]
        nxt_stripped = nxt.strip()
        if _object_assign_join_gap_skippable(nxt_stripped):
            j += 1
            continue
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
                joined = f"{joined} {after}"
                break
            else:
                break
            if not _object_define_properties_call_unclosed(joined):
                break
            continue
        joined = f"{joined} {nxt.lstrip(' \t')}"
        j += 1
        if not _object_define_properties_call_unclosed(joined):
            break
    return joined


def _object_define_properties_mutation_args_fully_synthesizable(raw_line: str) -> bool:
    """Return True when every ``defineProperties`` descriptors arg is synthesizable."""
    calls = _object_define_properties_call_targets(raw_line)
    if not calls:
        return False
    return all(fully for _target, fully in calls)


def _object_define_properties_mutation_binding_names(raw_line: str) -> tuple[str, ...]:
    """Return ``target.key`` keys mutated by ``Object.defineProperties`` literals.

    Tip-extra ``Object.defineProperties(guard, {enabled: {value: false}})`` must
    supersede salvage of ``guard.enabled``; assign and call scanners alone leave
    the salvage retained (PRRT_kwDOSJAM6s6ZzifG). Opaque descriptor maps
    synthesize nothing here and are handled by tip-extra receiver fail-closed.
    """
    stripped = raw_line.lstrip(" \t")
    if stripped.startswith("//") or stripped.startswith("#"):
        return ()
    scan = _executable_call_scan_text(raw_line)
    names: list[str] = []
    for match in _INLINE_OBJECT_DEFINE_PROPERTIES_RE.finditer(raw_line):
        if not _helper_keyword_executable(
            raw_line=raw_line,
            scan=scan,
            match_start=match.start(),
            tokens=("defineProperties",),
        ):
            continue
        parsed = _object_define_properties_target_and_args(raw_line, match_start=match.start())
        if parsed is None:
            continue
        target, sources = parsed
        if sources is None:
            continue
        for source in sources:
            dict_match = _UPDATE_DICT_LITERAL_RE.match(source.strip())
            if dict_match is None:
                continue
            body = dict_match.group(1)
            if not body.strip():
                continue
            for entry in _split_top_level_call_args(body):
                key = _object_literal_entry_key(entry)
                if not key:
                    continue
                binding = f"{target}.{key}"
                if binding not in names:
                    names.append(binding)
    return tuple(names)


def _join_incomplete_object_mutation_line(lines: list[str], idx: int) -> str:
    """Join incomplete Object.assign / defineProperty(ies) / Reflect.set args."""
    raw_line = lines[idx]
    if _object_assign_call_unclosed(raw_line):
        return _join_incomplete_object_assign_line(lines, idx)
    if _object_define_properties_call_unclosed(raw_line):
        return _join_incomplete_object_define_properties_line(lines, idx)
    if _object_define_property_call_unclosed(raw_line):
        return _join_incomplete_object_define_property_line(lines, idx)
    if _reflect_set_call_unclosed(raw_line):
        return _join_incomplete_reflect_set_line(lines, idx)
    return raw_line


def _object_mutation_join_last_index(lines: list[str], opener_idx: int) -> int:
    """Return the last line index consumed when joining from ``opener_idx``."""
    raw_line = lines[opener_idx]
    if _object_assign_call_unclosed(raw_line):
        unclosed = _object_assign_call_unclosed
    elif _object_define_properties_call_unclosed(raw_line):
        unclosed = _object_define_properties_call_unclosed
    elif _object_define_property_call_unclosed(raw_line):
        unclosed = _object_define_property_call_unclosed
    elif _reflect_set_call_unclosed(raw_line):
        unclosed = _reflect_set_call_unclosed
    else:
        return opener_idx
    joined = raw_line.rstrip()
    j = opener_idx + 1
    last = opener_idx
    while j < len(lines):
        nxt = lines[j]
        nxt_stripped = nxt.strip()
        if _object_assign_join_gap_skippable(nxt_stripped):
            j += 1
            continue
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
                joined = f"{joined} {after}"
                last = j - 1
                break
            else:
                break
            if not unclosed(joined):
                break
            continue
        joined = f"{joined} {nxt.lstrip(' \t')}"
        last = j
        j += 1
        if not unclosed(joined):
            break
    return last


def _join_incomplete_object_mutation_line_covering(lines: list[str], idx: int) -> str:
    """Join Object.assign / defineProperty(ies) / Reflect.set covering ``idx``.

    Tip-extra scanners only visit tip-extra indices. When salvage already has a
    shared ``Object.assign(`` / ``Object.defineProperty(`` /
    ``Object.defineProperties(`` / ``Reflect.set(`` opener and the tip only edits
    argument lines, forward join from the arg line sees no call — look back for
    an unclosed opener whose forward join includes ``idx``
    (PRRT_kwDOSJAM6s6Zy5DN). Nested mutation openers in earlier arguments may
    close before ``idx``; skip those and keep looking for an outer opener that
    still covers the tip line (PRRT_kwDOSJAM6s6ZzLlE).
    """
    forward = _join_incomplete_object_mutation_line(lines, idx)
    raw = lines[idx]
    if (
        _object_assign_call_unclosed(raw)
        or _object_define_properties_call_unclosed(raw)
        or _object_define_property_call_unclosed(raw)
        or _reflect_set_call_unclosed(raw)
        or _object_assign_call_targets(forward)
        or _object_define_properties_call_targets(forward)
        or _object_define_property_call_targets(forward)
        or _reflect_set_call_targets(forward)
    ):
        return forward
    for opener_idx in range(idx - 1, -1, -1):
        opener_raw = lines[opener_idx]
        opener_stripped = opener_raw.strip()
        if opener_stripped == "":
            continue
        if not (
            _object_assign_call_unclosed(opener_raw)
            or _object_define_properties_call_unclosed(opener_raw)
            or _object_define_property_call_unclosed(opener_raw)
            or _reflect_set_call_unclosed(opener_raw)
        ):
            continue
        if _object_mutation_join_last_index(lines, opener_idx) >= idx:
            return _join_incomplete_object_mutation_line(lines, opener_idx)
        # Nested opener closed before ``idx`` — keep looking for an outer cover.
    return forward


def _object_define_property_mutation_args_fully_synthesizable(raw_line: str) -> bool:
    """Return True when every ``defineProperty`` property arg is a string literal."""
    calls = _object_define_property_call_targets(raw_line)
    if not calls:
        return False
    return all(fully for _target, fully in calls)


def _object_define_property_mutation_binding_names(raw_line: str) -> tuple[str, ...]:
    """Return ``target.key`` keys mutated by ``Object.defineProperty`` literals.

    Tip-extra ``Object.defineProperty(guard, "enabled", {value: false})`` must
    supersede salvage of ``guard.enabled``; assign and call scanners alone leave
    the salvage retained (PRRT_kwDOSJAM6s6Zy4pR). Opaque property names synthesize
    nothing here and are handled by tip-extra receiver fail-closed.
    """
    stripped = raw_line.lstrip(" \t")
    if stripped.startswith("//") or stripped.startswith("#"):
        return ()
    scan = _executable_call_scan_text(raw_line)
    names: list[str] = []
    for match in _INLINE_OBJECT_DEFINE_PROPERTY_RE.finditer(raw_line):
        if not _helper_keyword_executable(
            raw_line=raw_line,
            scan=scan,
            match_start=match.start(),
            tokens=("defineProperty",),
        ):
            continue
        parsed = _object_define_property_target_and_args(raw_line, match_start=match.start())
        if parsed is None:
            continue
        target, rest = parsed
        if rest is None or not rest:
            continue
        key = _object_define_property_literal_key(rest[0])
        if not key:
            continue
        binding = f"{target}.{key}"
        if binding not in names:
            names.append(binding)
    return tuple(names)


def _reflect_set_literal_key(prop_arg: str) -> str | None:
    """Return a string-literal property name, or None when opaque."""
    return _object_define_property_literal_key(prop_arg)


def _reflect_set_target_and_args(
    raw_line: str, *, match_start: int
) -> tuple[str, list[str] | None] | None:
    """Return ``(target, remaining_args)`` for a ``Reflect.set`` call.

    ``remaining_args`` is ``None`` when the argument list is unclosed (fail closed
    on a shared salvaged receiver). Returns ``None`` when no target can be read.
    """
    target_match = _REFLECT_SET_TARGET_RE.match(raw_line, match_start)
    if target_match is None:
        return None
    target = target_match.group(1)
    open_paren = raw_line.find("(", match_start)
    if open_paren < 0:  # pragma: no cover — INLINE/TARGET patterns already require '('
        return None
    args = _update_call_argument_span(raw_line, open_paren)
    if args is None:
        return target, None
    parts = _split_top_level_call_args(args)
    if not parts:  # pragma: no cover — TARGET match implies at least the receiver arg
        return target, None
    return target, parts[1:]


def _reflect_set_call_targets(
    raw_line: str,
) -> tuple[tuple[str, bool], ...]:
    """Return ``(target, prop_fully_synthesizable)`` for each Reflect.set.

    Unclosed or opaque property names report ``prop_fully_synthesizable=False`` so
    tip-extra fail-closed can drop stale salvage (PRRT_kwDOSJAM6s6ZzN-l).
    """
    stripped = raw_line.lstrip(" \t")
    if stripped.startswith("//") or stripped.startswith("#"):
        return ()
    scan = _executable_call_scan_text(raw_line)
    out: list[tuple[str, bool]] = []
    for match in _INLINE_REFLECT_SET_RE.finditer(raw_line):
        if not _helper_keyword_executable(
            raw_line=raw_line,
            scan=scan,
            match_start=match.start(),
            tokens=("set",),
        ):
            continue
        parsed = _reflect_set_target_and_args(raw_line, match_start=match.start())
        if parsed is None:
            continue
        target, rest = parsed
        if rest is None or not rest:
            out.append((target, False))
            continue
        fully = _reflect_set_literal_key(rest[0]) is not None
        out.append((target, fully))
    return tuple(out)


def _reflect_set_call_unclosed(raw_line: str) -> bool:
    """Return True when an executable ``Reflect.set(`` lacks ``)``.

    Formatters split ``Reflect.set(guard, "enabled", false)`` across lines;
    per-line scanners then see no target on the opener and no mutation on
    continuations (PRRT_kwDOSJAM6s6ZzN-l).
    """
    stripped = raw_line.lstrip(" \t")
    if stripped.startswith("//") or stripped.startswith("#"):
        return False
    scan = _executable_call_scan_text(raw_line)
    for match in _INLINE_REFLECT_SET_RE.finditer(raw_line):
        if not _helper_keyword_executable(
            raw_line=raw_line,
            scan=scan,
            match_start=match.start(),
            tokens=("set",),
        ):
            continue
        open_paren = raw_line.find("(", match.start())
        if open_paren < 0:  # pragma: no cover — INLINE pattern already requires '('
            continue
        if _update_call_argument_span(raw_line, open_paren) is None:
            return True
    return False


def _join_incomplete_reflect_set_line(lines: list[str], idx: int) -> str:
    """Join ``lines[idx]`` with following lines until ``Reflect.set(…)`` closes.

    Tip-extra scanners must see the target and property together; otherwise
    multiline Reflect.set retains stale salvage (PRRT_kwDOSJAM6s6ZzN-l).
    """
    raw_line = lines[idx]
    if not _reflect_set_call_unclosed(raw_line):
        return raw_line
    joined = raw_line.rstrip()
    j = idx + 1
    while j < len(lines):
        nxt = lines[j]
        nxt_stripped = nxt.strip()
        if _object_assign_join_gap_skippable(nxt_stripped):
            j += 1
            continue
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
                joined = f"{joined} {after}"
                break
            else:
                break
            if not _reflect_set_call_unclosed(joined):
                break
            continue
        joined = f"{joined} {nxt.lstrip(' \t')}"
        j += 1
        if not _reflect_set_call_unclosed(joined):
            break
    return joined


def _reflect_set_mutation_args_fully_synthesizable(raw_line: str) -> bool:
    """Return True when every ``Reflect.set`` property arg is a string literal."""
    calls = _reflect_set_call_targets(raw_line)
    if not calls:
        return False
    return all(fully for _target, fully in calls)


def _reflect_set_mutation_binding_names(raw_line: str) -> tuple[str, ...]:
    """Return ``target.key`` keys mutated by ``Reflect.set`` literals.

    Tip-extra ``Reflect.set(guard, "enabled", false)`` must supersede salvage of
    ``guard.enabled``; assign and call scanners alone leave the salvage retained
    (PRRT_kwDOSJAM6s6ZzN-l). Opaque property names synthesize nothing here and
    are handled by tip-extra receiver fail-closed.
    """
    stripped = raw_line.lstrip(" \t")
    if stripped.startswith("//") or stripped.startswith("#"):
        return ()
    scan = _executable_call_scan_text(raw_line)
    names: list[str] = []
    for match in _INLINE_REFLECT_SET_RE.finditer(raw_line):
        if not _helper_keyword_executable(
            raw_line=raw_line,
            scan=scan,
            match_start=match.start(),
            tokens=("set",),
        ):
            continue
        parsed = _reflect_set_target_and_args(raw_line, match_start=match.start())
        if parsed is None:
            continue
        target, rest = parsed
        if rest is None or not rest:
            continue
        key = _reflect_set_literal_key(rest[0])
        if not key:
            continue
        binding = f"{target}.{key}"
        if binding not in names:
            names.append(binding)
    return tuple(names)
