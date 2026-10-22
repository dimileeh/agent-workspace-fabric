"""ES-module binding readers for JS/TS cross-file callee evidence (issue #1047).

The Python import readers in ``..._ancestry_cross_file_imports`` return nothing
for a JS/TS call site, which used to leave every JS/TS callee on the name-only
rule: ``import { record } from './telemetry'; record()`` was then "fixed" by an
edit to an unrelated ``export function record`` anywhere in the range. This
module is the JS/TS counterpart of those readers. It is pure and dependency
free: a small tokenizer, the import and export tables of one file, a
deliberately over-inclusive shadow detector, and relative-specifier probing.
The Git reads that tie them together live in
``..._ancestry_cross_file_es_evidence``.

Every reader here fails closed. Text the tokenizer cannot lex, an import or
export statement it cannot read, and a specifier it cannot resolve to exactly
one file all yield None, which the evidence gate treats as "no evidence".
"""

from __future__ import annotations

import bisect
import itertools
import posixpath
import re
from typing import NamedTuple, TypeGuard

from awf.runtime.pr_monitor_runner import (
    pre_push_validation_fix_pass_ancestry_callees_masking as _masking,
)

# The eight JS/TS source suffixes, shared with the callee scan's masking.
_JS_TS_CALL_SITE_SUFFIXES = _masking._JS_TS_PRIVATE_FIELD_SUFFIXES

# Suffixes whose ``<`` at an expression start opens a JSX element. Plain
# ``.ts`` / ``.mts`` / ``.cts`` use ``<`` for generics and type assertions.
_JSX_CAPABLE_SUFFIXES = frozenset({".js", ".jsx", ".mjs", ".cjs", ".tsx"})

_IDENT_RE = re.compile(r"(?:[^\W\d]|\$)[\w$]*")
_NUMBER_RE = re.compile(r"\d[\w$.]*|\.\d[\w$]*")
_JSX_NAME_RE = re.compile(r"[\w$.:-]*")
# Longest first, so ``>>>=`` is not read as ``>>`` then ``>=``.
_MULTI_CHAR_PUNCTUATORS = (
    ">>>=",
    "...",
    "===",
    "!==",
    "**=",
    "<<=",
    ">>=",
    ">>>",
    "&&=",
    "||=",
    "??=",
    "=>",
    "==",
    "!=",
    "<=",
    ">=",
    "&&",
    "||",
    "??",
    "?.",
    "++",
    "--",
    "+=",
    "-=",
    "*=",
    "/=",
    "%=",
    "&=",
    "|=",
    "^=",
    "**",
    "<<",
    ">>",
)
_SINGLE_CHAR_PUNCTUATORS = frozenset("+-*/%&|^!~?:;,.<>=@")
_CLOSER_FOR = {"(": ")", "[": "]", "{": "}"}
# Keywords whose ``( ... )`` is a statement head rather than an expression.
_CONDITION_KEYWORDS = frozenset({"for", "if", "while", "with"})
_LINE_TERMINATORS = frozenset("\n\r  ")


class _EsToken(NamedTuple):
    """One significant token: kind, value, bracket depth, preceding newline, line.

    ``kind`` is ``ident``, ``punct``, ``string``, ``template``, ``regex``,
    ``number`` or ``jsx`` (a skipped JSX element). A string keeps
    its raw text between the quotes, so a module specifier is readable.
    ``line`` is 1-based and counted the way ``str.splitlines`` counts, the
    numbering a review anchor uses; a literal or JSX token sits on the line
    where it ends.
    """

    kind: str
    value: str
    depth: int
    newline_before: bool
    line: int


class _EsLexError(Exception):
    """Raised inside the tokenizer when the text cannot be lexed."""


class _EsLexer:
    """A recursive tokenizer; templates and JSX expressions recurse into code."""

    def __init__(self, text: str, *, jsx: bool, tsx: bool = False) -> None:
        self.text = text
        self.pos = 0
        self.depth = 0
        self.newline = False
        self.jsx = jsx
        self.tsx = tsx
        self.tokens: list[_EsToken] = []
        # Offset just past each line, so a position's line is one bisect away.
        self.line_ends = list(
            itertools.accumulate(len(line) for line in text.splitlines(keepends=True))
        )
        # Index of the last ``)`` that closed an ``if`` / ``for`` / ``while`` /
        # ``with`` condition: a statement, so a regex, may start after it.
        self.condition_close = -1

    def emit(self, kind: str, value: str) -> None:
        line = bisect.bisect_right(self.line_ends, self.pos) + 1
        self.tokens.append(_EsToken(kind, value, self.depth, self.newline, line))
        self.newline = False

    def ends_operand(self, index: int) -> bool:
        """True when ``tokens[index]`` ends an operand, so a ``!`` after it is postfix.

        TS reads ``x!`` / ``f()!`` / ``a[0]!`` as a non-null assertion when no
        line break precedes the ``!``. An identifier counts unless it is a
        keyword taking an operand (``return !x``), a ``)`` unless it closes a
        statement condition, and a ``!`` only when it is itself postfix.
        """
        token = self.tokens[index] if index >= 0 else None
        if token is None:
            return False
        if token.kind == "ident":
            return token.value not in _masking._JS_REGEX_PREFIX_KEYWORDS
        if token.value == "!":
            return not token.newline_before and self.ends_operand(index - 1)
        if token.value == ")":
            return self.condition_close != index
        return token.value == "]"

    def expression_may_start(self) -> bool:
        """True when a ``/`` here opens a regex, and a ``<`` a JSX element.

        The same rule ``_js_slash_can_start_regex`` applies per line: after an
        operator or an opening bracket, or after a keyword that takes an
        operand. A ``}`` ends a block far more often than an expression, and a
        ``)`` closing an ``if`` / ``for`` / ``while`` / ``with`` condition ends
        a statement head, so both start an expression too: reading a regex
        there as division would let a quote or backtick inside it swallow the
        code below into a literal while the stream still balances. Template
        and JSX tokens are emitted at their *end*, so a ``/`` after them is
        division, as it is after a TS non-null ``!`` (``x! / y``).
        """
        if not self.tokens:
            return True
        prev = self.tokens[-1]
        if prev.kind == "punct" and prev.value == ")":
            return self.condition_close == len(self.tokens) - 1
        if prev.kind == "punct" and prev.value == "!":
            return not self.ends_operand(len(self.tokens) - 1)
        if prev.kind == "punct":
            return prev.value not in ("]", "++", "--")
        if prev.kind == "ident":
            return prev.value in _masking._JS_REGEX_PREFIX_KEYWORDS
        return False

    def skip_trivia(self) -> None:
        text, n = self.text, len(self.text)
        while self.pos < n:
            ch = text[self.pos]
            if ch in _LINE_TERMINATORS:
                self.newline = True
                self.pos += 1
            elif ch.isspace():
                self.pos += 1
            elif text.startswith("//", self.pos) or (self.pos == 0 and text.startswith("#!")):
                self.pos = self.line_end(self.pos)
            elif text.startswith("/*", self.pos):
                end = text.find("*/", self.pos + 2)
                if end < 0:
                    raise _EsLexError
                if any(c in _LINE_TERMINATORS for c in text[self.pos : end]):
                    self.newline = True
                self.pos = end + 2
            else:
                return

    def opens_condition(self) -> bool:
        """True when a ``(`` here opens a statement condition, ``for await (`` included."""
        tail = [(t.kind, t.value) for t in self.tokens[-2:]]
        if tail[-1:] == [("ident", "await")]:
            return tail[:1] == [("ident", "for")]
        return bool(tail) and tail[-1][0] == "ident" and tail[-1][1] in _CONDITION_KEYWORDS

    def line_end(self, start: int) -> int:
        end = start
        while end < len(self.text) and self.text[end] not in _LINE_TERMINATORS:
            end += 1
        return end

    def lex_code(self, closer: str | None) -> None:
        """Lex until EOF, or until the ``}`` closing a ``${`` / JSX ``{``."""
        stack: list[tuple[str, bool]] = []
        text, n = self.text, len(self.text)
        while True:
            self.skip_trivia()
            if self.pos >= n:
                if closer is not None or stack:
                    raise _EsLexError
                return
            ch = text[self.pos]
            if ch in _CLOSER_FOR:
                condition = self.opens_condition()
                self.emit("punct", ch)
                stack.append((_CLOSER_FOR[ch], condition))
                self.depth += 1
                self.pos += 1
            elif ch in ")]}":
                if not stack:
                    if ch != closer:
                        raise _EsLexError
                    self.pos += 1
                    return
                closer_char, condition = stack.pop()
                if closer_char != ch:
                    raise _EsLexError
                self.depth -= 1
                self.emit("punct", ch)
                if condition and ch == ")":
                    self.condition_close = len(self.tokens) - 1
                self.pos += 1
            elif ch in "'\"":
                self.emit("string", self.read_string(ch))
            elif ch == "`":
                self.read_template()
            elif ch == "/" and self.expression_may_start():
                self.read_regex()
                self.emit("regex", "")
            elif (
                ch == "<"
                and self.jsx
                and self.expression_may_start()
                and not (self.tsx and _masking._TSX_GENERIC_ARROW_RE.match(text, self.pos))
            ):
                self.read_jsx_element()
                self.emit("jsx", "<")
            else:
                self.read_word_or_punctuator(ch)

    def read_word_or_punctuator(self, ch: str) -> None:
        text = self.text
        ident = _IDENT_RE.match(text, self.pos + (ch == "#"))
        if ident is not None:
            self.emit("ident", text[self.pos : ident.end()])
            self.pos = ident.end()
            return
        number = _NUMBER_RE.match(text, self.pos)
        if number is not None:
            self.emit("number", number.group())
            self.pos = number.end()
            return
        for punctuator in _MULTI_CHAR_PUNCTUATORS:
            if text.startswith(punctuator, self.pos):
                if punctuator == "?." and text[self.pos + 2 : self.pos + 3].isdigit():
                    continue
                self.emit("punct", punctuator)
                self.pos += len(punctuator)
                return
        if ch not in _SINGLE_CHAR_PUNCTUATORS:
            raise _EsLexError
        self.emit("punct", ch)
        self.pos += 1

    def read_string(self, quote: str) -> str:
        text, n = self.text, len(self.text)
        start = index = self.pos + 1
        while index < n:
            ch = text[index]
            if ch == "\\":
                index += 3 if text.startswith("\r\n", index + 1) else 2
            elif ch == quote:
                self.pos = index + 1
                return text[start:index]
            elif ch in "\r\n":
                break
            else:
                index += 1
        raise _EsLexError

    def read_template(self) -> None:
        text, n = self.text, len(self.text)
        self.pos += 1
        while self.pos < n:
            ch = text[self.pos]
            if ch == "\\":
                self.pos += 2
            elif ch == "`":
                self.pos += 1
                self.emit("template", "")
                return
            elif text.startswith("${", self.pos):
                self.pos += 2
                self.read_nested_code("${")
            else:
                self.pos += 1
        raise _EsLexError

    def read_nested_code(self, opener: str) -> None:
        """Lex a ``${...}`` substitution or JSX ``{...}`` expression; ``pos`` is past the opener."""
        self.emit("punct", opener)
        self.depth += 1
        self.lex_code("}")
        self.depth -= 1
        self.emit("punct", "}")

    def read_regex(self) -> None:
        text, n = self.text, len(self.text)
        index = self.pos + 1
        in_class = False
        while index < n:
            ch = text[index]
            if ch == "\\":
                index += 2
                continue
            if ch in _LINE_TERMINATORS:
                break
            if ch == "[":
                in_class = True
            elif ch == "]":
                in_class = False
            elif ch == "/" and not in_class:
                flags = _IDENT_RE.match(text, index + 1)
                self.pos = index + 1 if flags is None else flags.end()
                return
            index += 1
        raise _EsLexError

    def skip_jsx_space(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def read_jsx_name(self) -> str:
        name = _JSX_NAME_RE.match(self.text, self.pos)
        assert name is not None  # ``*`` always matches
        self.pos = name.end()
        return name.group()

    def read_jsx_element(self) -> None:
        """Skip one JSX element at ``<``; its ``{...}`` expressions are lexed as code."""
        text = self.text
        self.pos += 1
        self.skip_jsx_space()
        if text.startswith(">", self.pos):
            self.pos += 1
            self.read_jsx_children("")
            return
        name = self.read_jsx_name()
        if not name:
            raise _EsLexError
        while True:
            self.skip_jsx_space()
            if text.startswith("/>", self.pos):
                self.pos += 2
                return
            if text.startswith(">", self.pos):
                self.pos += 1
                self.read_jsx_children(name)
                return
            if text.startswith("{", self.pos):
                self.pos += 1
                self.read_nested_code("{")
                continue
            if not self.read_jsx_name():
                raise _EsLexError
            self.skip_jsx_space()
            if text.startswith("=", self.pos):
                self.pos += 1
                self.skip_jsx_space()
                self.read_jsx_attribute_value()

    def read_jsx_attribute_value(self) -> None:
        text = self.text
        quote = text[self.pos : self.pos + 1]
        if quote in ("'", '"'):
            end = text.find(quote, self.pos + 1)
            if end < 0:
                raise _EsLexError
            self.pos = end + 1
        elif quote == "{":
            self.pos += 1
            self.read_nested_code("{")
        elif quote == "<":
            self.read_jsx_element()
        else:
            raise _EsLexError

    def read_jsx_children(self, name: str) -> None:
        text, n = self.text, len(self.text)
        while self.pos < n:
            if text.startswith("</", self.pos):
                self.pos += 2
                self.skip_jsx_space()
                closing = self.read_jsx_name()
                self.skip_jsx_space()
                if closing != name or not text.startswith(">", self.pos):
                    raise _EsLexError
                self.pos += 1
                return
            if text.startswith("<", self.pos):
                self.read_jsx_element()
            elif text.startswith("{", self.pos):
                self.pos += 1
                self.read_nested_code("{")
            else:
                self.pos += 1
        raise _EsLexError


def _path_is_es_module_source(path: str) -> bool:
    """True when ``path`` is a JS/TS source this module's readers understand."""
    return f".{path.rsplit('.', 1)[-1].lower()}" in _JS_TS_CALL_SITE_SUFFIXES


def _es_module_tokens(text: str, *, path: str) -> tuple[_EsToken, ...] | None:
    """The significant tokens of ``text``, or None when it cannot be lexed.

    Comments are skipped; strings, template literals and regex literals are
    single tokens, so call- or import-shaped text inside them is inert. JSX
    elements are skipped in JSX-capable files, keeping the code inside their
    ``{...}`` expressions. Any anomaly — an unterminated literal or comment,
    an unbalanced bracket, a JSX element that does not close, text nesting so
    deep the tokenizer cannot follow it — returns None, which fails closed.
    """
    suffix = f".{path.rsplit('.', 1)[-1].lower()}"
    lexer = _EsLexer(
        text.removeprefix("﻿"), jsx=suffix in _JSX_CAPABLE_SUFFIXES, tsx=suffix == ".tsx"
    )
    try:
        lexer.lex_code(None)
    except (_EsLexError, RecursionError):
        return None
    return tuple(lexer.tokens)


class _EsBinding(NamedTuple):
    """What one name is bound to: a module ``specifier`` and the ``imported`` name.

    ``imported`` is the exported name, ``"default"``, or ``"*"`` for a
    namespace. A None ``specifier`` is a binding this reader cannot follow
    (``import x = require(...)``, or a name two imports bind).
    """

    specifier: str | None
    imported: str


_UNFOLLOWABLE = _EsBinding(None, "default")


def _token_at(tokens: tuple[_EsToken, ...], index: int) -> _EsToken | None:
    return tokens[index] if 0 <= index < len(tokens) else None


def _is(token: _EsToken | None, kind: str, value: str | None = None) -> TypeGuard[_EsToken]:
    return token is not None and token.kind == kind and (value is None or token.value == value)


def _ident_value(token: _EsToken | None) -> str | None:
    """``token``'s name when it is an identifier, else None."""
    return token.value if _is(token, "ident") else None


def _is_reference_position(tokens: tuple[_EsToken, ...], index: int) -> bool:
    """True when ``tokens[index]`` is a property name after ``.`` / ``?.``."""
    prev = _token_at(tokens, index - 1)
    return _is(prev, "punct") and prev.value in (".", "?.")


def _is_statement_keyword(tokens: tuple[_EsToken, ...], index: int, keyword: str) -> bool:
    """True when ``tokens[index]`` is ``keyword`` at depth 0 and not a property name."""
    token = tokens[index]
    return (
        _is(token, "ident", keyword)
        and token.depth == 0
        and not _is_reference_position(tokens, index)
    )


def _specifier_groups(
    tokens: tuple[_EsToken, ...], open_index: int
) -> tuple[list[list[_EsToken]], int]:
    """The comma-separated groups inside the ``{`` at ``open_index``, and the index after ``}``.

    The tokenizer only yields balanced text, so the matching ``}`` exists.
    """
    groups: list[list[_EsToken]] = [[]]
    depth = tokens[open_index].depth
    index = open_index + 1
    while not (tokens[index].depth == depth and _is(tokens[index], "punct", "}")):
        if _is(tokens[index], "punct", ","):
            groups.append([])
        else:
            groups[-1].append(tokens[index])
        index += 1
    return [group for group in groups if group], index + 1


def _module_specifier_after(tokens: tuple[_EsToken, ...], index: int) -> str | None:
    """The string after a ``from`` at ``index``, or None."""
    source = _token_at(tokens, index + 1)
    if _is(_token_at(tokens, index), "ident", "from") and _is(source, "string"):
        return source.value
    return None


def _named_import_specifier(group: list[_EsToken]) -> list[tuple[str, str]] | None:
    """``[(imported, local)]`` for one ``{ ... }`` import specifier.

    Empty for a type-only specifier, which binds no runtime name; None when the
    group is not a specifier this reader understands.
    """
    values = [token.value for token in group]
    if len(values) in (2, 4) and values[0] == "type" and (len(values) == 2 or values[2] == "as"):
        return []
    if len(values) == 1 and group[0].kind == "ident":
        return [(values[0], values[0])]
    if (
        len(values) == 3
        and values[1] == "as"
        and group[2].kind == "ident"
        and group[0].kind in ("ident", "string")
    ):
        return [(values[0], values[2])]
    return None


def _import_statement_bindings(
    tokens: tuple[_EsToken, ...], index: int
) -> list[tuple[str, _EsBinding]] | None:
    """The ``(local, binding)`` pairs of the import whose clause starts at ``index``."""
    first = _token_at(tokens, index)
    if first is None:
        return None
    if _is(first, "string") or (first.kind == "punct" and first.value in ("(", ".", "?.")):
        return []  # a side-effect import, ``import(...)`` or ``import.meta``
    after = _token_at(tokens, index + 1)
    if (
        _is(first, "ident", "type")
        and after is not None
        and (after.value in ("{", "*") or (after.kind == "ident" and after.value != "from"))
    ):
        return []  # ``import type ...`` binds no runtime name
    found: list[tuple[str, str]] = []
    if first.kind == "ident":
        if _is(after, "punct", "="):
            return [(first.value, _UNFOLLOWABLE)]  # TS ``import x = require(...)``
        found.append((first.value, "default"))
        index += 1
        if not _is(after, "punct", ","):
            specifier = _module_specifier_after(tokens, index)
            return None if specifier is None else _bound(found, specifier)
        index += 1
    clause = _token_at(tokens, index)
    if _is(clause, "punct", "*"):
        local = _ident_value(_token_at(tokens, index + 2))
        if not _is(_token_at(tokens, index + 1), "ident", "as") or local is None:
            return None
        found.append((local, "*"))
        index += 3
    elif _is(clause, "punct", "{"):
        groups, index = _specifier_groups(tokens, index)
        for group in groups:
            pairs = _named_import_specifier(group)
            if pairs is None:
                return None
            found.extend((local_name, imported) for imported, local_name in pairs)
    else:
        return None
    specifier = _module_specifier_after(tokens, index)
    return None if specifier is None else _bound(found, specifier)


def _bound(found: list[tuple[str, str]], specifier: str) -> list[tuple[str, _EsBinding]]:
    return [(local, _EsBinding(specifier, imported)) for local, imported in found]


def _es_import_bindings(tokens: tuple[_EsToken, ...]) -> dict[str, _EsBinding] | None:
    """Each runtime name the file's top-level ``import`` statements bind.

    Type-only imports bind nothing. A name bound twice is held unfollowable —
    ES rejects the duplicate, so neither binding is trusted. An ``import``
    statement this reader cannot read makes the whole table unreadable (None),
    because a name it would have bound could otherwise look unbound.
    """
    bindings: dict[str, _EsBinding] = {}
    for index in range(len(tokens)):
        if not _is_statement_keyword(tokens, index, "import"):
            continue
        statement = _import_statement_bindings(tokens, index + 1)
        if statement is None:
            return None
        for local, binding in statement:
            bindings[local] = _UNFOLLOWABLE if local in bindings else binding
    return bindings


class _EsExports(NamedTuple):
    """One module's export table.

    ``local`` maps an exported name to the local name the module binds it to;
    ``reexport`` maps an exported name to the binding an ``export { } from``
    forwards; ``namespace`` holds ``export * as ns`` names; ``star`` holds
    ``export * from`` specifiers. ``opaque`` marks a module exporting names
    through a form this reader does not list (destructuring, ``enum``,
    ``declare``, ``export =``, ...), so absence of a name proves nothing.
    """

    local: dict[str, str]
    reexport: dict[str, _EsBinding]
    namespace: frozenset[str]
    star: tuple[str, ...]
    opaque: bool


# Statement-only keywords: none can continue an expression at depth 0, so a
# variable declaration has ended when one appears.
_STATEMENT_KEYWORDS = frozenset(
    {
        "break",
        "const",
        "continue",
        "do",
        "export",
        "for",
        "if",
        "let",
        "return",
        "switch",
        "throw",
        "try",
        "var",
        "while",
    }
)


def _declaration_name(tokens: tuple[_EsToken, ...], index: int) -> str | None:
    """The name a ``function`` / ``class`` declaration at ``index`` binds, if any."""
    if _is(_token_at(tokens, index), "ident", "async"):
        index += 1
    if _is(_token_at(tokens, index), "ident", "function"):
        index += 2 if _is(_token_at(tokens, index + 1), "punct", "*") else 1
    else:
        if _is(_token_at(tokens, index), "ident", "abstract"):
            index += 1
        if not _is(_token_at(tokens, index), "ident", "class"):
            return None
        index += 1
    name = _ident_value(_token_at(tokens, index))
    return None if name in ("extends", "implements") else name


def _variable_declaration_is_single(tokens: tuple[_EsToken, ...], index: int) -> bool:
    """True when the declaration naming ``tokens[index]`` declares nothing else.

    Scans to the end of the statement — a ``;`` or a statement keyword at depth
    0 — and reports False on a depth-0 ``,`` that would start another
    declarator. A comma in an expression statement after an ASI boundary also
    reads as one, which only marks the module opaque.
    """
    for token in tokens[index + 1 :]:
        if token.depth:
            continue
        if _is(token, "punct", ";") or (
            token.kind == "ident" and token.value in _STATEMENT_KEYWORDS
        ):
            return True
        if _is(token, "punct", ","):
            return False
    return True


def _default_export_name(tokens: tuple[_EsToken, ...], index: int) -> str | None:
    """The local name ``export default`` at ``index`` exports, if it names one."""
    declared = _declaration_name(tokens, index)
    if declared is not None:
        return declared
    name = _ident_value(_token_at(tokens, index))
    after = _token_at(tokens, index + 1)
    if after is None or _is(after, "punct", ";"):
        return name
    if after.newline_before and after.kind == "ident" and after.value not in ("in", "instanceof"):
        return name
    return None


def _export_clause(
    tokens: tuple[_EsToken, ...], index: int, exports: _EsExports
) -> _EsExports | None:
    """``exports`` plus the ``export { ... } [from 's']`` clause at ``index``."""
    groups, after = _specifier_groups(tokens, index)
    specifier = _module_specifier_after(tokens, after)
    for group in groups:
        values = [token.value for token in group]
        if values[0] == "type" and len(values) in (2, 4):
            continue
        if len(values) == 1:
            local, exported = values[0], values[0]
        elif len(values) == 3 and values[1] == "as":
            local, exported = values[0], values[2]
        else:
            return None
        if specifier is not None:
            exports.reexport[exported] = _EsBinding(specifier, local)
        elif group[0].kind != "ident":
            return None
        else:
            exports.local[exported] = local
    if specifier is None and _is(_token_at(tokens, after), "ident", "from"):
        return None
    return exports


def _es_module_exports(tokens: tuple[_EsToken, ...]) -> _EsExports | None:
    """The export table of a module's top-level ``export`` statements.

    Type-only exports (``export type``, ``export interface``, ``{ type x }``)
    are skipped. A malformed ``export { }`` or ``export *`` makes the table
    unreadable (None), because a star lookup must be able to prove a name
    absent.
    """
    exports = _EsExports({}, {}, frozenset(), (), False)
    for index in range(len(tokens)):
        if not _is_statement_keyword(tokens, index, "export"):
            continue
        head = _token_at(tokens, index + 1)
        if head is None:
            return None
        declared = _declaration_name(tokens, index + 1)
        if declared is not None:
            exports.local[declared] = declared
        elif _is(head, "ident", "default"):
            default = _default_export_name(tokens, index + 2)
            if default is not None:
                exports.local["default"] = default
        elif head.kind == "ident" and head.value in ("const", "let", "var"):
            name = _ident_value(_token_at(tokens, index + 2))
            if name is not None and name != "enum":
                exports.local[name] = name
                if not _variable_declaration_is_single(tokens, index + 2):
                    exports = exports._replace(opaque=True)
            else:
                exports = exports._replace(opaque=True)
        elif _is(head, "punct", "{"):
            clause = _export_clause(tokens, index + 1, exports)
            if clause is None:
                return None
            exports = clause
        elif _is(head, "punct", "*"):
            star = _star_export(tokens, index + 2, exports)
            if star is None:
                return None
            exports = star
        elif not (head.kind == "ident" and head.value in ("type", "interface", "as")):
            exports = exports._replace(opaque=True)
    return exports


def _star_export(
    tokens: tuple[_EsToken, ...], index: int, exports: _EsExports
) -> _EsExports | None:
    """``exports`` plus the ``export * [as ns] from 's'`` whose tail starts at ``index``."""
    if _is(_token_at(tokens, index), "ident", "as"):
        name = _ident_value(_token_at(tokens, index + 1))
        if name is None or _module_specifier_after(tokens, index + 2) is None:
            return None
        return exports._replace(namespace=exports.namespace | {name})
    specifier = _module_specifier_after(tokens, index)
    if specifier is None:
        return None
    return exports._replace(star=(*exports.star, specifier))


# Keywords whose next identifier is a binding of that name.
_DECLARING_KEYWORDS = frozenset(
    {"class", "const", "enum", "function", "let", "module", "namespace", "using", "var"}
)
_PATTERN_KEYWORDS = frozenset({"const", "let", "using", "var"})
# ``(`` groups that hold an expression, not parameters, even when ``{`` follows.
_PARAMETER_EXCLUDING_KEYWORDS = _CONDITION_KEYWORDS | {"switch"}


def _declaration_pattern_names(tokens: tuple[_EsToken, ...], index: int) -> set[str]:
    """Identifiers a ``const`` / ``let`` / ``var`` / ``using`` at ``index`` may bind.

    Reads every declarator: pattern mode collects identifiers (defaults and
    keys included, which only over-reports), a depth-0 ``=`` switches to the
    initializer, and a depth-0 ``,`` back to the next pattern. The statement
    ends at ``;``, ``in`` / ``of``, a statement keyword, or a closer below the
    keyword's own depth.
    """
    base = tokens[index].depth
    pattern = True
    names: set[str] = set()
    for offset, token in enumerate(tokens[index + 1 :], start=index + 1):
        if token.depth < base:
            break
        if token.depth == base:
            if _is(token, "punct", ";") or (
                token.kind == "ident"
                and (token.value in ("in", "of") or token.value in _STATEMENT_KEYWORDS)
            ):
                break
            if _is(token, "punct", "="):
                pattern = False
                continue
            if _is(token, "punct", ","):
                pattern = True
                continue
        if pattern and token.kind == "ident" and not _is_reference_position(tokens, offset):
            names.add(token.value)
    return names


def _parameter_group_names(tokens: tuple[_EsToken, ...], close_index: int) -> set[str]:
    """Identifiers inside the ``( ... )`` closing at ``close_index`` if it is a parameter list.

    It is one when ``)`` is followed by ``=>``, ``{`` or a ``:`` return type
    and the ``(`` is not a condition (``if (record) {``). A call followed by a
    ternary ``:`` also reads as one, which only over-reports.
    """
    after = _token_at(tokens, close_index + 1)
    if not (_is(after, "punct") and after.value in ("=>", "{", ":")):
        return set()
    depth = tokens[close_index].depth
    open_index = close_index - 1
    while not (tokens[open_index].depth == depth and _is(tokens[open_index], "punct", "(")):
        open_index -= 1
    if _ident_value(_token_at(tokens, open_index - 1)) in _PARAMETER_EXCLUDING_KEYWORDS:
        return set()
    return _parameter_pattern_names(tokens, open_index, close_index)


def _parameter_pattern_names(
    tokens: tuple[_EsToken, ...], open_index: int, close_index: int
) -> set[str]:
    """Identifiers the binding patterns strictly between ``open_index`` and ``close_index`` bind.

    A ``=`` starts a default and a ``:`` a type annotation, each skipped to the
    next ``,`` or closer at its depth (a type also to a ``=`` that starts its
    default), so ``value = record()`` and ``value: typeof record`` bind only
    ``value``. Inside a ``{`` pattern a ``:`` follows a key, which is not
    bound, and the renamed binding after it is read. A ``,`` inside type
    arguments ends a type early, which only over-reports.
    """
    openers = {tokens[open_index].depth + 1: "("}
    skipping: tuple[int, bool] | None = None  # (depth, still a type annotation)
    names: set[str] = set()
    for offset in range(open_index + 1, close_index):
        token = tokens[offset]
        if skipping is not None:
            depth, annotation = skipping
            if token.depth > depth:
                continue
            if token.depth == depth and not _is(token, "punct", ","):
                if annotation and _is(token, "punct", "="):
                    skipping = (depth, False)
                continue
            skipping = None
        is_object = openers.get(token.depth) == "{"
        if token.kind == "ident":
            if not (is_object and _is(_token_at(tokens, offset + 1), "punct", ":")):
                names.add(token.value)
        elif _is(token, "punct") and token.value in ("(", "[", "{"):
            openers[token.depth + 1] = token.value
        elif _is(token, "punct", "="):
            skipping = (token.depth, False)
        elif _is(token, "punct", ":") and not is_object:
            skipping = (token.depth, True)
    return names


def _es_rebound_names(tokens: tuple[_EsToken, ...], names: frozenset[str]) -> frozenset[str]:
    """The ``names`` the file binds anywhere besides its imports.

    File-wide and order-insensitive, like ``_module_scope_rebound_names``, and
    deliberately over-inclusive: an import binding proves a callee only while
    the name still holds it, and every error this detector can make reports a
    name that was not rebound, which only fails a call closed. A name counts
    as rebound after a declaring keyword (``const``, ``function``, ``class``,
    ...), anywhere in a declaration pattern, in a parameter list (a ``catch``
    binding included), and as a lone arrow parameter (``record =>``). A plain
    reference such as ``const result = record()`` is not a binding.
    """
    if not names:
        return frozenset()
    bound: set[str] = set()
    for index, token in enumerate(tokens):
        if token.kind == "ident" and token.value in _DECLARING_KEYWORDS:
            following = _token_at(tokens, index + 1)
            if token.value == "function" and _is(following, "punct", "*"):
                following = _token_at(tokens, index + 2)
            if _is(following, "ident"):
                bound.add(following.value)
            if token.value in _PATTERN_KEYWORDS:
                bound |= _declaration_pattern_names(tokens, index)
        elif _is(token, "ident", "import") and _is(_token_at(tokens, index + 2), "punct", "="):
            # A TS ``import alias = ...`` anywhere — at depth 0 it is also an
            # unfollowable import, inside a ``namespace`` only a shadow.
            alias = _ident_value(_token_at(tokens, index + 1))
            bound |= {alias} if alias is not None else set()
        elif _is(token, "punct", ")"):
            bound |= _parameter_group_names(tokens, index)
        elif token.kind == "ident" and _is(_token_at(tokens, index + 1), "punct", "=>"):
            bound.add(token.value)
    return frozenset(bound & names)


_ASSIGNMENT_OPERATORS = frozenset(
    {"=", "+=", "-=", "*=", "/=", "%=", "**=", "<<=", ">>=", ">>>=", "&=", "|=", "^="}
    | {"&&=", "||=", "??="}
)
_VARIABLE_KEYWORDS = frozenset({"const", "let", "using", "var"})


def _destructuring_target_names(tokens: tuple[_EsToken, ...], close_index: int) -> set[str]:
    """Identifiers in the ``[...]`` / ``{...}`` closing at ``close_index`` if it is assigned to.

    It is an assignment target when the closer is followed by ``=`` or by a
    ``for`` head's ``of`` / ``in``, and the group is not a ``const`` / ``let``
    / ``var`` pattern, which declares a new binding instead.
    """
    after = _token_at(tokens, close_index + 1)
    if not (_is(after, "punct", "=") or _ident_value(after) in ("of", "in")):
        return set()
    depth = tokens[close_index].depth
    opener = "[" if tokens[close_index].value == "]" else "{"
    open_index = close_index - 1
    while not (tokens[open_index].depth == depth and _is(tokens[open_index], "punct", opener)):
        open_index -= 1
    if _ident_value(_token_at(tokens, open_index - 1)) in _VARIABLE_KEYWORDS:
        return set()
    return {
        token.value
        for offset, token in enumerate(tokens[open_index + 1 : close_index], start=open_index + 1)
        if token.kind == "ident" and not _is_reference_position(tokens, offset)
    }


# Words a type asserted on an assignment target stops at: another assertion,
# or the ``of`` / ``in`` of a ``for`` head.
_TARGET_TYPE_STOP_WORDS = frozenset({"as", "in", "instanceof", "of", "satisfies"})
_TARGET_TYPE_PUNCTUATORS = frozenset(
    {".", "|", "&", "=>", "(", ")", "[", "]", "{", "}", "?", ":", "<", ">", ">>", ">>>"}
)


def _es_asserted_type_end(tokens: tuple[_EsToken, ...], index: int) -> tuple[int, bool]:
    """The last index of the type after the ``as`` / ``satisfies`` at ``index``, and a split flag.

    The type runs over every token nested deeper than the keyword and, at its
    depth, over words, literals and type punctuators, with ``<`` / ``>``
    counted so a ``,`` inside type arguments stays in it. The flag is True when
    a ``>=`` / ``>>=`` / ``>>>=`` closes those arguments: its ``=`` then
    assigns to the asserted target (``record as Set<Set<V>>= other``).
    """
    depth = tokens[index].depth
    angle = 0
    end = index
    for offset in range(index + 1, len(tokens)):
        token = tokens[offset]
        if token.depth > depth:
            end = offset
            continue
        if token.depth < depth:
            break
        if token.kind == "punct":
            if angle and token.value in _SPLIT_ANGLE_PUNCTUATORS:
                return end, True
            if token.value not in _TARGET_TYPE_PUNCTUATORS and not (angle and token.value == ","):
                break
            angle = max(0, angle + _ANGLE_DELTA.get(token.value, 0))
        elif token.kind == "ident" and token.value in _TARGET_TYPE_STOP_WORDS:
            break
        end = offset
    return end, False


def _es_angle_assertion_start(tokens: tuple[_EsToken, ...], close: int) -> int | None:
    """The index of the ``<`` opening a ``<T>`` assertion whose ``>`` is at ``close``."""
    depth = tokens[close].depth
    count = 0
    for offset in range(close, -1, -1):
        token = tokens[offset]
        if token.depth < depth or _is(token, "punct", ";"):
            return None
        if token.depth == depth and token.kind == "punct":
            count -= _ANGLE_DELTA.get(token.value, 0)
            if count == 0:
                return offset
    return None


def _es_target_extent(tokens: tuple[_EsToken, ...], index: int) -> tuple[int, int, bool]:
    """The widest ``(start, end)`` around ``tokens[index]`` that is still that binding, and a flag.

    A TS non-null ``!``, an ``as`` / ``satisfies`` assertion, a prefix
    ``<T>`` assertion and enclosing parentheses all leave the binding the
    assignment target, in any combination (``((record as T)!) = other``,
    PRRT_kwDOSJAM6s6vQACC). The flag is ``_es_asserted_type_end``'s split.
    """
    start = end = index
    while True:
        before, after = _token_at(tokens, start - 1), _token_at(tokens, end + 1)
        opener = (
            _es_angle_assertion_start(tokens, start - 1)
            if _is(before, "punct") and before.value in (">", ">>", ">>>")
            else None
        )
        if _is(after, "punct", "!") and not after.newline_before:
            end += 1
        elif _ident_value(after) in ("as", "satisfies"):
            end, split = _es_asserted_type_end(tokens, end + 1)
            if split:
                return start, end, True
        elif opener is not None:
            start = opener
        elif _is(before, "punct", "(") and _is(after, "punct", ")"):
            start, end = start - 1, end + 1
        else:
            return start, end, False


def _es_assigned_names(tokens: tuple[_EsToken, ...], names: frozenset[str]) -> frozenset[str]:
    """The ``names`` the file assigns to anywhere, not counting their declarations.

    Read in the module that *defines* a callee: an ES export is a live
    binding, so ``export function record`` followed by ``[record] = [other]``
    (or ``(record) = other``, ``({ record } = o)``, ``for (record of xs)``,
    ``record++``, or an assignment inside any function) leaves importers
    calling something other than the edited definition. The target may be
    wrapped in parentheses and TS assertions under any assignment operator
    (``_es_target_extent``). File-wide and over-inclusive like
    ``_es_rebound_names``: an inner variable of the same name that is assigned
    also counts, which only fails the callee closed. A declaration initializer
    (``const record = ...``) and a declaration pattern are not assignments.
    """
    if not names:
        return frozenset()
    assigned: set[str] = set()
    for index, token in enumerate(tokens):
        if token.kind == "ident" and not _is_reference_position(tokens, index):
            start, end, split = _es_target_extent(tokens, index)
            before = _token_at(tokens, start - 1)
            after = _token_at(tokens, end + 1)
            operator = _is(after, "punct") and after.value in _ASSIGNMENT_OPERATORS
            if (
                split
                or (operator and _ident_value(before) not in _VARIABLE_KEYWORDS)
                or any(
                    _is(near, "punct") and near.value in ("++", "--") for near in (before, after)
                )
                or (_ident_value(after) in ("of", "in") and _is(before, "punct", "("))
            ):
                assigned.add(token.value)
        elif _is(token, "punct") and token.value in ("]", "}"):
            assigned |= _destructuring_target_names(tokens, index)
    return frozenset(assigned & names)


def _es_signature_head(tokens: tuple[_EsToken, ...], index: int) -> bool:
    """True when the identifier at ``index`` heads a parameter list, not a call.

    ``name( ... )`` followed by ``{``, ``:`` or ``=>`` is a method, accessor,
    object-literal method or interface signature (``record(v: number) {``,
    ``get size() {``, ``flush(): void;``). A call followed by a ternary ``:``
    also reads as one, which only fails it closed.
    """
    opener = _token_at(tokens, index + 1)
    if not _is(opener, "punct", "("):
        return False
    close = index + 2
    while not (tokens[close].depth == opener.depth and _is(tokens[close], "punct", ")")):
        close += 1
    after = _token_at(tokens, close + 1)
    return _is(after, "punct") and after.value in ("=>", "{", ":")


def _es_line_non_call_names(tokens: tuple[_EsToken, ...], line: int) -> frozenset[str]:
    """Names some occurrence on ``line`` does not use as a bare callee.

    A property after ``.`` / ``?.`` is a member call even when the dot ends the
    line above (``getClient().`` then ``record(v)``), which the line-based
    callee scan reads as bare; a name heading a parameter list is a definition
    (``_es_signature_head``). A bare ref to any such name fails closed.
    """
    return frozenset(
        token.value
        for index, token in enumerate(tokens)
        if token.line == line
        and token.kind == "ident"
        and (_is_reference_position(tokens, index) or _es_signature_head(tokens, index))
    )


def _es_definition_heads(tokens: tuple[_EsToken, ...], name: str) -> list[_EsToken]:
    """Every token heading a definition of ``name``, at any depth.

    A head follows a declaring keyword (``function`` / ``function*``,
    ``class``, ``const``, ...) or heads a parameter list (a method, accessor
    or signature). The callee's file must hold exactly one at depth 0, the
    only head inside the line-based span, for that span to be the definition
    importers reach. A function expression initializing a declaration of the
    same name (``const record = function record() {}``) only binds that name
    inside itself, so it is the declaration's head, not a second one.
    """
    heads: list[_EsToken] = []
    declared: int | None = None
    for index, token in enumerate(tokens):
        if not _is(token, "ident", name) or _is_reference_position(tokens, index):
            continue
        keyword = index - 1
        if _is(tokens[keyword], "punct", "*") and _is(
            _token_at(tokens, index - 2), "ident", "function"
        ):
            keyword = index - 2
        before = _token_at(tokens, keyword)
        if _ident_value(before) == "function" and (
            _es_overload_signature(tokens, index)
            or (declared is not None and _es_initializes(tokens, declared, keyword))
        ):
            continue
        if _ident_value(before) in _DECLARING_KEYWORDS or _es_signature_head(tokens, index):
            heads.append(token)
            declared = index if _ident_value(before) in _VARIABLE_KEYWORDS else None
    return heads


def _es_initializes(tokens: tuple[_EsToken, ...], declared: int, function: int) -> bool:
    """True when the ``function`` at ``function`` is the initializer of the variable at ``declared``.

    The declaration's first ``=`` at its own depth must directly precede the
    ``function`` keyword (or its ``async``). A ``=`` inside a type annotation
    comes first and fails closed, as does a later declarator
    (``const record = 1, other = function record() {}``).
    """
    depth = tokens[declared].depth
    assign = function - (2 if _is(_token_at(tokens, function - 1), "ident", "async") else 1)
    for index in range(declared + 1, function):
        if tokens[index].depth == depth and _is(tokens[index], "punct", "="):
            return index == assign
    return False


def _es_overload_signature(tokens: tuple[_EsToken, ...], index: int) -> bool:
    """True when the ``function`` named at ``index`` is a body-less TS signature.

    An overload (``function record(v: string): void;``) ends at a ``;`` before
    any ``{`` at its own depth; parameters nest deeper and a return type holds
    no ``;`` there, so an implementation reaches its body's ``{`` first. A
    ``{`` in type parameters or an object return type also reads as a body,
    and a signature ending without a ``;`` keeps its head too; each only
    fails closed.
    """
    depth = tokens[index].depth
    for token in tokens[index + 1 :]:
        if token.depth < depth:
            break
        if token.depth == depth and _is(token, "punct") and token.value in ("{", ";"):
            return token.value == ";"
    return False


# Words that may lead a definition's statement or class member, before its name;
# an ``enum`` / ``namespace`` / ``module`` is led there to be refused as a whole.
_DEFINITION_MODIFIERS = frozenset(
    {
        "abstract",
        "accessor",
        "async",
        "await",
        "class",
        "const",
        "declare",
        "default",
        "enum",
        "export",
        "function",
        "get",
        "let",
        "module",
        "namespace",
        "override",
        "private",
        "protected",
        "public",
        "readonly",
        "set",
        "static",
        "using",
        "var",
    }
)
# Words after which a ``{`` opens a type or heritage, not a body (``): v is {``).
_TYPE_PREFIX_WORDS = frozenset(
    {"asserts", "class", "extends", "implements", "infer", "is", "keyof", "readonly", "typeof"}
)
# Operator words that continue an expression past a line break.
_CONTINUING_WORDS = frozenset({"as", "in", "instanceof", "satisfies"})
# How each angle punctuator moves a type-argument count; ``>=`` and its
# compound forms would split a ``>`` from an ``=`` and fail closed instead.
_ANGLE_DELTA = {"<": 1, ">": -1, ">>": -2, ">>>": -3}
_SPLIT_ANGLE_PUNCTUATORS = frozenset({">=", ">>=", ">>>="})


def _es_closer(tokens: tuple[_EsToken, ...], open_index: int) -> int:
    """The index of the closer matching the opener at ``open_index``.

    The tokenizer only yields balanced text and emits an opener and its closer
    at the same depth, with everything between them deeper.
    """
    index = open_index + 1
    while tokens[index].depth != tokens[open_index].depth:
        index += 1
    return index


def _es_ends_type(token: _EsToken) -> bool:
    """True when ``token`` may end a type or heritage, so a ``{`` after it opens a body."""
    if token.kind == "punct":
        return token.value in (")", "]", "}", ">")
    return token.kind != "ident" or token.value not in _TYPE_PREFIX_WORDS


def _es_type_end(
    tokens: tuple[_EsToken, ...], index: int, stops: frozenset[str], *, commas: bool = False
) -> int | None:
    """The index of the first ``stops`` punctuator (or ``;``) ending the type at ``index``.

    The type runs at ``tokens[index]``'s depth, where ``<`` / ``>`` nest type
    arguments; a ``{`` in ``stops`` ends it only after a token that may end a
    type, so an object type is skipped. A top-level ``,`` (unless ``commas``),
    a ``>=`` that splits a closing ``>``, a closer, a line break before a word
    that would start a new statement, and the end of the text all fail closed.
    """
    depth = tokens[index - 1].depth
    angle = 0
    for offset in range(index, len(tokens)):
        token = tokens[offset]
        if token.depth < depth or (
            token.kind == "punct" and token.value in _SPLIT_ANGLE_PUNCTUATORS
        ):
            return None
        if token.depth > depth:
            continue
        if token.kind == "punct" and token.value in _ANGLE_DELTA:
            angle += _ANGLE_DELTA[token.value]
            if angle < 0:
                return None
            continue
        if angle:
            continue
        previous = tokens[offset - 1]
        if token.kind == "punct" and token.value in stops | {";"}:
            if token.value != "{" or _es_ends_type(previous):
                return offset
        elif (_is(token, "punct", ",") and not commas) or (
            token.newline_before
            and token.kind == "ident"
            and token.value not in ("extends", "implements")
            and _es_ends_type(previous)
        ):
            return None
    return None


def _es_after_type_parameters(tokens: tuple[_EsToken, ...], index: int) -> int | None:
    """The index after the ``<...>`` at ``index``, ``index`` itself when none opens there."""
    if not _is(_token_at(tokens, index), "punct", "<"):
        return index
    angle = 0
    for offset in range(index, len(tokens)):
        token = tokens[offset]
        if token.depth < tokens[index].depth or token.value in _SPLIT_ANGLE_PUNCTUATORS:
            return None
        if token.depth == tokens[index].depth and token.kind == "punct":
            angle += _ANGLE_DELTA.get(token.value, 0)
            if token.value == ";" or angle < 0:
                return None
            if angle == 0:
                return offset + 1
    return None


def _es_body_close(tokens: tuple[_EsToken, ...], index: int, *, heritage: bool) -> int | None:
    """The index of the ``}`` closing the body of the definition named at ``index``.

    A function or method reads optional type parameters, its parameter list
    and an optional return type; a class (``heritage``) reads type parameters,
    ``extends`` and ``implements`` up to its body. The body is the first ``{``
    at the head's depth that follows a token able to end that header; a header
    reaching a ``;`` first (an overload or abstract signature), or anything
    else this reader cannot place, has no body here and fails closed.
    """
    stops = frozenset({"{", "="})
    if heritage:
        body = _es_type_end(tokens, index + 1, stops, commas=True)
    else:
        params = _es_after_type_parameters(tokens, index + 1)
        if params is None or not _is(_token_at(tokens, params), "punct", "("):
            return None
        after = _token_at(tokens, _es_closer(tokens, params) + 1)
        if _is(after, "punct", "{"):
            body = _es_closer(tokens, params) + 1
        elif _is(after, "punct", ":"):
            body = _es_type_end(tokens, _es_closer(tokens, params) + 2, stops)
        else:
            return None
    if body is None or not _is(tokens[body], "punct", "{"):
        return None
    return _es_closer(tokens, body)


def _es_line_break_ends(previous: _EsToken, token: _EsToken) -> bool | None:
    """Whether a line break between ``previous`` and ``token`` ends the statement.

    True where automatic semicolon insertion applies — ``previous`` ends an
    operand and ``token`` cannot continue it (a word other than an operator
    word, or a literal) — False where the expression continues, and None where
    the reading depends on more than these two tokens: a ``(`` / ``[`` /
    template after a ``}`` that may close an arrow body or a function
    expression, a ``{`` / ``++`` / ``--`` / ``!`` / ``~`` / ``@`` / ``...``
    starting the line, or a postfix ``++`` / ``--`` / ``!`` before the break.
    """
    if previous.kind == "punct":
        if previous.value in ("++", "--", "!"):
            return None
        if previous.value not in (")", "]", "}"):
            return False
    elif previous.kind == "ident" and previous.value in _masking._JS_REGEX_PREFIX_KEYWORDS:
        return False
    if token.kind == "ident":
        return token.value not in _CONTINUING_WORDS
    if token.kind == "template" or (token.kind == "punct" and token.value in ("(", "[")):
        return None if _is(previous, "punct", "}") else False
    if token.kind != "punct":
        return True
    if token.value in ("{", "++", "--", "!", "~", "@", "..."):
        return None
    return False


def _es_expression_end(tokens: tuple[_EsToken, ...], index: int) -> int | None:
    """The index of the last token of the expression statement running from ``index``.

    It ends at a ``;`` at its depth, before a line break automatic semicolon
    insertion ends it at (see ``_es_line_break_ends``), before a closer of the
    enclosing block, or at the end of the text. A ``,`` at its depth starts
    another declarator, and an ambiguous line break cannot be placed; both
    fail closed.
    """
    depth = tokens[index - 1].depth
    for offset in range(index, len(tokens)):
        token = tokens[offset]
        if token.depth < depth:
            return offset - 1
        if token.depth > depth:
            continue
        if token.newline_before:
            ends = _es_line_break_ends(tokens[offset - 1], token)
            if ends is None:
                return None
            if ends:
                return offset - 1
        if _is(token, "punct", ";"):
            return offset
        if _is(token, "punct", ","):
            return None
    return len(tokens) - 1


def _es_variable_end(tokens: tuple[_EsToken, ...], index: int) -> int | None:
    """The index of the last token of the variable declarator named at ``index``.

    An optional definite-assignment ``!`` and type annotation, then an
    initializer that must be callable-shaped — a ``function`` or ``class``
    expression, or an arrow — since any other value (``make()``) is no
    definition this reader can place. A generic arrow's type parameters and
    an arrow's return type are types, so a ``,`` or ``{`` in them is not the
    declarator's own; the rest is an expression statement
    (``_es_expression_end``).
    """
    offset = index + 1
    if _is(_token_at(tokens, offset), "punct", "!"):
        offset += 1
    if _is(_token_at(tokens, offset), "punct", ":"):
        found = _es_type_end(tokens, offset + 1, frozenset({"="}))
        if found is None:
            return None
        offset = found
    if not _is(_token_at(tokens, offset), "punct", "="):
        return None
    offset += 1
    following = _token_at(tokens, offset + 1)
    if _is(_token_at(tokens, offset), "ident", "async") and (
        _is(following, "punct") or _is(following, "ident", "function")
    ):
        offset += 1
    found = _es_after_type_parameters(tokens, offset)
    if found is None:
        return None
    first = _token_at(tokens, found)
    if _is(first, "punct", "("):
        arrow = _es_closer(tokens, found) + 1
        if _is(_token_at(tokens, arrow), "punct", ":"):
            arrow = _es_type_end(tokens, arrow + 1, frozenset({"=>"})) or arrow
        if not _is(_token_at(tokens, arrow), "punct", "=>"):
            return None
        found = arrow
    elif _ident_value(first) not in ("function", "class") and not (
        _is(first, "ident") and _is(_token_at(tokens, found + 1), "punct", "=>")
    ):
        return None
    return _es_expression_end(tokens, found + 1)


def _es_definition_span(tokens: tuple[_EsToken, ...], head: _EsToken) -> tuple[int, int] | None:
    """``(first_line, last_line)`` of the definition ``head`` names, read from bracket depth.

    Never from indentation, which says nothing about where JS/TS code ends
    (PRRT_kwDOSJAM6s6vQABw). The span opens at the definition's first
    modifier (``export``, ``async``, ``static``, ...) and closes at the ``}``
    closing a function, class, method or object-method body, or at the end of
    a variable's declarator — a ``;``, or a line break automatic semicolon
    insertion ends it at — with a trailing ``;`` (or a member's ``,``) kept.
    A statement sharing the first or last line, a declarator list, an enum
    or namespace, and every end this reader cannot place fail closed (None),
    since a diff hunk on a shared line may change either statement.
    """
    index = next(offset for offset, token in enumerate(tokens) if token is head)
    start = index
    while (
        start > 0
        and not tokens[start].newline_before
        and tokens[start - 1].depth == head.depth
        and (
            _ident_value(tokens[start - 1]) in _DEFINITION_MODIFIERS
            or _is(tokens[start - 1], "punct", "*")
        )
    ):
        start -= 1
    if start > 0 and not tokens[start].newline_before:
        return None
    leading = {token.value for token in tokens[start:index] if token.kind == "ident"}
    end: int | None
    if leading & _VARIABLE_KEYWORDS:
        end = _es_variable_end(tokens, index)
    elif "class" in leading:
        end = _es_body_close(tokens, index, heritage=True)
    elif "function" in leading or _es_signature_head(tokens, index):
        end = _es_body_close(tokens, index, heritage=False)
    else:
        return None
    if end is None:
        return None
    after = _token_at(tokens, end + 1)
    if _is(after, "punct") and after.value in (";", ",") and not _is(tokens[end], "punct", ";"):
        end += 1
    after = _token_at(tokens, end + 1)
    if after is not None and not after.newline_before:
        return None
    return tokens[start].line, tokens[end].line


def _es_module_definition_span(tokens: tuple[_EsToken, ...], name: str) -> tuple[int, int] | None:
    """The span of the one module-scope definition of ``name``, or None.

    The file must hold exactly one head for the name at depth 0, its span
    must be placeable (``_es_definition_span``), and no other head of the
    name — a nested ``function record`` in its own body included — may share
    the span's lines, so an edit there is unambiguously an edit to the one
    definition importers reach.
    """
    heads = _es_definition_heads(tokens, name)
    module_heads = [head for head in heads if head.depth == 0]
    if len(module_heads) != 1:
        return None
    span = _es_definition_span(tokens, module_heads[0])
    if span is None or [head for head in heads if span[0] <= head.line <= span[1]] != module_heads:
        return None
    return span


def _es_chained_member_refs(tokens: tuple[_EsToken, ...], line: int) -> frozenset[tuple[str, str]]:
    """``(receiver, name)`` pairs ``line`` reaches as ``... .receiver.name``.

    A receiver that is itself a property (``other.tel.record``) is no import
    binding. The callee scan can miss that when the ``.`` ending one line
    continues the chain on the next, so a qualified ref whose pair appears in
    such a chain on the anchored line fails closed. A chain elsewhere in the
    file says nothing about the anchored occurrence.
    """
    return frozenset(
        (tokens[index + 1].value, tokens[index + 3].value)
        for index in range(len(tokens) - 3)
        if line in (tokens[index + 1].line, tokens[index + 3].line)
        and _is(tokens[index], "punct")
        and tokens[index].value in (".", "?.")
        and _is(tokens[index + 1], "ident")
        and _is(tokens[index + 2], "punct")
        and tokens[index + 2].value in (".", "?.")
        and _is(tokens[index + 3], "ident")
    )


def _es_direct_member_calls(tokens: tuple[_EsToken, ...], line: int) -> frozenset[tuple[str, str]]:
    """``(receiver, name)`` pairs ``line`` calls as ``receiver.name(`` on a bare receiver.

    The callee scan reports each pair once however often the line repeats it,
    so ``other.tel.record(); tel.record()`` reads as one chained ``tel``.
    A direct occurrence of the pair keeps it bound to the import whatever a
    chained occurrence beside it reaches (PRRT_kwDOSJAM6s6vOa0O).
    """
    return frozenset(
        (tokens[index].value, tokens[index + 2].value)
        for index in range(len(tokens) - 3)
        if line in (tokens[index].line, tokens[index + 2].line)
        and _is(tokens[index], "ident")
        and not _is_reference_position(tokens, index)
        and _is(tokens[index + 1], "punct")
        and tokens[index + 1].value in (".", "?.")
        and _is(tokens[index + 2], "ident")
        and (
            _is(tokens[index + 3], "punct", "(")
            or (
                _is(tokens[index + 3], "punct", "?.")
                and _is(_token_at(tokens, index + 4), "punct", "(")
            )
        )
    )


def _es_calls(tokens: tuple[_EsToken, ...], qualifier: str | None, name: str, line: int) -> bool:
    """True when an occurrence on ``line`` calls ``qualifier.name`` / ``name``.

    A bare call is ``name(`` (or ``name?.(``) outside a property position and
    not heading a parameter list; a qualified one is ``qualifier.name(`` on a
    bare receiver, with either name on ``line``. A correction that drops or
    renames the call while leaving its import in place reaches the definition
    through no call, so it fails closed (PRRT_kwDOSJAM6s6vO2G-); so does one
    whose only surviving call sits on another line (PRRT_kwDOSJAM6s6vRAzJ).
    """
    for index, token in enumerate(tokens):
        if not _is(token, "ident", qualifier or name) or _is_reference_position(tokens, index):
            continue
        callee = index
        if qualifier is not None:
            dot = _token_at(tokens, index + 1)
            if not (
                _is(dot, "punct")
                and dot.value in (".", "?.")
                and _is(_token_at(tokens, index + 2), "ident", name)
            ):
                continue
            callee = index + 2
        elif _es_signature_head(tokens, index):
            continue
        if line not in (token.line, tokens[callee].line):
            continue
        after = _token_at(tokens, callee + 1)
        if _is(after, "punct", "(") or (
            _is(after, "punct", "?.") and _is(_token_at(tokens, callee + 2), "punct", "(")
        ):
            return True
    return False


# Probe suffixes per specifier extension, in TS/Node resolution order. An
# extensionless specifier also probes ``index`` files under it.
_EXTENSIONLESS_PROBES = (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs")
_EXTENSION_PROBES = {
    ".js": (".ts", ".tsx", ".d.ts", ".js", ".jsx"),
    ".jsx": (".tsx", ".d.ts", ".jsx"),
    ".mjs": (".mts", ".d.mts", ".mjs"),
    ".cjs": (".cts", ".d.cts", ".cjs"),
    ".ts": (".ts",),
    ".tsx": (".tsx",),
    ".mts": (".mts",),
    ".cts": (".cts",),
}
_DECLARATION_SUFFIXES = (".d.ts", ".d.mts", ".d.cts")


def _relative_specifier_probe_paths(importer: str, specifier: str) -> tuple[str, ...] | None:
    """Repo paths a relative ``specifier`` imported from ``importer`` may resolve to.

    Only ``./`` / ``../`` specifiers (and ``.`` / ``..``) are resolvable here.
    Bare packages, path aliases (``@/x``, ``~/x``), absolute paths, ``node:``
    and URLs depend on configuration this reader does not see, so they return
    None, as does a path climbing above the repository root. A ``.js``
    specifier maps to its ``.ts`` source the way TS resolves it.
    """
    if not (specifier in (".", "..") or specifier.startswith(("./", "../"))):
        return None
    directory_only = specifier in (".", "..") or specifier.endswith("/")
    target = posixpath.normpath(posixpath.join(posixpath.dirname(importer), specifier))
    if target == ".." or target.startswith("../"):
        return None
    target = "" if target == "." else target
    if directory_only:
        return tuple(posixpath.join(target, f"index{suffix}") for suffix in _EXTENSIONLESS_PROBES)
    stem, suffix = posixpath.splitext(target)
    if suffix.lower() in _EXTENSION_PROBES:
        return tuple(f"{stem}{probe}" for probe in _EXTENSION_PROBES[suffix.lower()])
    return tuple(f"{target}{probe}" for probe in _EXTENSIONLESS_PROBES) + tuple(
        f"{target}/index{probe}" for probe in _EXTENSIONLESS_PROBES
    )


def _directory_manifest_paths(probes: tuple[str, ...]) -> tuple[str, ...]:
    """The ``package.json`` beside each ``index`` file among ``probes``, deduplicated."""
    return tuple(
        dict.fromkeys(
            posixpath.join(posixpath.dirname(path), "package.json")
            for path in probes
            if posixpath.basename(path).startswith("index.")
        )
    )


def _resolved_module_path(probes: tuple[str, ...], existing: frozenset[str]) -> str | None:
    """The one runtime file ``probes`` resolve to among ``existing``, or None.

    Declaration files carry no runtime definition and are ignored. Zero or
    several runtime candidates (``x.ts`` beside ``x.js`` or ``x/index.ts``)
    fail closed, which is deterministic whatever a toolchain would prefer. An
    ``index`` file with a ``package.json`` beside it fails closed too: the
    manifest's ``main`` / ``exports`` / ``types`` may point the directory
    import at another file.
    """
    found = [
        path for path in probes if path in existing and not path.endswith(_DECLARATION_SUFFIXES)
    ]
    if len(found) != 1:
        return None
    manifests = _directory_manifest_paths((found[0],))
    return None if any(manifest in existing for manifest in manifests) else found[0]


def _es_ref_target(
    imports: dict[str, _EsBinding],
    rebound: frozenset[str],
    chained: frozenset[tuple[str, str]],
    qualifier: str | None,
    name: str,
) -> tuple[str, str] | None:
    """``(specifier, export_name)`` one callee ref reaches, or None to fail closed.

    A bare call reaches the export its named or default import binds; a call
    on a namespace import reaches the namespace's export of that name. A
    namespace called bare, a named import used as a receiver (a member of an
    object is out of scope), an unbound or rebound key, a receiver the file
    reaches as a property on the anchored line (``chained``, see
    ``_es_chained_member_refs``)
    and an unfollowable binding all fail closed.
    """
    key = name if qualifier is None else qualifier
    binding = imports.get(key)
    if binding is None or binding.specifier is None or key in rebound:
        return None
    if (key, name) in chained:
        return None
    if qualifier is None:
        return None if binding.imported == "*" else (binding.specifier, binding.imported)
    return (binding.specifier, name) if binding.imported == "*" else None
