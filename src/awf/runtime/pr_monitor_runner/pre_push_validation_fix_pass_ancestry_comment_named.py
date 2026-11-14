"""Comment-named target FIXED evidence (issue #1232).

The four earlier correction-attempt evidence gates (see
``comment_verdict_correction``) are all derived from the comment *anchor*.
Reviewers often anchor at a usage site and name the place to fix in the comment
*body* instead — a runbook line, the route helper's file, the tuple a filter
reads — so a genuine fix there escalated to ``needs_human`` three times on
2026-11-13.

This module adds the fifth gate. Over the item's own commit range it accepts:

* **(a)** a change to a repo-relative path the reviewer's comment names
  explicitly. With ``:line``, the range must touch that line under the existing
  line-anchored rule (``_commit_range_touches_path``); without one, any change
  to the file counts.
* **(b)** a change to the *definition* of an identifier the reviewer's comment
  names in single backticks (``name`` / ``name()``): a module-reachable head the
  callee check already resolves, or a module-level plain assignment. A
  definition the range *adds* — in an existing file or a new one — counts too.

Only the reviewer's text is read (``reviewer_comment_text``): AWF-authored
replies, which can carry the agent's own reason (#1018), and the agent's
verdict reason are never evidence. Absolute paths, ``..`` / ``.`` segments,
``~``, globs and URLs never count, and every unreadable Git read fails closed.
Kept in its own module because ``..._cross_file`` sits near the first-party line
budget, and because the rule reads as one unit.
"""

from __future__ import annotations

import keyword
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_callees import (
    _CALLEE_KEYWORD_BLOCKLIST,
    _JS_IDENT,
    _definition_binding_scope,
    _definition_head_scan_lines,
    _is_ignorable_span_gap_line,
    _iter_definition_spans,
    _leading_indent,
    _mask_comments_and_string_literals_for_callee_scan,
    _path_is_jsx,
    _simple_statement_segments,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file import (
    _JS_TS_CALL_SITE_SUFFIXES,
    _MAX_CALLEE_EVIDENCE_CANDIDATE_PATHS,
    _PYTHON_CALL_SITE_SUFFIXES,
    _diff_adds_decorators_above_span,
    _importable_definition_spans_for_names,
    _path_diff_text_in_commit_range,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_rebinding import (
    _rebound_scope_names,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from awf.runtime.pr_monitor import ReviewThread

# Upper bound on the ``:line`` probes one comment can trigger. Each runs the
# line-anchored rule's Git reads, and a review names one to three targets.
_MAX_COMMENT_NAMED_PATHS = 10
# Suffixes whose definition heads the identifier gate (b) can read.
_DEFINITION_SUFFIXES = _PYTHON_CALL_SITE_SUFFIXES | _JS_TS_CALL_SITE_SUFFIXES
# Suffixes whose expressions can carry type arguments (``Array<number>``).
_TS_SUFFIXES = frozenset({".ts", ".tsx", ".mts", ".cts"})

# ``scheme://...`` links are dropped before path parsing, so a GitHub permalink
# never contributes a path (its line fragment is a non-goal).
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S*")
# Tokens are split on whitespace and quoting/bracketing characters that wrap a
# path in prose or markdown links. ``[]{}*?`` stay inside a token so a glob is
# rejected whole rather than split into an innocent-looking fragment.
_PATH_TOKEN_SPLIT_RE = re.compile(r"[\s`'\"()<>]+")
_PATH_TRAILING_PUNCTUATION = ".,;:!?"
_NAMED_PATH_RE = re.compile(r"((?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]+)(?::(\d+)(?:-\d+)?)?")
_FENCED_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
_NAMED_IDENTIFIER_RE = re.compile(rf"({_JS_IDENT})(?:\(\))?")
_NON_DEFINITION_NAMES = (
    _CALLEE_KEYWORD_BLOCKLIST
    | frozenset(keyword.kwlist)
    | frozenset({"self", "cls", "this", "null", "undefined"})
)
# A module-level plain binding: ``name = ...``, ``name: T = ...``,
# ``const|let|var name = ...`` and ``export const name = ...``. Matched on masked
# scan lines at column 0, so text in strings/docstrings and class or function
# bodies never counts; ``==`` / ``+=`` are not bindings. A JS/TS statement may be
# indented at module scope; its blocks are bracketed, so a binding inside one
# never opens a statement (PRRT_kwDOSJAM6s63P5Zi). A standalone arrow's ``=>``
# binds no parameter at module scope (PRRT_kwDOSJAM6s63V0Yl). An annotation's
# function type (``name: (r: R) => S = ...``) consumes its ``=>`` before the
# binding's ``=`` (PRRT_kwDOSJAM6s63WYf0). The annotation (group 2 is its ``:``)
# ends at its own top-level ``=``, which ``_binding_match`` locates: a generic
# default's ``=`` or a later declarator's never initializes the name
# (PRRT_kwDOSJAM6s63WhGd).
_TYPE_ANNOTATION_PATTERN = r"(?:[ \t]*(:)[^\n]*)?"
_MODULE_LEVEL_ASSIGNMENT_RE = re.compile(
    rf"^(?:export[ \t]+)?(?:(?:const|let|var)[ \t]+)?({_JS_IDENT}){_TYPE_ANNOTATION_PATTERN}[ \t]*=(?![=>])"
)
# A further target of a chained binding (``A = B = 10``), matched right after
# the previous target's ``=`` (PRRT_kwDOSJAM6s63PKQ9). An arrow's ``=>`` binds
# no parameter at module scope (PRRT_kwDOSJAM6s63VYOS).
_CHAINED_ASSIGNMENT_TARGET_RE = re.compile(rf"[ \t]*({_JS_IDENT})[ \t]*=(?![=>])")
# A Python logical line whose ``;``-separated later statements bind nothing at
# module scope: an indented one, or a compound head whose body follows its ``:``
# (PRRT_kwDOSJAM6s63Sh3G).
_PYTHON_INLINE_BODY_RE = re.compile(
    r"[ \t]|(?:if|elif|else|for|while|with|try|except|finally|class|def|async)(?![\w])"
)
# A JS/TS declaration binds each declarator of ``const A = 0, B = v``; a further
# one is matched right after a top-level ``,`` (PRRT_kwDOSJAM6s63QfAU).
_JS_TS_DECLARATION_RE = re.compile(r"^(?:export[ \t]+)?(?:const|let|var)[ \t]")
_JS_TS_DECLARATOR_RE = re.compile(rf"[ \t]*({_JS_IDENT}){_TYPE_ANNOTATION_PATTERN}[ \t]*=(?![=>])")
# What precedes a type-parameter list's ``<``: the start of an expression (a
# generic arrow or function type), a ``function``/``class`` head or a construct
# signature's ``new`` (PRRT_kwDOSJAM6s63U908). A ``<`` after an operand
# (``1 < 2``) is a comparison (PRRT_kwDOSJAM6s63UyJU), including one merely
# prefixed by a keyword (``className < 2``) (PRRT_kwDOSJAM6s63U903).
_JS_TS_TYPE_PARAMETER_LIST_OPENER_RE = re.compile(
    r"(?:^|[=(,:?\[{]|=>|(?<![\w$.])(?:async|new"
    r"|(?:function|class)(?![\w$])(?:[ \t]*\*)?(?:[ \t]*[\w$]+)?))"
    r"[ \t]*$"
)
# What follows a type-parameter list's ``>`` (PRRT_kwDOSJAM6s63SGuR).
_JS_TS_TYPE_PARAMETER_LIST_FOLLOWER_RE = re.compile(
    r"[ \t]*(?:[({]|(?:extends|implements)(?![\w$]))"
)
# Indentation is not scope in JS/TS: a statement ends where the code does, unless
# its last token or the next line's first token continues the expression
# (ASI's rules, approximately) (PRRT_kwDOSJAM6s63PU_-). A leading ``++``/``--``
# after a line break is a new prefix-update statement, not a continuation
# (PRRT_kwDOSJAM6s63QfAQ). A leading keyword operator (``instanceof``/``in``)
# cannot start a statement, so it continues the expression (PRRT_kwDOSJAM6s63Qv0F).
# A trailing keyword operator (``await``/``typeof``/``new``/...) still needs its
# operand; a property of that name (``cache.delete``) does not (PRRT_kwDOSJAM6s63SGuU).
# So does a trailing unary ``!``; a TS non-null assertion (``value!``) follows its
# operand and does not (PRRT_kwDOSJAM6s63Sh3A). So does a trailing TS type
# operator (``satisfies``/``as``), which still needs its type (PRRT_kwDOSJAM6s63SzHv).
# A trailing reserved ``extends`` awaits its operand, such as the extends type
# of a conditional type after ``as`` (PRRT_kwDOSJAM6s63X1Cv).
_JS_TS_TRAILING_CONTINUATION_RE = re.compile(
    r"(?:[=*/%&|^<>?:,.~]|(?<!\+)\+|(?<!-)-|(?:^|[^\w$)\]\s!])[ \t]*!+"
    r"|(?<![\w$.])(?:await|typeof|new|void|delete|instanceof|in|satisfies|as|extends))$"
)
# A trailing TS type operator that still needs its type operand (``keyof``,
# ``readonly``) right after an ``as``/``satisfies`` type operator
# (PRRT_kwDOSJAM6s63XaW6). Elsewhere it may be a contextual identifier.
_JS_TS_TRAILING_TYPE_PREFIX_RE = re.compile(r"(?<![\w$.])(?:keyof|readonly)$")
_JS_TS_TRAILING_TYPE_OPERATOR_RE = re.compile(r"(?<![\w$.])(?:satisfies|as)$")
# A type name right after an ``as``/``satisfies`` type operator, whose spaced or
# commented ``<`` still opens type arguments (``value as Array <number>``)
# (PRRT_kwDOSJAM6s63XmEA).
_JS_TS_ASSERTED_TYPE_NAME_RE = re.compile(
    r"(?<![\w$.])(?:satisfies|as)[ \t]+(?:(?:keyof|readonly)[ \t]+)*"
    r"[\w$]+(?:[ \t]*\.[ \t]*[\w$]+)*[ \t]*$"
)
# The ``>`` ending a completed JSX element (``<Widget />``, ``</div>``, ``</>``)
# closes an operand rather than opening a comparison (PRRT_kwDOSJAM6s63VJ_g).
_JSX_COMPLETED_ELEMENT_RE = re.compile(r"(?:/|</[\w$.:-]*[ \t]*)>$")
# A JSX opening tag's ``<`` (``<Widget``, ``<>``) follows the start of an
# expression, an operator or an enclosing tag's ``>``, never an operand: ``a <b``
# is a comparison. Until the tag's own ``>``, its statement stays open
# (PRRT_kwDOSJAM6s63Wpyz).
_JSX_OPENING_TAG_RE = re.compile(r"<[A-Za-z_$>]")
_JSX_OPENING_TAG_OPENER_RE = re.compile(
    r"(?:^|[=(,:?\[{&|>]|(?<![\w$.])(?:return|yield|await))[ \t]*$"
)
# A block comment closing a JS/TS operand, skipped to reach the operand that a
# following ``!`` asserts non-null (PRRT_kwDOSJAM6s63S15a).
_JS_TS_TRAILING_BLOCK_COMMENT_RE = re.compile(r"/\*(?:(?!\*/).)*\*/$")
# The last token of an operand that a contextual ``satisfies``/``as`` can follow
# as a type operator (PRRT_kwDOSJAM6s63WfSH).
_JS_TS_OPERAND_END_RE = re.compile(r"[\w$)\]}]$")
_JS_TS_LEADING_CONTINUATION_RE = re.compile(
    r"^[ \t]*(?:(?!\+\+|--)[.?:,=*/%&|^<>+\-(\[`]|(?:instanceof|in)(?![\w$]))"
)
# A JS/TS statement that leaves the next one as its brace-less body: an
# ``if``/``for``/``while``/``with`` head, ``else`` or ``do`` (PRRT_kwDOSJAM6s63QK8_).
# The head's ``(`` must match the statement's final ``)``, so a body already
# completed on the line (``if (on) init()``) leaves none (PRRT_kwDOSJAM6s63VYOX).
# A head opens a statement, possibly the body of another brace-less head
# (``if (on) while (x)``) (PRRT_kwDOSJAM6s63V2yC).
_JS_TS_BARE_BODY_HEAD_RE = re.compile(r"(?<![\w$])(?:else|do)$")
_JS_TS_DO_RE = re.compile(r"(?<![\w$])do(?![\w$])")
_JS_TS_CONTROL_HEAD_RE = re.compile(r"(?<![\w$])(?:if|for|while|with)(?:[ \t]+await)?[ \t]*$")
_JS_TS_LABEL_RE = re.compile(r"(?<![\w$])[A-Za-z_$][\w$]*[ \t]*:$")
# The ``=`` assigning a JS/TS destructuring pattern (PRRT_kwDOSJAM6s63W936).
_JS_TS_DESTRUCTURING_EQUALS_RE = re.compile(r"[ \t]*=(?![=>])")
# A JS/TS identifier any assignment or update operator writes, wherever it sits
# in its statement — an inline ``if (on) LIMIT = 20`` body included
# (PRRT_kwDOSJAM6s63XLQk). A parenthesized ``(LIMIT) = 20`` / ``++(LIMIT)``
# target writes it too (PRRT_kwDOSJAM6s63X1Cq).
_JS_TS_ASSIGNED_TARGET_RE = re.compile(
    rf"(?<![\w$.])({_JS_IDENT})(?:[ \t]*\))*[ \t]*"
    r"(?:(?:\*\*|<<|>>>?|&&|\|\||\?\?|[-+*/%&|^])?=(?![=>])|\+\+|--)"
    rf"|(?:\+\+|--)(?:[ \t]*\()*[ \t]*({_JS_IDENT})"
)
# A ``for`` head up to the target its ``of``/``in`` writes each iteration, which
# a block-scoped ``let``/``const`` declares afresh but a ``var`` does not
# (PRRT_kwDOSJAM6s63XrHa).
_JS_TS_LOOP_HEAD_RE = re.compile(r"(?<![\w$.])for(?:[ \t]+await)?[ \t]*\([ \t]*(?:var[ \t]+)?")
# A parenthesized ``for ((LIMIT) of xs)`` target, or a pattern's closing
# ``]``/``}``, may abut its ``of``/``in``.
_JS_TS_LOOP_TARGET_RE = re.compile(
    rf"(?:\([ \t]*)*({_JS_IDENT})(?:[ \t]*\))*"
    r"(?=(?<=\))[ \t]*(?:of|in)(?![\w$])|[ \t]+(?:of|in)(?![\w$]))"
)
_JS_TS_LOOP_KEYWORD_RE = re.compile(r"[ \t]*(?:of|in)(?![\w$])")
# A word a destructuring pattern can follow, unlike the object of ``a[LIMIT]``.
_JS_TS_PATTERN_KEYWORD_RE = re.compile(r"(?<![\w$])(?:const|let|var|else|do)$")


def reviewer_comment_text(thread: ReviewThread) -> str:
    """The reviewer-authored text of ``thread`` — the only comment-named evidence source.

    Comments AWF posted (``viewer_did_author``) are excluded because they can
    carry the agent's own reason; ``review_context`` is a separate review body,
    not this thread's comment. ``body_excerpt`` is used only when GitHub
    supplied no comment history.
    """
    if thread.comments:
        return "\n".join(
            comment.body for comment in thread.comments if not comment.viewer_did_author
        )
    return thread.body_excerpt or ""


def _comment_named_paths(text: str) -> tuple[tuple[str, int | None], ...]:
    """Repo-relative ``(path, line)`` pairs named in ``text``, de-duplicated in order."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _normalize_evidence_item_path,
    )

    named: dict[tuple[str, int | None], None] = {}
    for token in _PATH_TOKEN_SPLIT_RE.split(_URL_RE.sub(" ", text)):
        match = _NAMED_PATH_RE.fullmatch(token.rstrip(_PATH_TRAILING_PUNCTUATION))
        if match is None:
            continue
        path = _normalize_evidence_item_path(match.group(1))
        if not path or any(segment in {"", ".", ".."} for segment in path.split("/")):
            continue
        line = int(match.group(2)) if match.group(2) is not None else None
        if line is not None and line < 1:
            continue
        named[(path, line)] = None
    return tuple(named)


def _comment_named_identifiers(text: str) -> frozenset[str]:
    """Identifiers ``text`` names in single backticks (``name`` or ``name()``)."""
    names: set[str] = set()
    for match in _INLINE_CODE_RE.finditer(_FENCED_BLOCK_RE.sub(" ", text)):
        named = _NAMED_IDENTIFIER_RE.fullmatch(match.group(1).strip())
        if named is not None and named.group(1) not in _NON_DEFINITION_NAMES:
            names.add(named.group(1))
    return frozenset(names)


def _js_ts_line_ends_statement(
    raw: str, scan: str, *, typescript: bool, statement: Sequence[str] = (), jsx: bool = False
) -> bool:
    """Whether the JS/TS ``raw`` line (masked as ``scan``) can end its statement.

    A line whose code is followed by a masked literal ends on that operand; one
    followed only by a comment ends on its last code token. A trailing ``!``
    right after a masked string, template or regex literal is a TS non-null
    assertion on that operand, not a unary ``!`` (PRRT_kwDOSJAM6s63S15a). Only
    ``typescript`` code has type arguments to close; in JavaScript a trailing
    ``a<b >`` is a comparison (PRRT_kwDOSJAM6s63TdLy). A type-argument list may
    open on an earlier masked line of the ``statement`` (PRRT_kwDOSJAM6s63Tr7Q).
    In ``jsx`` code a line ending a completed JSX element ends on that operand
    (PRRT_kwDOSJAM6s63VJ_g); one inside an opening tag does not
    (PRRT_kwDOSJAM6s63Wpyz).
    """
    code = scan.rstrip()
    # A line-leading ``/>`` closing the tag is masked as a regex literal.
    if (
        jsx
        and not raw.lstrip().startswith("/>")
        and _jsx_opening_tag_is_open(" ".join([*statement, code]))
    ):
        return False
    tail = raw[len(code) :].lstrip()
    if tail and not tail.startswith(("//", "/*")):
        return True
    if code.endswith("!"):
        operand = raw[: len(code.rstrip("!"))].rstrip()
        while (comment := _JS_TS_TRAILING_BLOCK_COMMENT_RE.search(operand)) is not None:
            operand = operand[: comment.start()].rstrip()
        if operand and scan[len(operand) - 1] != operand[-1]:
            return True
    if typescript and _js_ts_ends_unfinished_type_operand(raw, scan, code, statement):
        return False
    trailing = _JS_TS_TRAILING_CONTINUATION_RE.search(code)
    return (
        trailing is None
        or (
            # A keyword-named property after a spaced or wrapped ``.`` is an
            # operand, not an operator (PRRT_kwDOSJAM6s63VtWM).
            trailing.group()[0].isalpha()
            and " ".join([*statement, code[: trailing.start()]]).rstrip().endswith(".")
        )
        or (
            trailing.group() in {"satisfies", "as"}
            and not _js_ts_type_operator_follows_operand(raw, scan, trailing.start())
        )
        or (jsx and _JSX_COMPLETED_ELEMENT_RE.search(code) is not None)
        or (typescript and _js_ts_ends_type_arguments(" ".join([*statement, code])))
    )


def _jsx_opening_tag_is_open(code: str) -> bool:
    """Whether the masked JSX ``code`` ends inside an opening tag, before its ``>``.

    A ``>`` inside the tag's ``{...}`` attribute expressions closes nothing.
    """
    open_tag = False
    depth = 0
    for position, char in enumerate(code):
        if not open_tag:
            open_tag = (
                _JSX_OPENING_TAG_RE.match(code, position) is not None
                and _JSX_OPENING_TAG_OPENER_RE.search(code, 0, position) is not None
            )
            depth = 0
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(depth - 1, 0)
        elif char == ">" and not depth:
            open_tag = False
    return open_tag


def _js_ts_ends_unfinished_type_operand(
    raw: str, scan: str, code: str, statement: Sequence[str] = ()
) -> bool:
    """Whether masked ``code`` ends ``as keyof`` / ``satisfies readonly``, awaiting its type.

    Only after a type operator that follows an operand is the ``keyof`` or
    ``readonly`` a type operator rather than a contextual identifier
    (PRRT_kwDOSJAM6s63XaW6). That operator may end the last masked line of the
    ``statement`` (``value as`` wrapping before ``keyof``).
    """
    prefix = _JS_TS_TRAILING_TYPE_PREFIX_RE.search(code)
    if prefix is None:
        return False
    before = raw[: prefix.start()].rstrip()
    while (comment := _JS_TS_TRAILING_BLOCK_COMMENT_RE.search(before)) is not None:
        before = before[: comment.start()].rstrip()
    if not before:
        previous = next((line.rstrip() for line in reversed(statement) if line.strip()), "")
        operator = _JS_TS_TRAILING_TYPE_OPERATOR_RE.search(previous)
        return operator is not None and _js_ts_type_operator_follows_operand(
            previous, previous, operator.start()
        )
    operator = _JS_TS_TRAILING_TYPE_OPERATOR_RE.search(before)
    return operator is not None and _js_ts_type_operator_follows_operand(
        raw, scan, operator.start()
    )


def _js_ts_type_operator_follows_operand(raw: str, scan: str, start: int) -> bool:
    """Whether the ``satisfies``/``as`` at ``start`` follows an operand on its line.

    Only then is it a type operator; after ``=``, a keyword operator or a line
    break it is a bare contextual identifier (PRRT_kwDOSJAM6s63WfSH). An operand
    masked as a string, template or regex literal counts, as does one completed
    by a postfix non-null assertion (``value! as``) (PRRT_kwDOSJAM6s63XU4a).
    """
    before = raw[:start].rstrip()
    while (comment := _JS_TS_TRAILING_BLOCK_COMMENT_RE.search(before)) is not None:
        before = before[: comment.start()].rstrip()
    if not before:
        return False
    if scan[len(before) - 1] != before[-1]:
        return True
    if before.endswith("!"):
        return _js_ts_type_operator_follows_operand(raw, scan, len(before.rstrip("!")))
    return (
        _JS_TS_OPERAND_END_RE.search(before) is not None
        and _JS_TS_TRAILING_CONTINUATION_RE.search(before) is None
    )


def _js_ts_ends_with_body_head(logical: str, *, previous: str = "") -> bool:
    """Whether the masked JS/TS ``logical`` statement leaves the next one as its body.

    A ``while (...)`` opening ``logical`` may terminate a ``do`` block that the
    ``previous`` statements of its line chain end with (PRRT_kwDOSJAM6s63W1Vj).
    """
    bare = _JS_TS_BARE_BODY_HEAD_RE.search(logical)
    if bare is not None:
        return _js_ts_head_opens_statement(logical[: bare.start()])
    if not logical.endswith(")"):
        return False
    depth = 0
    for position in range(len(logical) - 1, -1, -1):
        if logical[position] in ")]}":
            depth += 1
        elif logical[position] in "([{":
            depth -= 1
            if not depth:
                head = _JS_TS_CONTROL_HEAD_RE.search(logical[:position])
                return (
                    head is not None
                    and not (
                        head.group().startswith("while")
                        and _js_ts_ends_with_do_block(logical[: head.start()] or previous)
                    )
                    and _js_ts_head_opens_statement(logical[: head.start()])
                )
    return False


def _js_ts_ends_with_do_block(prefix: str) -> bool:
    """Whether the masked JS/TS ``prefix`` ends with a ``do`` statement's body.

    A ``while (...)`` after it terminates that ``do`` rather than heading a loop
    that takes the next statement as its body (PRRT_kwDOSJAM6s63Wpy7), whether
    the body is a block or a single statement (PRRT_kwDOSJAM6s63W94A).
    """
    prefix = prefix.rstrip(" \t")
    if not prefix.endswith("}"):
        return _js_ts_ends_with_braceless_do_body(prefix)
    depth = 0
    for position in range(len(prefix) - 1, -1, -1):
        if prefix[position] in ")]}":
            depth += 1
        elif prefix[position] in "([{":
            depth -= 1
            if not depth:
                head = _JS_TS_BARE_BODY_HEAD_RE.search(prefix[:position].rstrip(" \t"))
                return (
                    head is not None
                    and head.group() == "do"
                    and _js_ts_head_opens_statement(prefix[: head.start()])
                )
    return False


def _js_ts_ends_with_braceless_do_body(prefix: str) -> bool:
    """Whether the masked JS/TS ``prefix`` ends with a ``do`` and its single-statement body.

    The body after the last statement-opening ``do`` is one statement: no
    top-level ``;`` other than its own terminator, and no block of its own.
    """
    prefix = prefix.removesuffix(";").rstrip(" \t")
    for head in reversed(list(_JS_TS_DO_RE.finditer(prefix))):
        if not _js_ts_head_opens_statement(prefix[: head.start()]):
            continue
        body = prefix[head.end() :].strip(" \t")
        depth = 0
        for char in body:
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
            if depth < 0 or (char == ";" and not depth):
                return False
        return bool(body) and not body.startswith("{") and not depth
    return False


def _js_ts_head_opens_statement(prefix: str) -> bool:
    """Whether a JS/TS head after the masked ``prefix`` starts a statement.

    It does at the statement start, after a ``;`` or ``}``, or as the brace-less
    body of the head ``prefix`` ends with (PRRT_kwDOSJAM6s63V2yC), including
    after a statement label (``outer: while (x)``).
    """
    prefix = prefix.rstrip(" \t")
    label = _JS_TS_LABEL_RE.search(prefix)
    if label is not None:
        return _js_ts_head_opens_statement(prefix[: label.start()])
    return not prefix or prefix.endswith((";", "}")) or _js_ts_ends_with_body_head(prefix)


def _js_ts_next_line_continues(
    raw_lines: list[str], scan_lines: list[str], starts_in_code: list[bool], index: int
) -> bool:
    """Whether the JS/TS code line after ``index`` leads with an expression continuation.

    Blank, ``//`` and block-comment-only lines are skipped; a line opening with a
    ``/*`` is read from its masked code (PRRT_kwDOSJAM6s63TOG5), as is one that
    starts inside a block comment, whatever its prefix (PRRT_kwDOSJAM6s63U6z7).
    """
    for raw, scan, in_code in zip(
        raw_lines[index + 1 :], scan_lines[index + 1 :], starts_in_code[index + 1 :], strict=False
    ):
        comment = raw.lstrip().startswith(("/*", "*")) or not in_code
        if _is_ignorable_span_gap_line(raw) or (comment and not scan.strip()):
            continue
        return _JS_TS_LEADING_CONTINUATION_RE.match(scan if comment else raw) is not None
    return False


def _js_ts_line_ends_in_literal(raw_lines: list[str], index: int, *, path: str) -> bool:
    """Whether JS/TS line ``index``, which the next line opens inside, ends in a literal.

    Such a line ends in a multi-line template (or ``\\``-continued string) the
    next line continues, or in a block comment. Re-masked behind ``*/`` and a
    tab, the tab survives only when the ``*/`` closes a block comment
    (PRRT_kwDOSJAM6s63TdLt).
    """
    probe = "\n".join(raw_lines[: index + 1]) + "*/\t"
    return not _mask_comments_and_string_literals_for_callee_scan(probe, path=path).endswith("\t")


def _js_ts_ends_type_arguments(code: str) -> bool:
    """Whether ``code`` ends with the ``>`` closing a type-argument list (``Array<number>``).

    The list's ``<`` must directly follow the type or callee name; a spaced
    ``a < b >`` is a comparison, and a ``>`` of ``=>`` closes nothing
    (PRRT_kwDOSJAM6s63TIld). After an ``as``/``satisfies`` the name is a type,
    so its ``<`` may follow whitespace or a comment (PRRT_kwDOSJAM6s63XmEA).
    """
    if not code.endswith(">") or code.endswith("=>"):
        return False
    angles = 0
    depth = 0
    for position in range(len(code) - 1, 0, -1):
        char = code[position]
        if char in ")]}":
            depth += 1
        elif char in "([{":
            depth = max(depth - 1, 0)
        elif depth or (char == ">" and code[position - 1] == "="):
            continue
        elif char == ">":
            angles += 1
        elif char == "<":
            angles -= 1
            if not angles:
                return (
                    re.match(r"[\w$]", code[position - 1]) is not None
                    or _JS_TS_ASSERTED_TYPE_NAME_RE.search(code, 0, position) is not None
                )
    return False


def _js_ts_type_parameter_list_end(logical: str, start: int) -> int | None:
    """Index of the ``>`` closing a type-parameter list opened at ``logical[start]``.

    A ``<`` opens one only where an expression, a ``function``/``class`` head or
    a construct signature's ``new`` starts, and when its matching ``>`` is followed by the ``(`` or ``{`` of a
    generic arrow, function or class, or by ``extends``/``implements``; a ``>``
    of ``=>`` closes nothing. Anything else is a comparison: ``None``.
    """
    if _JS_TS_TYPE_PARAMETER_LIST_OPENER_RE.search(logical, 0, start) is None:
        return None
    angles = 0
    depth = 0
    for position in range(start, len(logical)):
        char = logical[position]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(depth - 1, 0)
        elif depth:
            continue
        elif char == ";":
            return None
        elif char == "<":
            angles += 1
        elif char == ">" and logical[position - 1] != "=":
            angles -= 1
            if not angles:
                follower = _JS_TS_TYPE_PARAMETER_LIST_FOLLOWER_RE.match(logical, position + 1)
                return position if follower is not None else None
    return None


def _js_ts_secondary_declarators(logical: str) -> list[re.Match[str]]:
    """Declarators after the first of the JS/TS declaration ``logical``.

    Each follows a ``,`` outside brackets and type-parameter lists and before
    the first top-level ``;``, so the ``LIMIT = number`` default of a generic
    ``<T, LIMIT = number>(x: T) => x`` binds nothing (PRRT_kwDOSJAM6s63SGuR); a
    comma that opens no ``name =`` (a generic's ``Map<K, V>``) is skipped.
    """
    found: list[re.Match[str]] = []
    depth = 0
    position = -1
    while position + 1 < len(logical):
        position += 1
        char = logical[position]
        if depth == 0 and char == "<":
            end = _js_ts_type_parameter_list_end(logical, position)
            if end is not None:
                position = end
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(depth - 1, 0)
        elif depth == 0 and char == ";":
            break
        elif depth == 0 and char == ",":
            match = _binding_match(_JS_TS_DECLARATOR_RE, logical, position + 1)
            if match is not None:
                found.append(match)
    return found


def _js_ts_assigned_names(segment: str, claimed: frozenset[int] = frozenset()) -> set[str]:
    """Identifiers the JS/TS statement ``segment`` assigns past its ``claimed`` heads.

    ``({ LIMIT } = next)`` and ``[LIMIT] = next`` rebind ``LIMIT`` with no
    ``name =`` for the plain-binding readers to match, so the earlier binding
    looked effective while importers reach the destructured value
    (PRRT_kwDOSJAM6s63W936). So does an assignment that does not open its
    statement — ``if (on) LIMIT = 20``, a braced block's, a ``+=`` or ``++`` —
    which those statement-opening readers never see (PRRT_kwDOSJAM6s63XLQk),
    or a ``for (LIMIT of xs)`` / ``for (LIMIT in o)`` target, which writes with
    no assignment operator at all (PRRT_kwDOSJAM6s63XrHa).
    Every identifier a pattern holds is returned — a property key or a
    default's operand included — and an assignment is not told apart from a
    parameter default or a shadowing local, so a name the statement may bind
    fails closed rather than leaving that dead binding as evidence. Only the
    binding heads the caller reads itself, at offsets ``claimed``, are skipped.
    """
    found = {
        match.group(group)
        for match in _JS_TS_ASSIGNED_TARGET_RE.finditer(segment)
        for group in (1, 2)
        if match.group(group) is not None and match.start(group) not in claimed
    }
    previous = -1
    for opener, char in enumerate(segment):
        if char in "[{" and (
            previous < 0
            or segment[previous] not in "].$_"
            and not segment[previous].isalnum()
            or _JS_TS_PATTERN_KEYWORD_RE.search(segment, max(previous - 4, 0), previous + 1)
            is not None
        ):
            depth = 0
            for position in range(opener, len(segment)):
                if segment[position] in "([{":
                    depth += 1
                elif segment[position] in ")]}":
                    depth -= 1
                    if not depth:
                        break
            else:
                continue
            if _JS_TS_DESTRUCTURING_EQUALS_RE.match(segment, position + 1) is not None:
                found.update(re.findall(_JS_IDENT, segment[opener : position + 1]))
        if char not in " \t":
            previous = opener
    for head in _JS_TS_LOOP_HEAD_RE.finditer(segment):
        target = _JS_TS_LOOP_TARGET_RE.match(segment, head.end())
        if target is not None:
            found.add(target.group(1))
        elif segment.startswith(("[", "{"), head.end()):
            depth = 0
            for position in range(head.end(), len(segment)):
                if segment[position] in "([{":
                    depth += 1
                elif segment[position] in ")]}":
                    depth -= 1
                    if not depth:
                        break
            if _JS_TS_LOOP_KEYWORD_RE.match(segment, position + 1) is not None:
                found.update(re.findall(_JS_IDENT, segment[head.end() : position + 1]))
    return found


def _annotation_binding_equals(text: str, start: int) -> int | None:
    """Index of the ``=`` binding an annotation that begins at ``text[start]``.

    That is its first ``=`` outside brackets and type-argument lists, skipping a
    function type's ``=>``. A top-level ``,`` or ``;`` first ends an
    uninitialized declarator, and ``==`` binds nothing: ``None``.
    """
    depth = 0
    for position in range(start, len(text)):
        char = text[position]
        if text.startswith("=>", position) or (char == ">" and text[position - 1] == "="):
            continue
        if char in "([{<":
            depth += 1
        elif char in ")]}>":
            depth = max(depth - 1, 0)
        elif depth:
            continue
        elif char in ",;":
            return None
        elif char == "=":
            return None if text.startswith("==", position) else position
    return None


def _binding_match(pattern: re.Pattern[str], text: str, pos: int = 0) -> re.Match[str] | None:
    """``pattern``'s binding at ``pos``, an annotated one ending at its own ``=``."""
    match = pattern.match(text, pos)
    if match is None or match.start(2) < 0:
        return match
    equals = _annotation_binding_equals(text, match.end(2))
    return None if equals is None else pattern.fullmatch(text, pos, equals + 1)


def _statement_line_index(statement: list[str], offset: int) -> int:
    """Index into ``statement`` of the line holding ``offset`` of ``" ".join(statement)``."""
    for index, line in enumerate(statement[:-1]):
        if offset <= len(line):
            return index
        offset -= len(line) + 1
    return len(statement) - 1


def _assignment_span_end_line(
    file_text: str, scan_lines: list[str], start: int, *, path: str, column: int = 0
) -> int:
    """Inclusive end line of the column-0 binding whose statement opens at ``start``.

    Read over comment/string-masked lines, so a multi-line string's contents are
    blank gaps rather than dedented statements, and a line inside open brackets
    or after a ``\\`` continuation belongs to the statement whatever its indent
    (PRRT_kwDOSJAM6s63Lp9D). Past those, the next column-0 line ends the span on
    the line before it, as ``_definition_span_end_line`` does — including one
    that opens a string literal or block comment, which masks blank. The raw
    lines are re-masked each behind a tab, which the masker keeps only outside a
    literal, so such a line is told apart from one the binding's own literal
    continues (PRRT_kwDOSJAM6s63MDQr). In JS/TS, where indentation is not scope,
    a line of any indent ends the span only after a complete statement
    (PRRT_kwDOSJAM6s63PU_-, PRRT_kwDOSJAM6s63Pq1L); a leading ``/*`` is a comment, not an operator, so
    only code after it can continue the expression (PRRT_kwDOSJAM6s63Pq1C). A
    ``;`` outside brackets ends the statement on its
    own line, so a ``;``-separated next statement's brackets are not the
    binding's (PRRT_kwDOSJAM6s63Pd7z); one opening at ``column`` of ``start``
    after such a ``;`` reads only the code from there (PRRT_kwDOSJAM6s63Sh3G).
    """
    js_ts = Path(path).suffix.lower() in _JS_TS_CALL_SITE_SUFFIXES
    typescript = Path(path).suffix.lower() in _TS_SUFFIXES
    raw_lines = file_text.splitlines()
    # Masked from the top of the file, so a ``start`` line closing an earlier
    # literal is not read as opening one (PRRT_kwDOSJAM6s63UNtz).
    starts_in_code = [
        line.startswith("\t")
        for line in _mask_comments_and_string_literals_for_callee_scan(
            "\n".join(f"\t{raw}" for raw in raw_lines), path=path
        ).split("\n")
    ][start - 1 :]
    depth = 0
    continued = False
    complete = False
    comment_open = False
    last_code = start
    code_lines: list[str] = []
    for index in range(start - 1, len(scan_lines)):
        scan = scan_lines[index]
        if index == start - 1:
            scan = " " * column + scan[column:]
        raw = raw_lines[index]
        # Code after the ``*/`` closing a block comment opens a statement as a
        # line starting in code would (PRRT_kwDOSJAM6s63UXPl), even when the
        # comment opened after code (PRRT_kwDOSJAM6s63Ukfg).
        in_code = starts_in_code[index - start + 1] or (comment_open and bool(scan.strip()))
        # A JS/TS line holding only a block comment (or its continuation) is a
        # gap too: the expression may continue past it (PRRT_kwDOSJAM6s63QLb-).
        in_comment = (
            js_ts
            and index >= start
            and not scan.strip()
            and (
                raw.lstrip().startswith("/*") if starts_in_code[index - start + 1] else comment_open
            )
        )
        comment_open = (
            js_ts
            and index + 1 < len(scan_lines)
            and not starts_in_code[index - start + 2]
            and not _js_ts_line_ends_in_literal(raw_lines, index, path=path)
        )
        # A literal's line spelling ``// ...`` is its contents, not a comment
        # gap (PRRT_kwDOSJAM6s63UG07).
        gap = (
            in_comment
            or not raw.strip()
            or (starts_in_code[index - start + 1] and _is_ignorable_span_gap_line(raw))
        )
        if (
            index >= start
            and depth == 0
            and not continued
            and in_code
            and not gap
            and (
                (
                    complete
                    and not _JS_TS_LEADING_CONTINUATION_RE.match(
                        raw
                        if starts_in_code[index - start + 1] and not raw.lstrip().startswith("/*")
                        else scan
                    )
                )
                if js_ts
                else _leading_indent(raw) == 0
            )
        ):
            return last_code if js_ts else index
        for char in scan:
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth = max(depth - 1, 0)
            elif char == ";" and depth == 0:
                return index + 1
        continued = scan.rstrip().endswith("\\")
        if not gap:
            complete = _js_ts_line_ends_statement(
                raw, scan, typescript=typescript, statement=code_lines, jsx=_path_is_jsx(path)
            )
            code_lines.append(scan)
            last_code = index + 1
    return last_code if js_ts else len(scan_lines)


def _module_level_assignment_spans(
    file_text: str, names: frozenset[str], *, path: str
) -> list[tuple[int, int]]:
    """Spans of effective module-level plain bindings of ``names``, multi-line literals included.

    The module body runs its column-0 bindings in order, so only the last one of
    a name is the value importers reach; an earlier one is dead, and an edit
    confined to it changes nothing (PRRT_kwDOSJAM6s63LR_S). A name the module
    scope also binds another way — a ``def``/``class`` head after that last
    binding or under a guard, an ``import``, or any assignment, loop or ``with``
    target the ``_rebound_scope_names`` readers find — withholds it entirely,
    matching ``_importable_definition_spans_for_names``'s effective-binding rule.
    Only statement-opening lines count: a column-0 line inside open brackets or
    after a ``\\`` continuation (a keyword argument, say) binds nothing
    (PRRT_kwDOSJAM6s63L3DZ). The pattern is matched against the whole logical
    statement, so an annotation that wraps before its ``=`` still binds
    (PRRT_kwDOSJAM6s63MSFg), and every target of a chained ``A = B = 10``
    binds, not only the first (PRRT_kwDOSJAM6s63PKQ9). In JS/TS a statement
    after a brace-less control head or an incomplete one (a concise arrow's
    ``=>``) is that statement's body, not a module-level binding
    (PRRT_kwDOSJAM6s63QK8_). Each declarator of a JS/TS ``const A = 0, B = v``
    binds over its own lines, from its name to the ``,`` ending it, and a
    declarator list read past a trailing ``,`` and any gap after it
    (PRRT_kwDOSJAM6s63QfAU) or a next line leading with a continuation
    (PRRT_kwDOSJAM6s63TOG5) or continuing a multi-line literal
    (PRRT_kwDOSJAM6s63TdLt), which ends on the line closing it even with no
    ``;`` (PRRT_kwDOSJAM6s63Tr7E). Each ``;``-separated statement of a line binds over
    its own lines too, unless the line is a Python compound head's body
    (PRRT_kwDOSJAM6s63Sh3G). A JS/TS destructuring assignment of the name,
    which no ``name =`` reader matches, withholds it too (PRRT_kwDOSJAM6s63W936),
    as does any JS/TS assignment of it not opening its statement, such as an
    inline ``if (on) LIMIT = 20`` body (PRRT_kwDOSJAM6s63XLQk).
    """
    js_ts = Path(path).suffix.lower() in _JS_TS_CALL_SITE_SUFFIXES
    typescript = Path(path).suffix.lower() in _TS_SUFFIXES
    scan_lines = _definition_head_scan_lines(file_text, path=path)
    raw_lines = file_text.splitlines()
    starts_in_code = [
        line.startswith("\t")
        for line in _mask_comments_and_string_literals_for_callee_scan(
            "\n".join(f"\t{raw}" for raw in raw_lines), path=path
        ).split("\n")
    ]
    bindings: list[tuple[str, int, int | None, int]] = []
    reassigned: set[str] = set()
    depth = 0
    continued = False
    in_body = False
    previous = ""
    statement: list[str] = []
    statement_start = 1
    for index, scan in enumerate(scan_lines):
        if not (depth or continued):
            statement_start, statement = index + 1, []
        statement.append(scan)
        for char in scan:
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth = max(depth - 1, 0)
        continued = scan.rstrip().endswith("\\")
        if (depth or continued) and index + 1 < len(scan_lines):
            continue
        logical = " ".join(statement)
        lead = 0
        declaration = False
        if js_ts:
            lead = len(logical) - len(logical.lstrip(" \t"))
            logical = logical.strip(" \t")
            if not logical:
                continue
            declaration = _JS_TS_DECLARATION_RE.match(logical) is not None
            if (
                declaration
                and not in_body
                and index + 1 < len(scan_lines)
                and not (
                    (
                        scan.strip()
                        or (
                            # A code line masked blank yet not a comment
                            # is a whole literal operand (PRRT_kwDOSJAM6s63VnCo).
                            bool(raw_lines[index].strip())
                            and not raw_lines[index].lstrip().startswith(("//", "/*"))
                            if starts_in_code[index]
                            else _js_ts_line_ends_in_literal(raw_lines, index - 1, path=path)
                        )
                    )
                    and _js_ts_line_ends_statement(
                        raw_lines[index],
                        scan,
                        typescript=typescript,
                        statement=statement[:-1],
                        jsx=_path_is_jsx(path),
                    )
                    and not _js_ts_next_line_continues(raw_lines, scan_lines, starts_in_code, index)
                    and (
                        starts_in_code[index + 1]
                        or not _js_ts_line_ends_in_literal(raw_lines, index, path=path)
                    )
                )
            ):
                continued = True
                continue
            is_body = in_body
            in_body = _js_ts_ends_with_body_head(logical, previous=previous) or not (
                _js_ts_line_ends_statement(
                    raw_lines[index],
                    scan,
                    typescript=typescript,
                    statement=statement[:-1],
                    jsx=_path_is_jsx(path),
                )
            )
            # A brace-less body continues its head's chain, which a ``do``
            # block ends across lines (PRRT_kwDOSJAM6s63W1Vj).
            previous = f"{previous} {logical}" if is_body else logical
            if is_body:
                for segment in _simple_statement_segments(logical):
                    reassigned |= _js_ts_assigned_names(segment)
                continue
        texts = (
            list(_simple_statement_segments(logical))
            if js_ts or not _PYTHON_INLINE_BODY_RE.match(logical)
            else [logical]
        )
        begin = 0
        for number, text in enumerate(texts):
            segment = text.lstrip(" \t") if number else text
            base = lead + begin + len(text) - len(segment)
            stop = begin + len(text)
            begin = stop + 1
            declares = js_ts and _JS_TS_DECLARATION_RE.match(segment) is not None
            first = _binding_match(_MODULE_LEVEL_ASSIGNMENT_RE, segment)
            # A JS/TS ``name: ... =`` outside a declaration is a labelled
            # statement, which binds no ``name`` (PRRT_kwDOSJAM6s63SzHq).
            if (
                js_ts
                and not declares
                and first is not None
                and ":" in segment[first.end(1) : first.end()]
            ):
                first = None
            heads = [first] if first is not None else []
            if declares:
                heads += _js_ts_secondary_declarators(segment)
            claimed: set[int] = set()
            for position, head in enumerate(heads):
                start_index = _statement_line_index(statement, base + head.start(1))
                start = statement_start + start_index
                end = (
                    statement_start
                    + _statement_line_index(statement, base + heads[position + 1].start() - 1)
                    if position + 1 < len(heads)
                    else statement_start + _statement_line_index(statement, lead + stop)
                    if number + 1 < len(texts)
                    else None
                )
                # The column this statement opens at, past the ``;`` it follows.
                column = (
                    base - sum(len(line) + 1 for line in statement[:start_index])
                    if number and _statement_line_index(statement, base) == start_index
                    else 0
                )
                match: re.Match[str] | None = head
                while match is not None:
                    claimed.add(match.start(1))
                    if match.group(1) in names:
                        bindings.append((match.group(1), start, end, column))
                    match = _CHAINED_ASSIGNMENT_TARGET_RE.match(segment, match.end())
            if js_ts:
                reassigned |= _js_ts_assigned_names(segment, frozenset(claimed))
    if not bindings:
        return []
    last = {name: (start, end, column) for name, start, end, column in bindings}
    starts = frozenset(start for _name, start, _end, _column in bindings)
    all_spans = _iter_definition_spans(file_text, path=path)
    shadowed = {
        name
        for name, start, _end, indent in all_spans
        if name in last
        and start not in starts
        and (indent > 0 or start > last[name][0])
        and _definition_binding_scope(file_text, all_spans, start=start, indent=indent)[0] == 0
    }
    rebound = _rebound_scope_names(
        file_text,
        all_spans,
        path=path,
        collected=[
            (name, start, 0, (0, 0), (start, start)) for name, start, _end, _column in bindings
        ],
        statement_heads=frozenset(
            (start, column) for _name, start, _end, column in bindings if column
        ),
    )
    return [
        (
            start,
            end
            if end is not None
            else _assignment_span_end_line(file_text, scan_lines, start, path=path, column=column),
        )
        for name, (start, end, column) in last.items()
        if name not in shadowed and (0, name) not in rebound and name not in reassigned
    ]


def _without_trailing_gaps(
    file_text: str | None, spans: list[tuple[int, int]], *, path: str
) -> list[tuple[int, int]]:
    """``spans`` ending at their last non-blank, non-comment line of ``file_text``.

    Definition spans keep the blank/comment gap before the next statement, but a
    hunk there changes no definition line, so it is dropped before the overlap
    check on either side (PRRT_kwDOSJAM6s63LtTS, PRRT_kwDOSJAM6s63L_5C). An
    insert in the gap that does extend a body shows up in the right-side span.
    Only a line that starts outside a literal is a gap: a multi-line string's
    last line spelling ``# ...`` or ``// ...`` is its contents, told apart by
    the tab re-masking ``_assignment_span_end_line`` uses (PRRT_kwDOSJAM6s63SSj1).
    The masker is language-aware, so a line it leaves non-blank is code — a
    Python ``// 2`` is floor division, not a comment (PRRT_kwDOSJAM6s63T8r0).
    """
    lines = (file_text or "").splitlines()
    masked = (
        _mask_comments_and_string_literals_for_callee_scan(
            "\n".join(f"\t{raw}" for raw in lines), path=path
        ).split("\n")
        if spans
        else []
    )
    trimmed: list[tuple[int, int]] = []
    for start, end in spans:
        while (
            end > start
            and masked[end - 1].startswith("\t")
            and not masked[end - 1].strip()
            and _is_ignorable_span_gap_line(lines[end - 1])
        ):
            end -= 1
        trimmed.append((start, end))
    return trimmed


def _diff_new_hunk_overlaps_line_span(diff_text: str, start: int, end: int) -> bool:
    """True when a new-side hunk of ``diff_text`` overlaps the inclusive ``[start, end]``.

    ``start``/``end`` are lines of the *right* blob, so new-side hunk ranges are
    exact there: an overlap means the range added or rewrote a line of that span.
    Pure deletions (``new_count == 0``) have no new-side lines and never count.
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _UNIFIED_DIFF_HUNK_HEADER_RE,
    )

    for match in _UNIFIED_DIFF_HUNK_HEADER_RE.finditer(diff_text):
        new_start = int(match.group(3))
        new_count = int(match.group(4)) if match.group(4) is not None else 1
        if new_count > 0 and new_start <= end and start < new_start + new_count:
            return True
    return False


async def _commit_range_changes_comment_named_target(
    self: Any,
    *,
    worktree_path: Path,
    left: str,
    right: str,
    comment_text: str,
    anchor_head: str | None = None,
) -> bool:
    """True when ``left``..``right`` changes a target the reviewer's text names.

    (a) A named path the range changed: any change without ``:line``, or one the
    line-anchored rule accepts for that line (the first
    ``_MAX_COMMENT_NAMED_PATHS`` such probes). Named paths and lines are in the
    coordinates of ``anchor_head``, the PR head the review was written against,
    so they are first relocated into ``left`` through the commits between them —
    an earlier item in the batch may have renamed the file or shifted the line
    (PRRT_kwDOSJAM6s63LTKz); one that no longer maps is skipped. (b) A named identifier whose
    definition span at ``left`` — a module-reachable head, decorators included,
    or a module-level plain binding — overlaps an old-side hunk of the range in
    a changed file, or whose span at ``right`` overlaps a new-side hunk, so a
    definition the range adds counts in an existing or a new file
    (PRRT_kwDOSJAM6s63LTK6). Each file is diffed paired with its rename source or
    target so a pure move stays hunkless. Fails closed on any unreadable Git
    output.
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _changed_paths_in_commit_range,
        _commit_range_touches_path,
        _diff_hunk_overlaps_line_span,
        _map_review_line_through_commits,
        _map_review_path_through_commits,
        _normalize_evidence_item_path,
        _path_text_at_ref,
        _rename_map_in_commit_range,
    )

    named_paths = _comment_named_paths(comment_text)
    names = _comment_named_identifiers(comment_text)
    if not (named_paths or names):
        return False
    changed = [
        normalized
        for normalized in (
            _normalize_evidence_item_path(path)
            for path in await _changed_paths_in_commit_range(
                self, worktree_path=worktree_path, left=left, right=right
            )
        )
        if normalized
    ]
    if not changed:
        return False
    anchor = (anchor_head or "").strip()
    remap = bool(anchor) and anchor.lower() != left.lower()
    line_probes = 0
    for named_path, named_line in named_paths:
        path: str | None = named_path
        if remap:
            path = await _map_review_path_through_commits(
                self,
                worktree_path=worktree_path,
                anchor_head=anchor,
                target_head=left,
                path=named_path,
            )
        if path is None or path not in changed:
            continue
        if named_line is None:
            return True
        if line_probes >= _MAX_COMMENT_NAMED_PATHS:
            continue
        line_probes += 1
        line: int | None = named_line
        if remap:
            line = await _map_review_line_through_commits(
                self,
                worktree_path=worktree_path,
                anchor_head=anchor,
                target_head=left,
                path=named_path,
                line=named_line,
            )
        if line is None or line < 1:
            continue
        if await _commit_range_touches_path(
            self, worktree_path=worktree_path, left=left, right=right, path=path, line=line
        ):
            return True
    if not names:
        return False
    rename_map, _name_status_z = await _rename_map_in_commit_range(
        self, worktree_path=worktree_path, left=left, right=right
    )
    renamed_from = {target: source for source, target in rename_map.items()}
    # Definition discovery reads Python and JS/TS heads; a docs or text snippet
    # spelling the name is not its definition (PRRT_kwDOSJAM6s63O-t2).
    sources = [path for path in changed if Path(path).suffix.lower() in _DEFINITION_SUFFIXES]
    for candidate in sources[:_MAX_CALLEE_EVIDENCE_CANDIDATE_PATHS]:
        file_text = await _path_text_at_ref(
            self, worktree_path=worktree_path, ref=left, path=candidate
        )
        head_spans, binding_spans = _named_definition_spans(file_text, names, path=candidate)
        spans = _without_trailing_gaps(file_text, head_spans, path=candidate)
        bindings = _without_trailing_gaps(file_text, binding_spans, path=candidate)
        # A rename source's right-side text is read under its target, itself a candidate.
        right_text = (
            None
            if candidate in rename_map
            else await _path_text_at_ref(
                self, worktree_path=worktree_path, ref=right, path=candidate
            )
        )
        right_heads, right_bindings = _named_definition_spans(right_text, names, path=candidate)
        right_spans = _without_trailing_gaps(
            right_text, right_heads + right_bindings, path=candidate
        )
        if not (spans or bindings or right_spans):
            continue
        source = renamed_from.get(candidate)
        diff_text = await _path_diff_text_in_commit_range(
            self,
            worktree_path=worktree_path,
            left=left,
            right=right,
            path=source or candidate,
            rename_path=candidate if source else rename_map.get(candidate),
        )
        if diff_text is None:
            continue
        if (
            any(
                _diff_hunk_overlaps_line_span(diff_text, start, end, file_text=file_text)
                or _diff_adds_decorators_above_span(diff_text, start)
                for start, end in spans
            )
            or any(
                # A statement does not extend by indentation, so a left-side insert
                # counts only inside the binding; one that does continue it overlaps
                # its right-side span (PRRT_kwDOSJAM6s63P5ZX).
                _diff_hunk_overlaps_line_span(diff_text, start, end)
                or _diff_adds_decorators_above_span(diff_text, start)
                for start, end in bindings
            )
            or any(
                _diff_new_hunk_overlaps_line_span(diff_text, start, end)
                for start, end in right_spans
            )
        ):
            return True
    return False


def _named_definition_spans(
    file_text: str | None, names: frozenset[str], *, path: str
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Module-scope definition-head spans and plain-binding spans of ``names`` in ``file_text``.

    A backticked name carries no receiver, so it is held to the bare-name
    module-scope rule: a same-named method of some class in a changed file is
    not the definition the reviewer named (PRRT_kwDOSJAM6s63LVep).
    """
    if not file_text:
        return [], []
    return _importable_definition_spans_for_names(
        file_text, frozenset(), path=path, bare_names=names
    ), _module_level_assignment_spans(file_text, names, path=path)
