"""The import and export tables of one ES module (issue #1047).

Split from ``..._ancestry_cross_file_es_modules``, which re-exports these
names. ``_es_import_bindings`` maps each imported name to the specifier and
export it binds; ``_es_module_exports`` reads what the module exports. A
statement either reader cannot read fails closed with None.
"""

from __future__ import annotations

from typing import NamedTuple, TypeGuard

from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file_es_lexer import (
    _EsToken,
)


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
