"""Rebinding readers for importable-definition selection (pre-push FIXED evidence).

Definition discovery recognizes only definition *heads*, so a scope that binds
one of those names again with a statement carrying no head of its own leaves
the dead head looking like the name's effective binding. These readers report
the ``(scope_start, name)`` pairs that happens for, so
``_importable_definition_spans_for_names`` can withhold those spans instead of
offering dead code as the callee a correction has to touch.

Kept beside ``..._ancestry_callees.py`` and ``..._ancestry_cross_file.py`` so
all three stay under the first-party file line limit.
"""

from __future__ import annotations

import ast

from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_callees import (
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
