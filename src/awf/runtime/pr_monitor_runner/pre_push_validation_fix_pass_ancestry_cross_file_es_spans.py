"""Definition-span, rebinding and call analysis over ES-module tokens (issue #1047).

Split from ``..._ancestry_cross_file_es_modules``, which re-exports these
names. The shadow detectors (``_es_rebound_names``, ``_es_assigned_names``)
are deliberately over-inclusive; ``_es_module_definition_span`` brackets one
module-level definition by bracket depth, and every ambiguity fails closed.
"""

from __future__ import annotations

from awf.runtime.pr_monitor_runner import (
    pre_push_validation_fix_pass_ancestry_callees_masking as _masking,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file_es_bindings import (
    _STATEMENT_KEYWORDS,
    _ident_value,
    _is,
    _is_reference_position,
    _token_at,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file_es_lexer import (
    _CONDITION_KEYWORDS,
    _EsToken,
)

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
    ``get size() {``, ``flush(): void;``), type parameters included
    (``record<T>(v: T) {``). A call followed by a ternary ``:`` also reads as
    one, which only fails it closed.
    """
    open_index = _es_after_type_parameters(tokens, index + 1)
    opener = None if open_index is None else _token_at(tokens, open_index)
    if open_index is None or not _is(opener, "punct", "("):
        return False
    close = open_index + 1
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


def _es_direct_member_calls(
    tokens: tuple[_EsToken, ...], line: int, *, typescript: bool = False
) -> frozenset[tuple[str, str]]:
    """``(receiver, name)`` pairs ``line`` calls as ``receiver.name(`` on a bare receiver.

    The callee scan reports each pair once however often the line repeats it,
    so ``other.tel.record(); tel.record()`` reads as one chained ``tel``.
    A direct occurrence of the pair keeps it bound to the import whatever a
    chained occurrence beside it reaches (PRRT_kwDOSJAM6s6vOa0O). With
    ``typescript``, type arguments may sit before the ``(``
    (``tel.record<Result>(``, PRRT_kwDOSJAM6s6vSMVp).
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
        and _es_called_at(tokens, index + 2, typescript=typescript)
    )


def _es_after_type_arguments(tokens: tuple[_EsToken, ...], index: int) -> int:
    """The index after the TS type arguments at ``index``, ``index`` itself when none.

    Only type punctuators may sit at the arguments' own depth, so a comparison
    such as ``a < b && c > (d)`` carries no type arguments.
    """
    end = _es_after_type_parameters(tokens, index)
    if end is None or any(
        token.kind == "punct"
        and token.depth == tokens[index].depth
        and token.value not in _TARGET_TYPE_PUNCTUATORS | {","}
        for token in tokens[index:end]
    ):
        return index
    return end


def _es_calls(
    tokens: tuple[_EsToken, ...],
    qualifier: str | None,
    name: str,
    line: int,
    *,
    typescript: bool = False,
) -> bool:
    """True when an occurrence on ``line`` calls ``qualifier.name`` / ``name``.

    A bare call is ``name(`` (or ``name?.(``) outside a property position and
    not heading a parameter list; a qualified one is ``qualifier.name(`` on a
    bare receiver, with either name on ``line``. With ``typescript``, type
    arguments may sit before the ``(`` (``record<Result>(``,
    ``record?.<Result>(``, PRRT_kwDOSJAM6s6vRrub); plain JS reads them as a
    comparison. A correction that drops or renames the call while leaving its
    import in place reaches the definition through no call, so it fails closed (PRRT_kwDOSJAM6s6vO2G-); so does one
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
        if _es_called_at(tokens, callee, typescript=typescript):
            return True
    return False


def _es_called_at(tokens: tuple[_EsToken, ...], callee: int, *, typescript: bool) -> bool:
    """True when the name at ``callee`` is followed by ``(`` or ``?.(``.

    With ``typescript``, type arguments may sit before the ``(``, after ``?.``
    for an optional call (``record?.<Result>(``).
    """
    if typescript:
        start = callee + 1
        if _is(_token_at(tokens, start), "punct", "?.") and _is(
            _token_at(tokens, start + 1), "punct", "<"
        ):
            start += 1
        end = _es_after_type_arguments(tokens, start)
        callee = callee if end == start else end - 1
    after = _token_at(tokens, callee + 1)
    return _is(after, "punct", "(") or (
        _is(after, "punct", "?.") and _is(_token_at(tokens, callee + 2), "punct", "(")
    )
