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
# ``**=`` is not split as ``*`` + ``*=``. ``==`` and JS/TS ``=>`` are excluded
# via ``=(?![=>])`` so equality and arrow parameters are not treated as
# rebinds (PRRT_kwDOSJAM6s6ZsNCC, PRRT_kwDOSJAM6s6ZtZ_2).
_EQUALS_STYLE_ASSIGN_OP = r"(?:<<=|>>=|\*\*=|//=|[+\-*/%&|^]=|=(?![=>]))"
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
# ``typeset`` / ``readonly`` forms). Typed ``name: T =`` is omitted from the
# regex itself: an optional type span would treat ``if ready: FEATURE_ENABLED =``
# as a typed bind of ``ready``. Instead ``_typed_assign_target_before`` recovers
# the annotated target when the matcher hits the type token ``T``
# (PRRT_kwDOSJAM6s6Zs0s8). Statement-leading typed assigns also stay on
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
# count (PRRT_kwDOSJAM6s6ZsOT0). Optional ``*`` after the comma so starred
# unpack finals (``a, *rest =``) still enter prior-LHS recovery
# (PRRT_kwDOSJAM6s6ZsfLc).
_INLINE_ASSIGN_KWARG_BEFORE_RE = re.compile(r"[(,][ \t]*(?:\*[ \t]*)?$")
# Prior targets in bare ``a, b, name =`` when the last name is kept as an
# unpacking bind (PRRT_kwDOSJAM6s6ZsOT0). Subscript forms
# (``FLAGS["enabled"], other =``) use the scan-shape target so blanked
# indices still match; spelling is recovered from ``raw_line``
# (PRRT_kwDOSJAM6s6ZsYZx).
_UNPACK_LHS_TARGET = (
    rf"(?:{_ASSIGN_SUBSCRIPT_TARGET_SCAN}|"
    r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*)"
)
# Optional ``*`` for starred unpack targets (``*rest``); name extraction still
# uses ``_UNPACK_LHS_TARGET_RE`` so the star is not part of the binding key
# (PRRT_kwDOSJAM6s6ZsfLc).
_UNPACK_LHS_ITEM = rf"(?:\*[ \t]*)?{_UNPACK_LHS_TARGET}"
_UNPACK_LHS_TARGET_RE = re.compile(_UNPACK_LHS_TARGET)
_INLINE_UNPACK_LHS_BEFORE_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_]))"
    rf"(({_UNPACK_LHS_ITEM})"
    rf"(?:[ \t]*,[ \t]*{_UNPACK_LHS_ITEM})*)"
    # Final comma before the matched last target; optional ``*`` when that
    # target is starred (``FEATURE_ENABLED, *rest =``; PRRT_kwDOSJAM6s6ZsfLc).
    r"[ \t]*,[ \t]*(?:\*[ \t]*)?$"
)
# Parenthesized / list unpacking LHS before equals-style assign
# (``(a, b) =`` / ``[a, b] =``). No identifier sits immediately before ``=``,
# so bare comma-before recovery misses every target (PRRT_kwDOSJAM6s6ZsZ5d).
# Bodies are matched with balanced ``()`` / ``[]`` (not a flat item regex) so
# nested targets such as ``(other, (FEATURE_ENABLED, rest)) =`` still bind
# (PRRT_kwDOSJAM6s6ZsnYi). Starred items and trailing commas remain covered
# via target extraction inside the body (PRRT_kwDOSJAM6s6ZsfLc).
_AFTER_PAREN_LIST_UNPACK_ASSIGN_RE = re.compile(rf"[ \t]*{_EQUALS_STYLE_ASSIGN_OP}")
# Include ``{}`` so JS object destructuring ``{a} =`` / ``({a} = …)`` binds like
# paren/list unpacking (PRRT_kwDOSJAM6s6ZtZ_0).
_BRACKET_CLOSE_FOR_OPEN = {"(": ")", "[": "]", "{": "}"}
# Trailing ``target: `` before a matched type token in ``target: T =``. The
# target is recovered so mid-line / nested typed assigns bind ``target`` rather
# than ``T`` (PRRT_kwDOSJAM6s6Zs0s8). Suite headers are excluded via
# ``_SUITE_HEADER_BEFORE_RE`` so ``if ready: name =`` / ``class C: name =`` /
# ``def f() -> T: name =`` still bind ``name`` (PRRT_kwDOSJAM6s6ZsJyZ,
# PRRT_kwDOSJAM6s6ZsD5y, PRRT_kwDOSJAM6s6Zs-so).
_TYPED_ASSIGN_TARGET_BEFORE_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_]))(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*:[ \t]*$"
)
# Full ``before`` span is a suite header (``if ready:`` / ``for x in y:`` /
# ``else:`` / ``class C:`` / ``def f() -> T:`` / ``async def f():``) when the
# keyword reaches the closing colon with no nested ``:``. Nested
# ``if ready: FEATURE_ENABLED:`` / ``class C: FEATURE_ENABLED:`` has an extra
# ``:`` so it is not a suite header and the trailing name is a typed-assign
# target. Omitting ``class`` / ``def`` treated ``class C: name =`` /
# ``def f() -> T: name =`` as typed binds of ``C`` / ``T`` and skipped the
# real target (PRRT_kwDOSJAM6s6Zs-so).
_SUITE_HEADER_BEFORE_RE = re.compile(
    r"(?:^|[;])[ \t]*(?:"
    r"(?:async[ \t]+)?(?:if|elif|while|for|with|match|case|def)\b[^:]*"
    r"|(?:try|else|finally|except|class)\b[^:]*"
    r"):[ \t]*$"
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
# Python ``del`` / JS ``delete`` targets supersede salvage bindings like
# rebinds: neither assign nor call scanners previously recorded the deletion,
# so tip ``del FEATURE_ENABLED`` / ``delete guard.enabled`` kept a
# line-aligned salvage prefix / clean merge-file equality and reused stale
# FIXED evidence (PRRT_kwDOSJAM6s6Zse8m, PRRT_kwDOSJAM6s6ZtiIE). Bare, dotted,
# and subscript targets match assign shapes; comma lists bind each name.
# Requires whitespace or ``(`` after ``del`` / ``delete`` so ``deleted`` is
# not a false hit. Parenthesized ``del(NAME)`` / ``delete(NAME)`` must bind
# too (PRRT_kwDOSJAM6s6ZsmNH). ``del(?:ete)?`` keeps both keywords without
# matching the identifier ``deleted``.
_DEL_TARGET = (
    rf"(?:{_ASSIGN_SUBSCRIPT_TARGET}|"
    r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*)"
)
_DEL_TARGET_SCAN = (
    rf"(?:{_ASSIGN_SUBSCRIPT_TARGET_SCAN}|"
    r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*)"
)
_DEL_TARGET_RE = re.compile(_DEL_TARGET_SCAN)
_INLINE_DEL_BINDING_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_]))del(?:ete)?(?:[ \t]*\([ \t]*|[ \t]+)"
    rf"({_DEL_TARGET_SCAN}(?:[ \t]*,[ \t]*{_DEL_TARGET_SCAN})*)"
    r"[ \t]*\)?"
)
# Shell ``unset`` removes a salvage binding without a rebind or call site the
# same way ``del`` / ``delete`` do; tip ``unset FEATURE_ENABLED`` /
# ``unset -v FEATURE_ENABLED`` kept a line-aligned salvage prefix / clean
# merge-file equality and reused stale FIXED evidence (PRRT_kwDOSJAM6s6ZuRSm).
# Optional short options (``-v`` / ``-f`` / ``-n`` / combinations) and ``--``
# precede one or more bare or quoted shell identifiers; ``unsettable`` is not a
# hit because whitespace (or options) is required after ``unset``. Quoted
# operands (``unset 'FEATURE_ENABLED'`` / ``unset "FEATURE_ENABLED"``) must be
# recovered from ``raw_line``: ``_executable_call_scan_text`` blanks the quoted
# span before a bare-name-only matcher would run (PRRT_kwDOSJAM6s6Zu20N).
_UNSET_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_UNSET_OPERAND = rf"(?:{_UNSET_NAME}|'[^'\n]*'|\"[^\n\"]*\")"
_UNSET_OPERAND_RE = re.compile(_UNSET_OPERAND)
_INLINE_UNSET_BINDING_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_]))unset"
    r"(?:[ \t]+(?:-[A-Za-z]+|--))*"
    r"[ \t]+"
    rf"({_UNSET_OPERAND}(?:[ \t]+{_UNSET_OPERAND})*)"
)
# JS/C/C++ ``++`` / ``--`` update expressions mutate a salvage binding without
# an equals-style rebind or call site, so tip ``retryBudget--`` /
# ``++retryBudget`` kept a line-aligned salvage prefix / clean merge-file
# equality and reused stale FIXED evidence (PRRT_kwDOSJAM6s6Zs-Rb). Bare,
# dotted, and subscript targets match assign shapes. Lookbehind excludes a
# preceding ``+`` / ``-`` so ``x+++y`` does not invent a prefix update of ``y``.
_UPDATE_TARGET_SCAN = (
    rf"(?:{_ASSIGN_SUBSCRIPT_TARGET_SCAN}|"
    r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*)"
)
_INLINE_UPDATE_POSTFIX_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_]))"
    rf"({_UPDATE_TARGET_SCAN})"
    r"(?:\+\+|--)"
)
_INLINE_UPDATE_PREFIX_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_+-]))"
    r"(?:\+\+|--)"
    rf"({_UPDATE_TARGET_SCAN})"
)
# Python ``setattr`` / ``delattr`` (and ``object.__setattr__`` /
# ``receiver.__setattr__`` / ``__delattr__``) mutate an attribute without an
# equals-style binding or a call name that intersects salvage ``obj.attr``;
# tip ``setattr(guard, "enabled", False)`` kept a line-aligned salvage prefix /
# clean merge-file equality and reused stale FIXED evidence
# (PRRT_kwDOSJAM6s6Zu8Kn). Free-function form takes ``(obj, "attr", …)``;
# method form takes ``obj.__setattr__("attr", …)``. Match on ``raw_line`` so
# quoted attr names survive ``_executable_call_scan_text`` blanking, then drop
# hits whose helper keyword was blanked (comment / string / ``/* … */``).
_SETATTR_OBJ = r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
_SETATTR_ATTR_LIT = r"(?:\"([^\"\n]+)\"|'([^'\n]+)')"
_SETATTR_HELPER = r"(?:setattr|delattr|__setattr__|__delattr__)"
_INLINE_SETATTR_FREE_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_]))"
    rf"(?:[A-Za-z_][A-Za-z0-9_]*\.)*{_SETATTR_HELPER}"
    rf"[ \t]*\([ \t]*({_SETATTR_OBJ})[ \t]*,[ \t]*{_SETATTR_ATTR_LIT}"
)
_INLINE_SETATTR_METHOD_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_]))"
    rf"({_SETATTR_OBJ})\.(?:__setattr__|__delattr__)"
    rf"[ \t]*\([ \t]*{_SETATTR_ATTR_LIT}"
)
# Mapping item mutations (``FLAGS.__setitem__("enabled", False)`` /
# ``dict.__setitem__(FLAGS, "enabled", False)`` / ``FLAGS.__delitem__("enabled")``)
# leave no assign binding and only call names ``FLAGS`` / ``FLAGS.__setitem__``,
# which do not match salvaged ``FLAGS["enabled"]`` (PRRT_kwDOSJAM6s6ZwrnH).
_SETITEM_HELPER = r"(?:__setitem__|__delitem__)"
_INLINE_SETITEM_FREE_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_]))"
    rf"(?:[A-Za-z_][A-Za-z0-9_]*\.)*{_SETITEM_HELPER}"
    rf"[ \t]*\([ \t]*({_SETATTR_OBJ})[ \t]*,[ \t]*{_SETATTR_ATTR_LIT}"
)
_INLINE_SETITEM_METHOD_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_]))"
    rf"({_SETATTR_OBJ})\.(?:__setitem__|__delitem__)"
    rf"[ \t]*\([ \t]*{_SETATTR_ATTR_LIT}"
)
# ``FLAGS.update(enabled=False)`` / ``FLAGS.update({"enabled": False})`` synthesize
# ``FLAGS["enabled"]``; opaque ``FLAGS.update(other)`` is handled by receiver
# fail-closed in the tip-extra call path (PRRT_kwDOSJAM6s6ZwrnH).
_INLINE_UPDATE_CALL_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_]))"
    rf"({_SETATTR_OBJ})\.update[ \t]*\("
)
_UPDATE_KWARG_RE = re.compile(r"(?:^|[,])[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*=")
_UPDATE_DICT_KEY_RE = re.compile(r"(?:^|[{,])[ \t]*(?:\"([^\"\n]+)\"|'([^'\n]+)')[ \t]*:")


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
    if (
        pos != length or not segments
    ):  # pragma: no cover - defensive; loop exits only at EOS with segments
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
            return None  # pragma: no cover - binding regexes always capture a name group
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


def _typed_assign_target_before(before: str) -> str | None:
    """Return annotated target when ``before`` ends with ``target: `` for ``target: T =``.

    Distinguishes typed ``FEATURE_ENABLED: bool =`` (and nested
    ``if ready: FEATURE_ENABLED: bool =``) from suite headers
    ``if ready: FEATURE_ENABLED =`` / ``for x in y: name =`` /
    ``class C: name =`` / ``def f() -> T: name =``, where the trailing
    ``ident:`` closes the suite and must not mark the following assign as a
    type token (PRRT_kwDOSJAM6s6Zs0s8, PRRT_kwDOSJAM6s6ZsD5y,
    PRRT_kwDOSJAM6s6ZsJyZ, PRRT_kwDOSJAM6s6Zs-so).
    """
    if _SUITE_HEADER_BEFORE_RE.search(before):
        return None
    match = _TYPED_ASSIGN_TARGET_BEFORE_RE.search(before)
    if match is None:
        return None
    return match.group(1)


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


def _unpacking_lhs_names_before(before: str, *, raw_before: str) -> tuple[str, ...]:
    """Return prior unpacking targets when ``before`` ends with ``,``.

    ``before`` is executable-scan text (quoted subscript indices blanked);
    ``raw_before`` is the same span of the original line so
    ``FLAGS["enabled"], other =`` recovers the real key spelling
    (PRRT_kwDOSJAM6s6ZsYZx).
    """
    match = _INLINE_UNPACK_LHS_BEFORE_RE.search(before)
    if match is None:
        return ()
    names: list[str] = []
    for part_match in _UNPACK_LHS_TARGET_RE.finditer(before, match.start(1), match.end(1)):
        raw_name = raw_before[part_match.start() : part_match.end()]
        name = _normalize_subscript_binding_name(raw_name) if "[" in raw_name else raw_name
        names.append(name)
    return tuple(names)


def _matching_bracket_closer_index(text: str, start: int) -> int | None:
    """Return index of the closer matching ``text[start]`` (``(`` / ``[`` / ``{``), or None.

    ``()``, ``[]``, and ``{}`` may nest interchangeably so ``(a, [b, c]) =`` /
    ``({a} = …)`` still resolve (PRRT_kwDOSJAM6s6ZsnYi, PRRT_kwDOSJAM6s6ZtZ_0).
    ``text`` is executable-scan output where strings are already blanked.
    """
    opener = text[start]
    closer = _BRACKET_CLOSE_FOR_OPEN.get(opener)
    if closer is None:
        return None
    stack = [closer]
    for idx in range(start + 1, len(text)):
        ch = text[idx]
        nested = _BRACKET_CLOSE_FOR_OPEN.get(ch)
        if nested is not None:
            stack.append(nested)
            continue
        if ch in ")]}":
            if not stack or ch != stack[-1]:
                return None
            stack.pop()
            if not stack:
                return idx
    return None


def _object_pattern_ident_is_property_key(scan: str, end: int, body_end: int) -> bool:
    """True when the ident ending at ``end`` is a ``key:`` property in ``{…}``.

    JS ``{FEATURE_ENABLED: local} =`` assigns only ``local``; treating the key
    as a binding falsely supersedes salvage of that name (PRRT_kwDOSJAM6s6Zv4pl).
    """
    j = end
    while j < body_end and scan[j] in " \t":
        j += 1
    return j < body_end and scan[j] == ":"


def _paren_list_unpack_binding_names(raw_line: str, *, scan: str) -> tuple[str, ...]:
    """Return targets from ``(a, b) =`` / ``[a, b] =`` / ``{a} =`` forms on the line.

    Parenthesized, list, and JS object-destructuring unpacking place ``)`` /
    ``]`` / ``}`` immediately before ``=``, so identifier-before-assign
    patterns find nothing and would keep stale FIXED salvage
    (PRRT_kwDOSJAM6s6ZsZ5d, PRRT_kwDOSJAM6s6ZtZ_0). Bodies are scanned with
    balanced brackets so nested ``(other, (FEATURE_ENABLED, rest)) =`` /
    ``[other, [FEATURE_ENABLED, rest]] =`` / ``({FEATURE_ENABLED} = …)`` still
    bind (PRRT_kwDOSJAM6s6ZsnYi). Object-pattern alias properties record only
    the value-side target so ``{FEATURE_ENABLED: local}`` binds ``local`` and
    not the key (PRRT_kwDOSJAM6s6Zv4pl). Starred items and trailing commas are
    included so ``(a, *rest) =`` / ``(a, b,) =`` still bind
    (PRRT_kwDOSJAM6s6ZsfLc). Subscript spellings are recovered from
    ``raw_line`` because scan blanks quoted indices.
    """
    names: list[str] = []
    idx = 0
    length = len(scan)
    while idx < length:
        ch = scan[idx]
        if ch not in "([{":
            idx += 1
            continue
        # Require a non-identifier boundary before the opener so ``foo(a) =`` /
        # ``FLAGS[0] =`` are not treated as unpack LHS.
        if idx > 0 and (scan[idx - 1].isalnum() or scan[idx - 1] == "_"):
            idx += 1
            continue
        close = _matching_bracket_closer_index(scan, idx)
        if close is None:
            idx += 1
            continue
        if _AFTER_PAREN_LIST_UNPACK_ASSIGN_RE.match(scan, close + 1) is None:
            idx += 1
            continue
        body_start, body_end = idx + 1, close
        is_object_pattern = ch == "{"
        for part_match in _UNPACK_LHS_TARGET_RE.finditer(scan, body_start, body_end):
            if is_object_pattern and _object_pattern_ident_is_property_key(
                scan, part_match.end(), body_end
            ):
                continue
            raw_name = raw_line[part_match.start() : part_match.end()]
            name = _normalize_subscript_binding_name(raw_name) if "[" in raw_name else raw_name
            if name not in names:
                names.append(name)
        idx = close + 1
    return tuple(names)


def _inline_assign_binding_names(raw_line: str) -> tuple[str, ...]:
    """Return equals-style assign names found anywhere on ``raw_line``.

    Covers nested overrides the line-start assign anchor misses
    (``if ready: FEATURE_ENABLED = False``, ``x = 1; FEATURE_ENABLED = False``,
    ``if ready: FLAGS["enabled"] = False``; PRRT_kwDOSJAM6s6ZsD5y,
    PRRT_kwDOSJAM6s6ZsQFs). Strings and ``#`` / ``//`` / ``/* … */`` regions are
    blanked via ``_executable_call_scan_text``. Whole-line comments yield no
    names. Bare YAML ``key:`` is not matched mid-line. Typed ``name: T =`` is
    recovered via ``_typed_assign_target_before`` when the matcher hits ``T``
    so nested ``if ready: FEATURE_ENABLED: bool = False`` binds
    ``FEATURE_ENABLED`` rather than ``bool`` (PRRT_kwDOSJAM6s6Zs0s8). Call
    kwargs, default parameters, and type tokens are skipped so phantoms cannot
    enter salvage / tip-extra key sets (PRRT_kwDOSJAM6s6ZsJyZ). Bare unpacking
    and parenthesized walrus after ``,`` / ``(`` still bind
    (PRRT_kwDOSJAM6s6ZsOT0). Parenthesized / list / object-destructuring
    unpacking ``(a, b) =`` / ``[a, b] =`` / ``{a} =`` bind too, including
    nested targets (PRRT_kwDOSJAM6s6ZsZ5d, PRRT_kwDOSJAM6s6ZsnYi,
    PRRT_kwDOSJAM6s6ZtZ_0). Subscript targets recover their spelling from
    ``raw_line`` because scan blanking turns ``FLAGS["enabled"]`` into
    ``FLAGS[         ]``.
    """
    stripped = raw_line.lstrip(" \t")
    if stripped.startswith("//") or stripped.startswith("#"):
        return ()
    scan = _executable_call_scan_text(raw_line)
    names: list[str] = list(_paren_list_unpack_binding_names(raw_line, scan=scan))
    for match in _INLINE_ASSIGN_BINDING_RE.finditer(scan):
        before = scan[: match.start()]
        # Recover from ``raw_line``: scan blanks quoted subscript indices to
        # spaces (``FLAGS[         ]``), so group(1) on scan loses the key
        # spelling needed for salvage intersection (PRRT_kwDOSJAM6s6ZsQFs).
        raw_name = raw_line[match.start(1) : match.end(1)]
        name = _normalize_subscript_binding_name(raw_name) if "[" in raw_name else raw_name
        if _inline_assign_is_kwarg_or_default(before=before, matched=match.group(0), name=raw_name):
            continue
        typed_target = _typed_assign_target_before(before)
        if typed_target is not None:
            # Matched name is the type token ``T`` in ``target: T =``; bind the
            # annotated target instead (PRRT_kwDOSJAM6s6Zs0s8).
            if typed_target not in names:
                names.append(typed_target)
            continue
        # Depth-0 comma before the matched target → bare unpacking; include
        # earlier LHS names so ``FEATURE_ENABLED, other =`` / subscript
        # ``FLAGS["enabled"], other =`` supersede too (PRRT_kwDOSJAM6s6ZsOT0,
        # PRRT_kwDOSJAM6s6ZsYZx).
        if (
            _INLINE_ASSIGN_KWARG_BEFORE_RE.search(before)
            and _paren_depth(before) == 0
            and ":=" not in match.group(0)[len(raw_name) :]
        ):
            raw_before = raw_line[: match.start()]
            for prior in _unpacking_lhs_names_before(before, raw_before=raw_before):
                if prior not in names:
                    names.append(prior)
        if name not in names:
            names.append(name)
    return tuple(names)


def _del_binding_names(raw_line: str) -> tuple[str, ...]:
    """Return names deleted by ``del`` / ``delete`` on ``raw_line``.

    Tip-extra ``del FEATURE_ENABLED`` / ``if ready: del FEATURE_ENABLED`` /
    ``del FLAGS["enabled"]`` / ``del(FEATURE_ENABLED)`` / ``delete
    guard.enabled`` / ``delete(guard.enabled)`` must supersede salvage of
    those bindings; assign and call scanners alone leave the salvage retained
    (PRRT_kwDOSJAM6s6Zse8m, PRRT_kwDOSJAM6s6ZsmNH, PRRT_kwDOSJAM6s6ZtiIE).
    Strings and ``#`` / ``//`` / ``/* … */`` regions are blanked via
    ``_executable_call_scan_text``. Subscript spellings recover from
    ``raw_line`` because scan blanks quoted indices.
    """
    stripped = raw_line.lstrip(" \t")
    if stripped.startswith("//") or stripped.startswith("#"):
        return ()
    scan = _executable_call_scan_text(raw_line)
    names: list[str] = []
    for match in _INLINE_DEL_BINDING_RE.finditer(scan):
        for part_match in _DEL_TARGET_RE.finditer(scan, match.start(1), match.end(1)):
            raw_name = raw_line[part_match.start() : part_match.end()]
            name = _normalize_subscript_binding_name(raw_name) if "[" in raw_name else raw_name
            if name not in names:
                names.append(name)
    return tuple(names)


def _unset_binding_names(raw_line: str) -> tuple[str, ...]:
    """Return names removed by shell ``unset`` on ``raw_line``.

    Tip-extra ``unset FEATURE_ENABLED`` / ``unset -v FEATURE_ENABLED`` /
    ``unset -- FEATURE_ENABLED`` / ``unset 'FEATURE_ENABLED'`` /
    ``unset "FEATURE_ENABLED"`` / ``if true; then unset FEATURE_ENABLED; fi``
    must supersede salvage of those bindings; assign, ``del`` / ``delete``, and
    call scanners alone leave the salvage retained (PRRT_kwDOSJAM6s6ZuRSm,
    PRRT_kwDOSJAM6s6Zu20N). Match operands on ``raw_line`` so quoted literals
    remain visible, then drop hits whose ``unset`` keyword was blanked by
    ``_executable_call_scan_text`` (comment / string / ``/* … */`` regions).
    """
    stripped = raw_line.lstrip(" \t")
    if stripped.startswith("//") or stripped.startswith("#"):
        return ()
    scan = _executable_call_scan_text(raw_line)
    names: list[str] = []
    for match in _INLINE_UNSET_BINDING_RE.finditer(raw_line):
        # ``unset`` starts at match.start(); skip when that span is non-executable.
        if scan[match.start() : match.start() + 5] != "unset":
            continue
        for part_match in _UNSET_OPERAND_RE.finditer(raw_line, match.start(1), match.end(1)):
            token = part_match.group(0)
            name = token[1:-1] if token[:1] in "'\"" else token
            if name and name not in names:
                names.append(name)
    return tuple(names)


def _update_expr_binding_names(raw_line: str) -> tuple[str, ...]:
    """Return names mutated by ``++`` / ``--`` on ``raw_line``.

    Tip-extra ``retryBudget--`` / ``++retryBudget`` / ``if (ready) obj.count++`` /
    ``FLAGS["enabled"]++`` must supersede salvage of those bindings; assign and
    call scanners alone leave the salvage retained (PRRT_kwDOSJAM6s6Zs-Rb).
    Strings and ``#`` / ``//`` / ``/* … */`` regions are blanked via
    ``_executable_call_scan_text``. Subscript spellings recover from
    ``raw_line`` because scan blanks quoted indices.
    """
    stripped = raw_line.lstrip(" \t")
    if stripped.startswith("//") or stripped.startswith("#"):
        return ()
    scan = _executable_call_scan_text(raw_line)
    names: list[str] = []
    for pattern in (_INLINE_UPDATE_POSTFIX_RE, _INLINE_UPDATE_PREFIX_RE):
        for match in pattern.finditer(scan):
            raw_name = raw_line[match.start(1) : match.end(1)]
            name = _normalize_subscript_binding_name(raw_name) if "[" in raw_name else raw_name
            if name not in names:
                names.append(name)
    return tuple(names)


def _setattr_helper_executable(*, raw_line: str, scan: str, match_start: int) -> bool:
    """True when the setattr/delattr helper keyword at ``match_start`` is executable."""
    open_paren = raw_line.find("(", match_start)
    if open_paren < 0:
        return False
    helper_scan = scan[match_start:open_paren]
    return any(
        token in helper_scan for token in ("setattr", "delattr", "__setattr__", "__delattr__")
    )


def _helper_keyword_executable(
    *, raw_line: str, scan: str, match_start: int, tokens: tuple[str, ...]
) -> bool:
    """True when any of ``tokens`` remains executable in ``scan`` before ``(``."""
    open_paren = raw_line.find("(", match_start)
    if open_paren < 0:
        return False
    helper_scan = scan[match_start:open_paren]
    return any(token in helper_scan for token in tokens)


def _setattr_mutation_binding_names(raw_line: str) -> tuple[str, ...]:
    """Return ``obj.attr`` keys mutated by setattr/delattr helpers on ``raw_line``.

    Tip-extra ``setattr(guard, "enabled", False)`` / ``delattr(guard, "enabled")`` /
    ``object.__setattr__(guard, "enabled", False)`` /
    ``guard.__setattr__("enabled", False)`` / ``guard.__delattr__("enabled")``
    must supersede salvage of ``guard.enabled``; assign and call scanners alone
    leave the salvage retained (PRRT_kwDOSJAM6s6Zu8Kn). Match operands on
    ``raw_line`` so quoted attr literals remain visible, then drop hits whose
    helper keyword was blanked by ``_executable_call_scan_text``.
    """
    stripped = raw_line.lstrip(" \t")
    if stripped.startswith("//") or stripped.startswith("#"):
        return ()
    scan = _executable_call_scan_text(raw_line)
    names: list[str] = []

    def _attr_from_groups(match: re.Match[str], double_idx: int, single_idx: int) -> str | None:
        attr = match.group(double_idx) or match.group(single_idx)
        return attr if attr else None

    for match in _INLINE_SETATTR_FREE_RE.finditer(raw_line):
        if not _setattr_helper_executable(raw_line=raw_line, scan=scan, match_start=match.start()):
            continue
        attr = _attr_from_groups(match, 2, 3)
        if not attr:
            continue
        key = f"{match.group(1)}.{attr}"
        if key not in names:
            names.append(key)
    for match in _INLINE_SETATTR_METHOD_RE.finditer(raw_line):
        if not _setattr_helper_executable(raw_line=raw_line, scan=scan, match_start=match.start()):
            continue
        attr = _attr_from_groups(match, 2, 3)
        if not attr:
            continue
        key = f"{match.group(1)}.{attr}"
        if key not in names:
            names.append(key)
    return tuple(names)


def _setitem_mutation_binding_names(raw_line: str) -> tuple[str, ...]:
    """Return ``obj["key"]`` keys mutated by ``__setitem__`` / ``__delitem__``.

    Tip-extra ``FLAGS.__setitem__("enabled", False)`` /
    ``dict.__setitem__(FLAGS, "enabled", False)`` /
    ``FLAGS.__delitem__("enabled")`` must supersede salvage of
    ``FLAGS["enabled"]``; assign and call scanners alone leave the salvage
    retained (PRRT_kwDOSJAM6s6ZwrnH).
    """
    stripped = raw_line.lstrip(" \t")
    if stripped.startswith("//") or stripped.startswith("#"):
        return ()
    scan = _executable_call_scan_text(raw_line)
    names: list[str] = []
    tokens = ("__setitem__", "__delitem__")

    def _key_from_groups(match: re.Match[str], double_idx: int, single_idx: int) -> str | None:
        key = match.group(double_idx) or match.group(single_idx)
        return key if key else None

    def _append_subscript(receiver: str, key: str) -> None:
        binding = _normalize_subscript_binding_name(f'{receiver}["{key}"]')
        if binding not in names:
            names.append(binding)

    for match in _INLINE_SETITEM_FREE_RE.finditer(raw_line):
        if not _helper_keyword_executable(
            raw_line=raw_line, scan=scan, match_start=match.start(), tokens=tokens
        ):
            continue
        key = _key_from_groups(match, 2, 3)
        if not key:
            continue
        _append_subscript(match.group(1), key)
    for match in _INLINE_SETITEM_METHOD_RE.finditer(raw_line):
        if not _helper_keyword_executable(
            raw_line=raw_line, scan=scan, match_start=match.start(), tokens=tokens
        ):
            continue
        key = _key_from_groups(match, 2, 3)
        if not key:
            continue
        _append_subscript(match.group(1), key)
    return tuple(names)


def _update_call_argument_span(raw_line: str, open_paren: int) -> str | None:
    """Return the inside of a balanced ``(…)`` starting at ``open_paren``, or None."""
    depth = 0
    for idx in range(open_paren, len(raw_line)):
        ch = raw_line[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return raw_line[open_paren + 1 : idx]
    return None


def _update_mutation_binding_names(raw_line: str) -> tuple[str, ...]:
    """Return ``obj["key"]`` keys from ``obj.update(key=…)`` / dict-literal forms.

    Keyword and simple ``{"key": …}`` arguments synthesize subscript bindings so
    tip-extra ``FLAGS.update(enabled=False)`` supersedes salvaged
    ``FLAGS["enabled"]`` (PRRT_kwDOSJAM6s6ZwrnH). Opaque ``update(other)`` yields
    no keys here; tip-extra call fail-closed covers that case.
    """
    stripped = raw_line.lstrip(" \t")
    if stripped.startswith("//") or stripped.startswith("#"):
        return ()
    scan = _executable_call_scan_text(raw_line)
    names: list[str] = []
    for match in _INLINE_UPDATE_CALL_RE.finditer(raw_line):
        if not _helper_keyword_executable(
            raw_line=raw_line,
            scan=scan,
            match_start=match.start(),
            tokens=("update",),
        ):
            continue
        open_paren = raw_line.find("(", match.start())
        if open_paren < 0:
            continue
        args = _update_call_argument_span(raw_line, open_paren)
        if args is None:
            continue
        receiver = match.group(1)
        for kw_match in _UPDATE_KWARG_RE.finditer(args):
            binding = _normalize_subscript_binding_name(f'{receiver}["{kw_match.group(1)}"]')
            if binding not in names:
                names.append(binding)
        for dict_match in _UPDATE_DICT_KEY_RE.finditer(args):
            key = dict_match.group(1) or dict_match.group(2)
            if not key:
                continue
            binding = _normalize_subscript_binding_name(f'{receiver}["{key}"]')
            if binding not in names:
                names.append(binding)
    return tuple(names)


def _binding_names_for_line(raw_line: str) -> tuple[str, ...]:
    """Return all binding names on ``raw_line`` (leading first, then mid-line).

    Statement-leading defs/assigns/YAML keys come from ``_binding_name_for_line``.
    Additional mid-line equals-style assigns are appended so compound tip-extra
    lines still supersede salvage-bound names (PRRT_kwDOSJAM6s6ZsD5y). Python
    ``del`` and JS ``delete`` targets are included so deletions supersede
    without a rebind (PRRT_kwDOSJAM6s6Zse8m, PRRT_kwDOSJAM6s6ZtiIE). Shell
    ``unset`` targets (including ``unset -v NAME`` and quoted
    ``unset 'NAME'`` / ``unset "NAME"``) are included the same way
    (PRRT_kwDOSJAM6s6ZuRSm, PRRT_kwDOSJAM6s6Zu20N). JS/C/C++ ``++`` / ``--``
    update targets are included so increment/decrement supersedes without an
    equals-style rebind (PRRT_kwDOSJAM6s6Zs-Rb). ``setattr`` / ``delattr`` /
    ``__setattr__`` / ``__delattr__`` targets are included so indirect attribute
    mutations supersede without a rebind or intersecting call name
    (PRRT_kwDOSJAM6s6Zu8Kn). ``__setitem__`` / ``__delitem__`` / ``update``
    targets synthesize ``obj["key"]`` the same way (PRRT_kwDOSJAM6s6ZwrnH).
    """
    primary = _binding_name_for_line(raw_line)
    inline = _inline_assign_binding_names(raw_line)
    deleted = _del_binding_names(raw_line)
    unset = _unset_binding_names(raw_line)
    updated = _update_expr_binding_names(raw_line)
    mutated = _setattr_mutation_binding_names(raw_line)
    setitem = _setitem_mutation_binding_names(raw_line)
    update_call = _update_mutation_binding_names(raw_line)
    if (
        primary is None
        and not inline
        and not deleted
        and not unset
        and not updated
        and not mutated
        and not setitem
        and not update_call
    ):
        return ()
    names: list[str] = []
    if primary is not None:
        names.append(primary)
    for name in (*inline, *deleted, *unset, *updated, *mutated, *setitem, *update_call):
        if name not in names:
            names.append(name)
    return tuple(names)
