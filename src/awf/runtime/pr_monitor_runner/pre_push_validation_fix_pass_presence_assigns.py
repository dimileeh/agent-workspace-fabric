"""Assign / declaration binding scanners for salvage presence checks."""

from __future__ import annotations

import re

from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_calls import (
    _executable_call_scan_text as _executable_call_scan_text,
)

# Binding targets that an appended tip can rebind to supersede added salvage.
# Optional YAML block-sequence marker (``- ``) before the key so list-item
# mappings bind like nested leaves (PRRT_kwDOSJAM6s6ZqeWt).
# Optional shell ``export `` / ``declare … `` / ``typeset … `` / ``readonly … ``
# before the key so ``export FEATURE_ENABLED=true``, ``declare -x
# FEATURE_ENABLED=true``, and ``readonly FEATURE_ENABLED=true`` bind like bare
# ``FEATURE_ENABLED=true`` (PRRT_kwDOSJAM6s6ZqseO, PRRT_kwDOSJAM6s6ZqxX4,
# PRRT_kwDOSJAM6s6ZrBJF). Declaration forms (``export class`` / ``export const``)
# still match earlier patterns.
# Bare keys allow ``-`` so TOML / YAML hyphenated names bind
# (``feature-enabled = true``; PRRT_kwDOSJAM6s6Zqip3).
# TOML dotted keys join bare or quoted segments with ``.``
# (``feature.enabled`` / ``site."google.com"``; PRRT_kwDOSJAM6s6Zql88).
_ASSIGN_KEY_SEGMENT = r'(?:[A-Za-z_][A-Za-z0-9_-]*|"[^"\n]+"|\'[^\'\n]+\')'
# Subscript index: bare ident, decimal, or quoted string (PRRT_kwDOSJAM6s6ZsQFs).
_ASSIGN_SUBSCRIPT_INDEX = r'(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+|"[^"\n]+"|\'[^\'\n]+\')'
# ``FLAGS["enabled"]`` / ``cfg.flags[0]`` / nested ``a["b"]["c"]`` — requires
# ≥1 ``[...]`` so bare/dotted assign alts stay distinct.
_ASSIGN_SUBSCRIPT_TARGET = (
    r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*"
    rf"(?:\[{_ASSIGN_SUBSCRIPT_INDEX}\])+"
)
# Same shape for ``_executable_call_scan_text`` output where quoted indices are
# spaces (``FLAGS[         ]``); bracket bodies may be whitespace-only
# (PRRT_kwDOSJAM6s6ZsQFs; compare computed call sites PRRT_kwDOSJAM6s6ZroRa).
_ASSIGN_SUBSCRIPT_TARGET_SCAN = (
    r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*(?:\[[^\]]*\])+"
)
# Plain ``=`` or compound ``+=`` / ``-=`` / ``*=`` / ``/=`` / ``%=`` / ``**=`` /
# ``//=`` / ``&=`` / ``|=`` / ``^=`` / ``<<=`` / ``>>=``. Longest ops first so
# ``**=`` is not split as ``*`` + ``*=``. ``==`` is excluded via ``=(?!=)``
# (PRRT_kwDOSJAM6s6ZsNCC).
_EQUALS_STYLE_ASSIGN_OP = r"(?:<<=|>>=|\*\*=|//=|[+\-*/%&|^]=|=(?!=))"
# Statement-leading assign forms (line-start). Mid-statement overrides such as
# ``if ready: FEATURE_ENABLED = False`` are collected separately via
# ``_INLINE_ASSIGN_BINDING_RE`` so tip-extra nested rebinds still supersede
# (PRRT_kwDOSJAM6s6ZsD5y).
_ASSIGN_BINDING_RE = re.compile(
    r"(?m)^[ \t]*(?:-[ \t]+)?(?:export[ \t]+|(?:declare|typeset|readonly)(?:[ \t]+-[A-Za-z]+)*[ \t]+)?(?:"
    # Dotted TOML keys (≥1 ``.``): require the full path before ``=`` / ``:``
    # so ``feature.enabled = true`` binds as ``feature.enabled``, not nothing.
    rf"({_ASSIGN_KEY_SEGMENT}(?:\.{_ASSIGN_KEY_SEGMENT})+)"
    r"(?:"
    rf"(?:[ \t]*:[ \t]*[^=\n]+)?[ \t]*{_EQUALS_STYLE_ASSIGN_OP}"
    r"|"
    r"[ \t]*:="
    r"|"
    r"[ \t]*:[ \t]*(?!=)"
    r")"
    r"|"
    # Subscript targets (``FLAGS["enabled"] =`` / ``cfg.flags[0] =``). Equals-
    # style only — bare YAML ``:`` is not used for computed LHS forms
    # (PRRT_kwDOSJAM6s6ZsQFs).
    rf"({_ASSIGN_SUBSCRIPT_TARGET})"
    r"(?:"
    rf"(?:[ \t]*:[ \t]*[^=\n]+)?[ \t]*{_EQUALS_STYLE_ASSIGN_OP}"
    r"|"
    r"[ \t]*:="
    r")"
    r"|"
    r"([A-Za-z_][A-Za-z0-9_-]*)"
    r"(?:"
    # `name =` / `name &=` / `name: T =` (typed optional; compound after type
    # still binds because ``[^=\n]+`` stops before the final ``=``).
    rf"(?:[ \t]*:[ \t]*[^=\n]+)?[ \t]*{_EQUALS_STYLE_ASSIGN_OP}"
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
    # PRRT_kwDOSJAM6s6ZqtHj). Compound ``=`` forms match too (PRRT_kwDOSJAM6s6ZsNCC).
    rf'("[^"\n]+")[ \t]*(?::[ \t]*(?!=)|{_EQUALS_STYLE_ASSIGN_OP})'
    r"|"
    rf"('[^'\n]+')[ \t]*(?::[ \t]*(?!=)|{_EQUALS_STYLE_ASSIGN_OP})"
    r")"
)
# Mid-line / nested ``name =`` / ``name &=`` / ``name :=`` / dotted ``a.b =`` /
# subscript ``FLAGS["enabled"] =`` (and shell ``export`` / ``declare`` /
# ``typeset`` / ``readonly`` forms). Typed ``name: T =`` is omitted here: the
# optional type span would treat ``if ready: FEATURE_ENABLED =`` as a typed
# bind of ``ready``. Statement-leading typed assigns stay on
# ``_ASSIGN_BINDING_RE``. Bare YAML ``key:`` is also omitted. Dotted paths and
# subscript targets are captured whole so ``feature.enabled =`` /
# ``FLAGS["enabled"] =`` do not emit a bare leaf (PRRT_kwDOSJAM6s6ZsD5y,
# PRRT_kwDOSJAM6s6ZsQFs). Compound ops supersede salvage like plain ``=``
# (PRRT_kwDOSJAM6s6ZsNCC).
_INLINE_ASSIGN_BINDING_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_]))"
    r"(?:export[ \t]+|(?:declare|typeset|readonly)(?:[ \t]+-[A-Za-z]+)*[ \t]+)?"
    rf"({_ASSIGN_SUBSCRIPT_TARGET_SCAN}|[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*)"
    r"(?:"
    rf"[ \t]*{_EQUALS_STYLE_ASSIGN_OP}"
    r"|"
    r"[ \t]*:="
    r")"
)
# Normalize ``FLAGS['enabled']`` → ``FLAGS["enabled"]`` so mixed quote spellings
# supersede each other (PRRT_kwDOSJAM6s6ZsQFs).
_SUBSCRIPT_INDEX_NORMALIZE_RE = re.compile(
    r"\[(?:"
    r'"(?P<double>[^"\n]+)"'
    r"|'(?P<single>[^'\n]+)'"
    r"|(?P<ident>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<num>[0-9]+)"
    r")\]"
)
# Same-line call kwargs / default parameters: ``name=`` after ``(`` or ``,``
# is not a rebind (PRRT_kwDOSJAM6s6ZsJyZ). Applied only inside unmatched ``(``
# … ``)`` and never for ``:=`` so bare unpacking / parenthesized walrus still
# count (PRRT_kwDOSJAM6s6ZsOT0).
_INLINE_ASSIGN_KWARG_BEFORE_RE = re.compile(r"[(,][ \t]*$")
# Prior targets in bare ``a, b, name =`` when the last name is kept as an
# unpacking bind (PRRT_kwDOSJAM6s6ZsOT0).
_INLINE_UNPACK_LHS_BEFORE_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_]))"
    r"((?:[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*)"
    r"(?:[ \t]*,[ \t]*[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*)*)"
    r"[ \t]*,[ \t]*$"
)
# Type token in statement-leading ``name: T =``: bare ``T =`` after ``ident:``
# must not invent a second binding key (PRRT_kwDOSJAM6s6ZsJyZ). Requires a
# bare identifier immediately before ``:`` so ``if ready: name =`` still binds.
_INLINE_ASSIGN_TYPE_ANNOTATION_BEFORE_RE = re.compile(
    r"(?:^|[;])[ \t]*(?:export[ \t]+)?[A-Za-z_][A-Za-z0-9_]*[ \t]*:[ \t]*$"
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


def _normalize_subscript_binding_name(name: str) -> str:
    """Normalize quote style inside subscript binding keys.

    ``FLAGS['enabled']`` and ``FLAGS["enabled"]`` must share one key so a tip
    rebind with the other quote spelling still supersedes salvage
    (PRRT_kwDOSJAM6s6ZsQFs). Bare-ident and decimal indices are unchanged.
    """

    def _repl(match: re.Match[str]) -> str:
        if match.group("double") is not None:
            return f'["{match.group("double")}"]'
        if match.group("single") is not None:
            return f'["{match.group("single")}"]'
        if match.group("ident") is not None:
            return f"[{match.group('ident')}]"
        return f"[{match.group('num')}]"

    return _SUBSCRIPT_INDEX_NORMALIZE_RE.sub(_repl, name)


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
    """Return the statement-leading binding name on ``raw_line``, or None.

    Pure ``#`` / ``//`` comment lines are skipped so commented rebinds do not
    count; ``#define`` / ``# define`` remain bindings (whitespace between ``#``
    and ``define`` is allowed, matching open-``#if`` scanning;
    PRRT_kwDOSJAM6s6Zp_sv). Callers must also skip lines that start inside an
    open ``/*`` or triple-quoted string so docstring prose is not treated as a
    YAML-style rebind (PRRT_kwDOSJAM6s6ZqPO9). Mid-statement equals-style
    assignments are collected by ``_binding_names_for_line`` /
    ``_inline_assign_binding_names`` (PRRT_kwDOSJAM6s6ZsD5y).
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
                    # Subscript targets normalize quote style inside ``[...]``
                    # (PRRT_kwDOSJAM6s6ZsQFs).
                    if "[" in group:
                        return _normalize_subscript_binding_name(group)
                    requote_non_bare = pattern is not _ASSIGN_BINDING_RE or "=" in match.group(0)
                    return _normalize_assign_binding_name(group, requote_non_bare=requote_non_bare)
            return None
    return None


def _paren_depth(text: str) -> int:
    """Return net ``(`` / ``)`` depth over ``text`` (floored at 0)."""
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
    return depth


def _inline_assign_is_kwarg_or_default(*, before: str, matched: str, name: str) -> bool:
    """True when an equals-style bind is a call kwarg / default, not a rebind.

    ``name=`` after ``(`` / ``,`` inside unmatched parens is a kwarg or default
    (PRRT_kwDOSJAM6s6ZsJyZ). Bare unpacking ``a, name =`` sits at paren-depth 0,
    and ``:=`` is never kwarg syntax, so those stay rebinds
    (PRRT_kwDOSJAM6s6ZsOT0).
    """
    if not _INLINE_ASSIGN_KWARG_BEFORE_RE.search(before):
        return False
    op = matched[len(name) :]
    if ":=" in op:
        return False
    return _paren_depth(before) > 0


def _unpacking_lhs_names_before(before: str) -> tuple[str, ...]:
    """Return prior bare-unpacking targets when ``before`` ends with ``,``."""
    match = _INLINE_UNPACK_LHS_BEFORE_RE.search(before)
    if match is None:
        return ()
    return tuple(part.strip() for part in match.group(1).split(","))


def _inline_assign_binding_names(raw_line: str) -> tuple[str, ...]:
    """Return equals-style assign names found anywhere on ``raw_line``.

    Covers nested overrides the line-start assign anchor misses
    (``if ready: FEATURE_ENABLED = False``, ``x = 1; FEATURE_ENABLED = False``,
    ``if ready: FLAGS["enabled"] = False``; PRRT_kwDOSJAM6s6ZsD5y,
    PRRT_kwDOSJAM6s6ZsQFs). Strings and ``#`` / ``//`` / ``/* … */`` regions are
    blanked via ``_executable_call_scan_text``. Whole-line comments yield no
    names. Bare YAML ``key:`` and typed ``name: T =`` forms are not matched
    mid-line (typed stays statement-leading). Call kwargs, default parameters,
    and the type token in ``name: T =`` are skipped so phantoms cannot enter
    salvage / tip-extra key sets (PRRT_kwDOSJAM6s6ZsJyZ). Bare unpacking and
    parenthesized walrus after ``,`` / ``(`` still bind (PRRT_kwDOSJAM6s6ZsOT0).
    Subscript targets recover their spelling from ``raw_line`` because scan
    blanking turns ``FLAGS["enabled"]`` into ``FLAGS[         ]``.
    """
    stripped = raw_line.lstrip(" \t")
    if stripped.startswith("//") or stripped.startswith("#"):
        return ()
    scan = _executable_call_scan_text(raw_line)
    names: list[str] = []
    for match in _INLINE_ASSIGN_BINDING_RE.finditer(scan):
        before = scan[: match.start()]
        # Recover from ``raw_line``: scan blanks quoted subscript indices to
        # spaces (``FLAGS[         ]``), so group(1) on scan loses the key
        # spelling needed for salvage intersection (PRRT_kwDOSJAM6s6ZsQFs).
        raw_name = raw_line[match.start(1) : match.end(1)]
        name = _normalize_subscript_binding_name(raw_name) if "[" in raw_name else raw_name
        if _inline_assign_is_kwarg_or_default(before=before, matched=match.group(0), name=raw_name):
            continue
        if _INLINE_ASSIGN_TYPE_ANNOTATION_BEFORE_RE.search(before):
            continue
        # Depth-0 comma before the matched target → bare unpacking; include
        # earlier LHS names so ``FEATURE_ENABLED, other =`` supersedes too.
        if (
            _INLINE_ASSIGN_KWARG_BEFORE_RE.search(before)
            and _paren_depth(before) == 0
            and ":=" not in match.group(0)[len(raw_name) :]
        ):
            for prior in _unpacking_lhs_names_before(before):
                if prior not in names:
                    names.append(prior)
        if name not in names:
            names.append(name)
    return tuple(names)


def _binding_names_for_line(raw_line: str) -> tuple[str, ...]:
    """Return all binding names on ``raw_line`` (leading first, then mid-line).

    Statement-leading defs/assigns/YAML keys come from ``_binding_name_for_line``.
    Additional mid-line equals-style assigns are appended so compound tip-extra
    lines still supersede salvage-bound names (PRRT_kwDOSJAM6s6ZsD5y).
    """
    primary = _binding_name_for_line(raw_line)
    inline = _inline_assign_binding_names(raw_line)
    if primary is None:
        return inline
    names: list[str] = [primary]
    for name in inline:
        if name not in names:
            names.append(name)
    return tuple(names)
