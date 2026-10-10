"""Effective-binding readers for importable-definition selection (pre-push FIXED evidence).

Definition discovery recognizes only definition *heads*, so a scope that binds
one of those names again with a statement carrying no head of its own leaves
the dead head looking like the name's effective binding. These readers report
the ``(scope_start, name)`` pairs that happens for, so
``_importable_definition_spans_for_names`` can withhold those spans instead of
offering dead code as the callee a correction has to touch — together with the
last-head-wins fold itself, which the same module applies to a candidate's
*enclosing* heads as well (PRRT_kwDOSJAM6s6rAhm1).

Kept beside ``..._ancestry_callees.py`` and ``..._ancestry_cross_file.py`` so
all three stay under the first-party file line limit.
"""

from __future__ import annotations

import ast

from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_callees import (
    _ANCHORED_SCOPES,
    _FUNCTION_SCOPES,
    _definition_binding_scope,
    _leading_indent,
    _plain_assignment_rebound_scope_names,
)


def _import_rebound_scope_names(
    file_text: str,
    all_spans: list[tuple[str, int, int, int]],
    *,
    names: frozenset[str],
) -> set[tuple[int, str]]:
    """``(scope_start, name)`` pairs an ``import`` statement rebinds in that scope.

    ``def validate(...)`` followed by ``from fallback import validate`` leaves
    the ``def`` dead: importing ``validate`` from this module reaches the
    fallback object, so a correction confined to the earlier head changes
    nothing the importing call site reaches. An import binds a name without
    carrying a definition head, so it is invisible to the last-head fold and
    the name has to fail closed here, exactly as a plain assignment does
    (PRRT_kwDOSJAM6s6rAAWY). Textual order is not read, for the same reason
    ``_plain_assignment_rebound_scope_names`` does not read it: a guarded or
    conditional import need not be the binding that runs last.

    ``from pkg import *`` binds names this reader cannot enumerate, so every
    candidate name of the scope holding it fails closed. Each binding is
    attributed — through ``_definition_binding_scope``, so the keys match the
    heads' own — to the scope whose body executes the statement, leaving a
    function-local import shadowing nothing at module scope. Text this reader
    cannot parse yields nothing: it is the JS/TS case, where a definition head
    and an import of that same name are a redeclaration error rather than a
    dead definition.
    """
    if not names:
        return set()
    try:
        tree = ast.parse(file_text)
    except (SyntaxError, ValueError):
        return set()
    raw_lines = file_text.splitlines()
    rebound: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        bound = {alias.asname or alias.name.split(".")[0] for alias in node.names}
        targets = names if "*" in bound else bound & names
        if not targets:
            continue
        scope_start, _body_indent = _definition_binding_scope(
            file_text,
            all_spans,
            start=node.lineno,
            indent=_leading_indent(raw_lines[node.lineno - 1]),
        )
        rebound.update((scope_start, target) for target in targets)
    return rebound


def _rebound_scope_names(
    file_text: str,
    all_spans: list[tuple[str, int, int, int]],
    *,
    path: str | None,
    collected: list[tuple[str, int, int, tuple[int, int], tuple[int, int]]],
) -> set[tuple[int, str]]:
    """``(scope_start, name)`` pairs no ``collected`` definition head can own.

    The union of both rebinding forms these readers know: a plain assignment
    (PRRT_kwDOSJAM6s6q_ywa) and an ``import`` of the same name
    (PRRT_kwDOSJAM6s6rAAWY). ``collected`` carries the candidate heads in
    ``_importable_definition_spans_for_names``'s own shape — the names to look
    for, and the lines already read as definitions of their own.
    """
    names = frozenset(name for name, *_rest in collected)
    return _plain_assignment_rebound_scope_names(
        file_text,
        all_spans,
        path=path,
        names=names,
        definition_head_starts=frozenset(start for _name, start, *_rest in collected),
    ) | _import_rebound_scope_names(file_text, all_spans, names=names)


def _effective_scope_head_starts(
    collected: list[tuple[str, int, int, tuple[int, int], tuple[int, int]]],
) -> dict[tuple[int, str], int]:
    """The head each collected name is provably bound to, within its own scope.

    "Last head wins" holds because a module or class body executes its heads in
    textual order — but a head indented deeper than that body runs only when
    its enclosing block does, so a name defined in mutually exclusive branches
    (``if sys.platform == "win32": def validate(...)`` / ``else:``) is bound by
    the branch taken, not by textual order. Crediting a correction confined to
    the textually later head would mark the feedback fixed while the callable
    actually imported stays untouched — and the survival check would find that
    same inactive head — so a name with more than one head whose last head is
    conditional is omitted here and fails closed in both checks
    (PRRT_kwDOSJAM6s6q-6LP). A lone head stays effective (conditional or not,
    it is the only binding the call site could reach), as does a last head at
    its scope's body indent, which rebinds the name after any guarded head.

    Heads group by the scope that binds them, so a class member folds against
    that same class's other definitions of the name — the later ``def`` is the
    only attribute a receiver reaches — and never against a same-named method
    of a *different* class, a distinct attribute (PRRT_kwDOSJAM6s6q_M0o).
    """
    heads: dict[tuple[int, str], list[tuple[int, int]]] = {}
    body_indents: dict[int, int] = {}
    for name, start, indent, (scope_start, body_indent), _span in collected:
        heads.setdefault((scope_start, name), []).append((start, indent))
        body_indents[scope_start] = body_indent
    effective: dict[tuple[int, str], int] = {}
    for key, name_heads in heads.items():
        last_start, last_indent = max(name_heads)
        if len(name_heads) > 1 and last_indent > body_indents[key[0]]:
            continue
        effective[key] = last_start
    return effective


def _without_shadowed_enclosing_definitions(
    file_text: str,
    all_spans: list[tuple[str, int, int, int]],
    *,
    path: str | None,
    collected: list[tuple[str, int, int, tuple[int, int], tuple[int, int]]],
) -> list[tuple[str, int, int, tuple[int, int], tuple[int, int]]]:
    """``collected`` without the heads whose *enclosing* head is itself dead.

    A member is only reachable through the object its enclosing definition
    binds, so the effective-binding rule has to be applied to that enclosing
    head too: a module declaring ``class Collector`` twice carries the name on
    both, and a member of the first one is dead code no importer reaches, even
    though ``_definition_is_member_of_named_module_scope_definition`` finds the
    pinned name on its enclosing class (PRRT_kwDOSJAM6s6rAhm1). Each enclosing
    head is therefore folded against every head of *its* own name — the same
    last-head-wins rule, through :func:`_effective_scope_head_starts`, plus the
    rebinding readers above — and a member whose chain holds one head that
    cannot be proved effective fails closed.

    The fold runs over every head in the file, not just the candidate names,
    because an enclosing class is rarely the callee's own name; the whole pass
    is skipped when no candidate has an enclosing head at all.
    """
    enclosing = {
        start: frozenset(
            span_start
            for _span_name, span_start, span_end, span_indent in all_spans
            if span_start < start <= span_end and span_indent < indent
        )
        for _name, start, indent, _scope, _span in collected
    }
    if not any(enclosing.values()):
        return collected
    heads = [
        (
            name,
            start,
            indent,
            _definition_binding_scope(file_text, all_spans, start=start, indent=indent),
            (start, end),
        )
        for name, start, end, indent in all_spans
    ]
    effective = _effective_scope_head_starts(heads)
    rebound = _rebound_scope_names(file_text, all_spans, path=path, collected=heads)
    shadowed = {
        start
        for name, start, _indent, (scope_start, _body_indent), _span in heads
        if effective.get((scope_start, name)) != start or (scope_start, name) in rebound
    }
    return [entry for entry in collected if not enclosing[entry[1]] & shadowed]


def _scope_own_import_names(scope: ast.AST) -> frozenset[str]:
    """Names ``scope``'s own body binds with an ``import`` statement.

    Child scopes are not descended into: a nested function's import is its own
    local and a class body's is an attribute, neither of which makes the name
    local to ``scope``. Callers read every scope enclosing the line, so a
    nested function still contributes its own imports for the lines inside it.
    A name the body declares ``global`` or ``nonlocal`` is dropped — the import
    then writes that enclosing binding rather than a local one.
    """
    imported: set[str] = set()
    declared: set[str] = set()
    pending: list[ast.AST] = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        if isinstance(node, (*_ANCHORED_SCOPES, ast.ClassDef)):
            continue
        if isinstance(node, ast.alias):
            imported.add(node.asname or node.name.partition(".")[0])
            continue
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            declared.update(node.names)
            continue
        pending.extend(ast.iter_child_nodes(node))
    return frozenset(imported - declared)


def _function_local_import_names_at_line(file_text: str, line: int) -> frozenset[str]:
    """Names a function scope enclosing ``line`` binds with its own ``import``.

    A lazy ``def run(): from pkg.mod import validate; validate()`` makes
    ``validate`` local to ``run`` for the whole body, so a *module*-scope
    rebinding of the global — a top-level ``def validate`` beside it, say —
    cannot reach that call and must not hold the import to
    ``_AMBIGUOUS_IMPORT_TARGET`` and park a correction confined to the imported
    definition as ``needs_human`` (PRRT_kwDOSJAM6s6rAhm2). Only
    ``_module_scope_rebound_names``'s verdict is softened this way: a scope
    that binds the name some *other* way as well is still reported by
    ``_locally_rebound_names_at_line``, and a second import of it still fails
    closed through the import readers' own rebinding guard.

    Class bodies are not read, by ``_scope_own_import_names`` — they carry no
    function-wide local rule, so a call executing directly in one before the
    import line still reaches the global and keeps failing closed. Text this
    reader cannot parse yields nothing, which leaves the module-scope reader's
    names standing rather than crediting an import that may not be there.
    """
    try:
        tree = ast.parse(file_text)
    except (SyntaxError, ValueError):
        return frozenset()
    return frozenset(
        name
        for node in ast.walk(tree)
        if isinstance(node, _FUNCTION_SCOPES)
        and node.lineno <= line <= (node.end_lineno or node.lineno)
        for name in _scope_own_import_names(node)
    )
