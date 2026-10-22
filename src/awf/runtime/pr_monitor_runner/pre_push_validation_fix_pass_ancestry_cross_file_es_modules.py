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
_LINE_TERMINATORS = frozenset("\n\r  ")


class _EsToken(NamedTuple):
    """One significant token: kind, value, bracket depth, preceding newline.

    ``kind`` is ``ident``, ``punct``, ``string``, ``template``, ``regex``,
    ``number`` or ``jsx`` (the start of a skipped JSX element). A string keeps
    its raw text between the quotes, so a module specifier is readable.
    """

    kind: str
    value: str
    depth: int
    newline_before: bool


class _EsLexError(Exception):
    """Raised inside the tokenizer when the text cannot be lexed."""


class _EsLexer:
    """A recursive tokenizer; templates and JSX expressions recurse into code."""

    def __init__(self, text: str, *, jsx: bool) -> None:
        self.text = text
        self.pos = 0
        self.depth = 0
        self.newline = False
        self.jsx = jsx
        self.tokens: list[_EsToken] = []

    def emit(self, kind: str, value: str) -> None:
        self.tokens.append(_EsToken(kind, value, self.depth, self.newline))
        self.newline = False

    def expression_may_start(self) -> bool:
        """True when a ``/`` here opens a regex, and a ``<`` a JSX element.

        The same rule ``_js_slash_can_start_regex`` applies per line: after an
        operator or an opening bracket, or after a keyword that takes an
        operand. A ``}`` is read as the end of an expression, which can only
        misread a regex statement that follows a block — a lexing anomaly that
        fails closed rather than a wrong table.
        """
        if not self.tokens:
            return True
        prev = self.tokens[-1]
        if prev.kind == "punct":
            return prev.value not in (")", "]", "}", "++", "--")
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

    def line_end(self, start: int) -> int:
        end = start
        while end < len(self.text) and self.text[end] not in _LINE_TERMINATORS:
            end += 1
        return end

    def lex_code(self, closer: str | None) -> None:
        """Lex until EOF, or until the ``}`` closing a ``${`` / JSX ``{``."""
        stack: list[str] = []
        text, n = self.text, len(self.text)
        while True:
            self.skip_trivia()
            if self.pos >= n:
                if closer is not None or stack:
                    raise _EsLexError
                return
            ch = text[self.pos]
            if ch in _CLOSER_FOR:
                self.emit("punct", ch)
                stack.append(_CLOSER_FOR[ch])
                self.depth += 1
                self.pos += 1
            elif ch in ")]}":
                if not stack:
                    if ch != closer:
                        raise _EsLexError
                    self.pos += 1
                    return
                if stack.pop() != ch:
                    raise _EsLexError
                self.depth -= 1
                self.emit("punct", ch)
                self.pos += 1
            elif ch in "'\"":
                self.emit("string", self.read_string(ch))
            elif ch == "`":
                self.read_template()
            elif ch == "/" and self.expression_may_start():
                self.read_regex()
                self.emit("regex", "")
            elif ch == "<" and self.jsx and self.expression_may_start():
                self.emit("jsx", "<")
                self.read_jsx_element()
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
        self.emit("template", "")
        text, n = self.text, len(self.text)
        self.pos += 1
        while self.pos < n:
            ch = text[self.pos]
            if ch == "\\":
                self.pos += 2
            elif ch == "`":
                self.pos += 1
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
    jsx = f".{path.rsplit('.', 1)[-1].lower()}" in _JSX_CAPABLE_SUFFIXES
    lexer = _EsLexer(text.removeprefix("﻿"), jsx=jsx)
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
_CONDITION_KEYWORDS = frozenset({"for", "if", "switch", "while", "with"})


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
    if _ident_value(_token_at(tokens, open_index - 1)) in _CONDITION_KEYWORDS:
        return set()
    return {
        token.value
        for offset, token in enumerate(tokens[open_index + 1 : close_index], start=open_index + 1)
        if token.kind == "ident" and not _is_reference_position(tokens, offset)
    }


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
        elif _is(token, "punct", ")"):
            bound |= _parameter_group_names(tokens, index)
        elif token.kind == "ident" and _is(_token_at(tokens, index + 1), "punct", "=>"):
            bound.add(token.value)
    return frozenset(bound & names)


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


def _resolved_module_path(probes: tuple[str, ...], existing: frozenset[str]) -> str | None:
    """The one runtime file ``probes`` resolve to among ``existing``, or None.

    Declaration files carry no runtime definition and are ignored. Zero or
    several runtime candidates (``x.ts`` beside ``x.js`` or ``x/index.ts``)
    fail closed, which is deterministic whatever a toolchain would prefer.
    """
    found = [
        path for path in probes if path in existing and not path.endswith(_DECLARATION_SUFFIXES)
    ]
    return found[0] if len(found) == 1 else None


def _es_ref_target(
    imports: dict[str, _EsBinding],
    rebound: frozenset[str],
    qualifier: str | None,
    name: str,
) -> tuple[str, str] | None:
    """``(specifier, export_name)`` one callee ref reaches, or None to fail closed.

    A bare call reaches the export its named or default import binds; a call
    on a namespace import reaches the namespace's export of that name. A
    namespace called bare, a named import used as a receiver (a member of an
    object is out of scope), an unbound or rebound key and an unfollowable
    binding all fail closed.
    """
    key = name if qualifier is None else qualifier
    binding = imports.get(key)
    if binding is None or binding.specifier is None or key in rebound:
        return None
    if qualifier is None:
        return None if binding.imported == "*" else (binding.specifier, binding.imported)
    return (binding.specifier, name) if binding.imported == "*" else None
