"""The JS/TS tokenizer behind the ES-module binding readers (issue #1047).

Split from ``..._ancestry_cross_file_es_modules``, which re-exports these
names. ``_es_module_tokens`` reduces a JS/TS source to its significant tokens:
comments are skipped, strings, templates and regex literals are single tokens,
and JSX elements are skipped in JSX-capable files. Any anomaly fails closed
with None.
"""

from __future__ import annotations

import bisect
import itertools
import re
from typing import NamedTuple

from awf.runtime.pr_monitor_runner import (
    pre_push_validation_fix_pass_ancestry_callees_masking as _masking,
)

# The eight JS/TS source suffixes, shared with the callee scan's masking.
_JS_TS_CALL_SITE_SUFFIXES = _masking._JS_TS_PRIVATE_FIELD_SUFFIXES

# Suffixes whose ``<`` at an expression start opens a JSX element. Plain
# ``.ts`` / ``.mts`` / ``.cts`` use ``<`` for generics and type assertions.
_JSX_CAPABLE_SUFFIXES = frozenset({".js", ".jsx", ".mjs", ".cjs", ".tsx"})
_TS_SUFFIXES = frozenset({".ts", ".tsx", ".mts", ".cts"})
# Tokens that may close a TS type-argument list (``generic<T> / x``,
# ``f<A<B>> / x``), so a ``/`` after them is ambiguous in TS.
_TYPE_ARGUMENT_CLOSERS = frozenset({">", ">>", ">>>"})

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
# Tokens after which an expression may start, yet a ``{`` opens a block: a
# statement boundary, an arrow body, ``else`` / ``do``, or a TS ``>`` closing
# a generic return type (``(): A<B> {``).
_BLOCK_BRACE_PREFIXES = frozenset({";", "{", "}", ")", "=>", ">", ">>", ">>>", "else", "do"})
_LINE_TERMINATORS = frozenset("\n\r  ")
# Tokens after which ``class`` / ``function`` starts a declaration, not an expression.
_DECLARATION_PREFIXES = frozenset({";", "{", "}", ")", "else", "do", "export", "default"})


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

    def __init__(
        self, text: str, *, jsx: bool, tsx: bool = False, typescript: bool = False
    ) -> None:
        self.text = text
        self.pos = 0
        self.depth = 0
        self.newline = False
        self.jsx = jsx
        self.tsx = tsx
        self.typescript = typescript
        self.tokens: list[_EsToken] = []
        # Offset just past each line, so a position's line is one bisect away.
        self.line_ends = list(
            itertools.accumulate(len(line) for line in text.splitlines(keepends=True))
        )
        # Index of the last ``)`` that closed an ``if`` / ``for`` / ``while`` /
        # ``with`` condition: a statement, so a regex, may start after it.
        self.condition_close = -1
        # Indexes of the ``}`` tokens closing an object literal (``"object"``),
        # a class or function expression's body (``"operand"``), or a ``{``
        # after ``:`` that may open an object or a block (``"colon"``).
        self.brace_closes: dict[int, str] = {}
        # ``(depth, role)`` of each ``class`` / ``function`` whose body ``{`` is
        # still ahead; the body's ``}`` takes the role, as an object's does.
        self.callable_heads: list[tuple[int, str]] = []
        # The head role an ``async`` read, for an ``async function`` after it.
        self.async_head_role: str | None = None

    def emit(self, kind: str, value: str) -> None:
        line = bisect.bisect_right(self.line_ends, self.pos) + 1
        self.tokens.append(_EsToken(kind, value, self.depth, self.newline, line))
        self.newline = False

    def ends_operand(self, index: int) -> bool:
        """True when ``tokens[index]`` ends an operand, so a ``!`` after it is postfix.

        TS reads ``x!`` / ``f()!`` / ``a[0]!`` as a non-null assertion when no
        line break precedes the ``!``. An identifier counts unless it is a
        keyword taking an operand (``return !x``) rather than a member name
        (``a.return!``), a ``)`` unless it closes a
        statement condition, and a ``!`` only when it is itself postfix.
        """
        token = self.tokens[index] if index >= 0 else None
        if token is None:
            return False
        if token.kind == "ident":
            return token.value not in _masking._JS_REGEX_PREFIX_KEYWORDS or self.is_member_name(
                index
            )
        if token.value == "!":
            return not token.newline_before and self.ends_operand(index - 1)
        if token.value == ")":
            return self.condition_close != index
        return token.value == "]"

    def expression_may_start(self) -> bool:
        """True when a ``/`` here opens a regex, and a ``<`` a JSX element.

        The same rule ``_js_slash_can_start_regex`` applies per line: after an
        operator or an opening bracket, or after a keyword that takes an
        operand — not a member name such as ``a.return``. A ``}`` ends a block far more often than an expression, and a
        ``)`` closing an ``if`` / ``for`` / ``while`` / ``with`` condition ends
        a statement head, so both start an expression too: reading a regex
        there as division would let a quote or backtick inside it swallow the
        code below into a literal while the stream still balances. A ``}``
        closing an object literal ends an operand, so ``{a: 1} / x`` divides,
        and one whose ``{`` followed a ``:`` (an object, a label's block or a
        ``case`` block) cannot be told apart and raises, failing closed. Template
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
        if prev.kind == "punct" and prev.value == "}":
            role = self.brace_closes.get(len(self.tokens) - 1)
            if role == "colon":
                raise _EsLexError
            return role is None
        if prev.kind == "punct":
            return prev.value not in ("]", "++", "--")
        if prev.kind == "ident":
            return prev.value in _masking._JS_REGEX_PREFIX_KEYWORDS and not self.is_member_name(
                len(self.tokens) - 1
            )
        return False

    def slash_opens_regex(self) -> bool:
        """True when a ``/`` here opens a regex; raises when TS leaves it ambiguous.

        In TS a ``>`` may close a type-argument list, an instantiation
        expression that ends an operand (``generic<T> / x`` divides), or a
        comparison or shift, after which a regex starts. The tokens cannot
        tell the two apart, so a ``/`` there fails closed; reading division
        as a regex would swallow the code up to the next ``/``.
        """
        if not self.expression_may_start():
            return False
        prev = self.tokens[-1] if self.tokens else None
        if (
            self.typescript
            and prev is not None
            and prev.kind == "punct"
            and prev.value in _TYPE_ARGUMENT_CLOSERS
        ):
            raise _EsLexError
        return True

    def is_member_name(self, index: int) -> bool:
        """True when ``tokens[index]`` follows ``.`` / ``?.``: a property, never a keyword."""
        prev = self.tokens[index - 1] if index > 0 else None
        return prev is not None and prev.kind == "punct" and prev.value in (".", "?.")

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

    def brace_role(self) -> str:
        """What the ``{`` here opens: ``"object"``, ``"colon"`` (unknown) or ``""`` (a block).

        A ``{`` where no expression may start, or after a token in
        ``_BLOCK_BRACE_PREFIXES``, opens a block; after a ``:`` it may open
        either. Elsewhere — after an operator, ``(``, ``[``, ``,`` or a
        keyword taking an operand — it opens an object literal, unless a line
        break follows the keyword (``return`` then a block, by ASI).
        """
        prev = self.tokens[-1] if self.tokens else None
        if prev is None or prev.kind in ("ident", "punct") and prev.value in _BLOCK_BRACE_PREFIXES:
            return ""
        if not self.expression_may_start():
            return ""
        if prev.value == ":":
            return "colon"
        return "" if prev.kind == "ident" and self.newline else "object"

    def opens_condition(self) -> bool:
        """True when a ``(`` here opens a statement condition, ``for await (`` included."""
        tail = [(t.kind, t.value) for t in self.tokens[-2:]]
        if tail[-1:] == [("ident", "await")]:
            return tail[:1] == [("ident", "for")]
        return (
            bool(tail)
            and tail[-1][0] == "ident"
            and tail[-1][1] in _CONDITION_KEYWORDS
            and not self.is_member_name(len(self.tokens) - 1)
        )

    def line_end(self, start: int) -> int:
        end = start
        while end < len(self.text) and self.text[end] not in _LINE_TERMINATORS:
            end += 1
        return end

    def lex_code(self, closer: str | None) -> None:
        """Lex until EOF, or until the ``}`` closing a ``${`` / JSX ``{``."""
        stack: list[tuple[str, str]] = []
        text, n = self.text, len(self.text)
        while True:
            self.skip_trivia()
            if self.pos >= n:
                if closer is not None or stack:
                    raise _EsLexError
                return
            ch = text[self.pos]
            if ch in _CLOSER_FOR:
                if ch == "{":
                    role = self.brace_role()
                    heads = self.callable_heads
                    if not role and heads and heads[-1][0] == self.depth:
                        role = heads.pop()[1]
                else:
                    role = "condition" if self.opens_condition() else ""
                self.emit("punct", ch)
                stack.append((_CLOSER_FOR[ch], role))
                self.depth += 1
                self.pos += 1
            elif ch in ")]}":
                if not stack:
                    if ch != closer:
                        raise _EsLexError
                    self.pos += 1
                    return
                closer_char, role = stack.pop()
                if closer_char != ch:
                    raise _EsLexError
                self.depth -= 1
                while self.callable_heads and self.callable_heads[-1][0] > self.depth:
                    self.callable_heads.pop()
                self.emit("punct", ch)
                if role == "condition":
                    self.condition_close = len(self.tokens) - 1
                elif role:
                    self.brace_closes[len(self.tokens) - 1] = role
                self.pos += 1
            elif ch in "'\"":
                self.emit("string", self.read_string(ch))
            elif ch == "`":
                self.read_template()
            elif ch == "/" and self.slash_opens_regex():
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

    def callable_head_role(self) -> str | None:
        """The role a ``class`` / ``function`` here gives its body's ``}``.

        A class or function expression is an operand, so a ``/`` after the
        ``}`` closing its body divides (``class {} / x``, ``"operand"``); after
        a declaration's ``}`` it opens a regex (``""``). A head where no
        expression may start, or after a token in ``_DECLARATION_PREFIXES``,
        starts a declaration; after a ``:`` or a line-broken keyword
        (``return`` then a declaration, by ASI) it may start either, so its
        ``}`` fails closed (``"colon"``). A member name (``a.class``) is
        neither (None). ``async function`` takes the role read at ``async``.
        """
        prev = self.tokens[-1] if self.tokens else None
        if prev is None or prev.value in _DECLARATION_PREFIXES:
            return ""
        if prev.value in (".", "?."):
            return None
        if not self.expression_may_start():
            return ""
        if prev.value == ":" or prev.kind == "ident" and self.newline:
            return "colon"
        return "operand"

    def read_word_or_punctuator(self, ch: str) -> None:
        text = self.text
        ident = _IDENT_RE.match(text, self.pos + (ch == "#"))
        if ident is not None:
            word = text[self.pos : ident.end()]
            if word == "async":
                self.async_head_role = self.callable_head_role()
            elif word in ("class", "function"):
                prev = self.tokens[-1] if self.tokens else None
                is_async = word == "function" and prev is not None and prev.value == "async"
                role = (
                    self.async_head_role
                    if is_async and not self.newline
                    else self.callable_head_role()
                )
                if role is not None:
                    self.callable_heads.append((self.depth, role))
            self.emit("ident", word)
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
        text.removeprefix("﻿"),
        jsx=suffix in _JSX_CAPABLE_SUFFIXES,
        tsx=suffix == ".tsx",
        typescript=suffix in _TS_SUFFIXES,
    )
    try:
        lexer.lex_code(None)
    except (_EsLexError, RecursionError):
        return None
    return tuple(lexer.tokens)
