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
from collections.abc import Iterator

from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_callees import (
    _ANCHORED_SCOPES,
    _FUNCTION_SCOPES,
    _definition_binding_scope,
    _leading_indent,
    _plain_assignment_rebound_scope_names,
)


def _assignment_target_names(target: ast.expr) -> Iterator[str]:
    """The plain names one assignment or loop/``with`` target binds.

    Unpacking targets nest (``first, (second, *rest) = ...``), while an
    attribute or subscript target binds nothing in the scope's own namespace,
    so only ``ast.Name`` stores are yielded.
    """
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, ast.Starred):
        yield from _assignment_target_names(target.value)
    elif isinstance(target, (ast.List, ast.Tuple)):
        for element in target.elts:
            yield from _assignment_target_names(element)


def _header_line_suite_heads(raw_lines: list[str], tree: ast.AST) -> dict[int, tuple[int, int]]:
    """``suite_line -> (column, head_line)`` for each definition suite on a header line.

    ``def other(): record = replacement`` runs its body on the header's own
    physical line, so the statement shares that line's leading indent — the
    *header's* — and starts on no line below the head, which is exactly where
    ``_definition_binding_scope`` stops reading: it only takes spans that start
    strictly above the line it is given. A wrapped signature does the same on
    the closing ``): record = replacement`` line of its header, which sits
    outside the head's lexical span entirely. Both are read the
    same way, by the one symptom they share — a first body statement no deeper
    than its own header — so the readers can tell such a binding apart from one
    the enclosing scope's body really executes, and attribute it to the head
    holding it (PRRT_kwDOSJAM6s6rBkXd).
    """
    suites: dict[int, tuple[int, int]] = {}
    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        first = scope.body[0]
        if _leading_indent(raw_lines[first.lineno - 1]) <= scope.col_offset:
            suites[first.lineno] = (first.col_offset, scope.lineno)
    return suites


def _global_declared_scope_ranges(tree: ast.AST) -> list[tuple[int, int, frozenset[str]]]:
    """``(start, end, names)`` for each function scope declaring names ``global``.

    ``global record`` exists only so an assignment in that body reaches the
    *module* binding, so such a statement rebinds the module name even though
    it sits inside a function whose other assignments bind locals: left
    attributed to the function's own scope, a ``def record`` replaced by
    ``global record; record = replacement`` stayed importable and a correction
    confined to that dead head could satisfy the cross-file evidence gate while
    importers still reach the replacement (PRRT_kwDOSJAM6s6rBsj5).

    Each scope reports only the declarations its *own* body makes — a nested
    function's ``global`` governs that nested body — but the ranges nest, so a
    binding inside a declaring scope's nested local is attributed to module
    scope as well. That only ever withholds a span, which is the direction this
    reader fails in.
    """
    ranges: list[tuple[int, int, frozenset[str]]] = []
    for scope in ast.walk(tree):
        if not isinstance(scope, _FUNCTION_SCOPES):
            continue
        declared: set[str] = set()
        pending: list[ast.AST] = list(ast.iter_child_nodes(scope))
        while pending:
            node = pending.pop()
            if isinstance(node, (*_FUNCTION_SCOPES, ast.ClassDef)):
                continue
            if isinstance(node, ast.Global):
                declared.update(node.names)
            pending.extend(ast.iter_child_nodes(node))
        if declared:
            ranges.append((scope.lineno, scope.end_lineno or scope.lineno, frozenset(declared)))
    return ranges


def _declared_global_at(
    global_scopes: list[tuple[int, int, frozenset[str]]], line: int, name: str
) -> bool:
    """True when a function scope holding ``line`` declares ``name`` ``global``."""
    return any(start <= line <= end and name in declared for start, end, declared in global_scopes)


def _binding_scope_start(
    file_text: str,
    all_spans: list[tuple[str, int, int, int]],
    *,
    header_line_suites: dict[int, tuple[int, int]],
    line: int,
    column: int,
    indent: int,
) -> int:
    """The ``scope_start`` key of the scope whose body executes this binding.

    Normally the indent of the line the binding starts on, read through
    ``_definition_binding_scope`` so the key matches the definition heads' own.
    A binding inside a suite written on a header line is the exception: it
    carries the header's indent on a line the header's own span does not
    contain, which that reader resolves to the scope *enclosing* the header —
    so ``def other(): record = ...``, and the same suite on a wrapped
    signature's closing line, looked like a module-scope rebinding and withheld
    the live module ``def record``, parking a correct callee-span fix as
    ``needs_human`` (PRRT_kwDOSJAM6s6rBkXd). Such a binding is attributed to
    the head holding it instead, which is the key that head's own body already
    uses; a one-line suite can hold no definition head of its own, so the pair
    it keys shadows nothing — which is the point, since a local or class
    attribute is no rebinding of the module name.
    """
    suite = header_line_suites.get(line)
    if suite is not None and column >= suite[0]:
        return suite[1]
    scope_start, _body_indent = _definition_binding_scope(
        file_text, all_spans, start=line, indent=indent
    )
    return scope_start


def _assignment_rebound_scope_names(
    file_text: str,
    all_spans: list[tuple[str, int, int, int]],
    *,
    names: frozenset[str],
    definition_head_starts: frozenset[int],
) -> set[tuple[int, str]]:
    """``(scope_start, name)`` pairs a binding statement rebinds, read from the AST.

    The same rebinding form ``_plain_assignment_rebound_scope_names`` reads
    lexically (PRRT_kwDOSJAM6s6q_ywa), found wherever Python actually spells
    it rather than only at a physical line's left edge. A suite written on its
    header's own line — ``if enabled: record = replacement`` — puts the
    assignment past the ``if``, where the anchored reader matches neither the
    header nor the statement, so the name keeps looking bound to the dead
    ``def`` above it while the importer reaches ``replacement``
    (PRRT_kwDOSJAM6s6rBbdn). An ``ast`` walk sees that statement, a
    semicolon-separated one and a wrapped one alike, and it is also the only
    reading a ``:=`` binding has: ``if (record := replacement):`` rebinds the
    name inside the header itself, where no statement starts at all.

    A ``for`` or ``with`` target binds its name the same way and is read the
    same way: ``for record in handlers:`` leaves the ``def record`` above it
    dead once the loop has run, and ``with open(path) as record:`` rebinds the
    name for its whole suite, so crediting a correction confined to that head
    would resolve the thread while importers still reach the loop's last
    handler or the context manager's value (PRRT_kwDOSJAM6s6rBjC4).

    A ``match`` pattern's capture binds its name the same way and is read the
    same way: ``case record:`` replaces the ``def record`` above it once the
    pattern matches, as do a sequence pattern's ``*rest`` and a mapping
    pattern's ``**rest``, whose names live on the pattern node instead of on an
    ``ast.Name`` store, so a reader watching only assignment statements still
    offered the dead head while importers receive the captured value
    (PRRT_kwDOSJAM6s6rBsj0).

    An ``except Exception as record`` target is read the same way, and carries
    its name on the handler node for the same reason. It is the one binding
    form that *unbinds* the name again — Python deletes the target when the
    handler exits — so a module body whose handler runs replaces the ``def
    record`` above it and then leaves the name importable from nowhere at all,
    which makes offering that dead head worse than for any other rebinding
    (PRRT_kwDOSJAM6s6rB2mh).

    Only bindings that bind a name are read: an annotation without a value
    binds nothing, a ``with`` item without ``as`` binds nothing, a bare
    ``except Exception:`` binds nothing, a wildcard
    ``case _:`` and a class or value pattern capture nothing, and an
    attribute or subscript target rebinds no name of the scope. Comprehension
    targets are not read at all — they bind in the comprehension's own scope,
    not the one holding it. The head of a line already read as a definition is
    skipped exactly as the lexical reader skips it — ``record = lambda ...``
    *is* the head the last-head fold orders — while any other statement on
    that line is read.
    Each binding is attributed through ``_binding_scope_start``, from the
    indent of the line it starts on, so the keys match the heads' own and a
    function-local assignment shadows nothing at module scope — including one
    written in its header's own line (PRRT_kwDOSJAM6s6rBkXd) — except where a
    ``global`` declaration says otherwise, which is the one binding a function
    body makes at module scope (see ``_global_declared_scope_ranges``). Text this
    reader cannot parse yields nothing, leaving the lexical reader's verdict
    as it stands: that is the JS/TS case, which this Python-shaped walk has no
    reading of.
    """
    if not names:
        return set()
    try:
        tree = ast.parse(file_text)
    except (SyntaxError, ValueError):
        return set()
    raw_lines = file_text.splitlines()
    header_line_suites = _header_line_suite_heads(raw_lines, tree)
    global_scopes = _global_declared_scope_ranges(tree)
    rebound: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        captured: set[str] = set()
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            targets = [node.target]
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            targets = [item.optional_vars for item in node.items if item.optional_vars is not None]
        elif isinstance(node, ast.NamedExpr) or (
            isinstance(node, ast.AnnAssign) and node.value is not None
        ):
            targets = [node.target]
        elif isinstance(node, (ast.MatchAs, ast.MatchStar, ast.ExceptHandler)) and node.name:
            targets = []
            captured = {node.name}
        elif isinstance(node, ast.MatchMapping) and node.rest:
            targets = []
            captured = {node.rest}
        else:
            continue
        bound = (
            {name for target in targets for name in _assignment_target_names(target)} | captured
        ) & names
        if not bound:
            continue
        indent = _leading_indent(raw_lines[node.lineno - 1])
        if node.lineno in definition_head_starts and node.col_offset == indent:
            continue
        scope_start = _binding_scope_start(
            file_text,
            all_spans,
            header_line_suites=header_line_suites,
            line=node.lineno,
            column=node.col_offset,
            indent=indent,
        )
        rebound.update(
            (0 if _declared_global_at(global_scopes, node.lineno, name) else scope_start, name)
            for name in bound
        )
    return rebound


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
    attributed — through ``_binding_scope_start``, so the keys match the heads'
    own — to the scope whose body executes the statement, leaving a
    function-local import shadowing nothing at module scope, ``def other():
    import record`` included, except where a ``global`` declaration says
    otherwise: an import is the one other statement form that binds a name
    while carrying no definition head, so ``global record`` beside ``from
    fallback import record`` replaces the module binding exactly as a declared
    assignment does, and attributing it to the helper's own scope left the dead
    head importable (PRRT_kwDOSJAM6s6rB2mf). Text this reader cannot parse
    yields nothing: it is the JS/TS case, where a definition head and an import
    of that same name are a redeclaration error rather than a dead definition.
    """
    if not names:
        return set()
    try:
        tree = ast.parse(file_text)
    except (SyntaxError, ValueError):
        return set()
    raw_lines = file_text.splitlines()
    header_line_suites = _header_line_suite_heads(raw_lines, tree)
    global_scopes = _global_declared_scope_ranges(tree)
    rebound: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        bound = {alias.asname or alias.name.split(".")[0] for alias in node.names}
        targets = names if "*" in bound else bound & names
        if not targets:
            continue
        scope_start = _binding_scope_start(
            file_text,
            all_spans,
            header_line_suites=header_line_suites,
            line=node.lineno,
            column=node.col_offset,
            indent=_leading_indent(raw_lines[node.lineno - 1]),
        )
        rebound.update(
            (0 if _declared_global_at(global_scopes, node.lineno, target) else scope_start, target)
            for target in targets
        )
    return rebound


def _rebound_scope_names(
    file_text: str,
    all_spans: list[tuple[str, int, int, int]],
    *,
    path: str | None,
    collected: list[tuple[str, int, int, tuple[int, int], tuple[int, int]]],
) -> set[tuple[int, str]]:
    """``(scope_start, name)`` pairs no ``collected`` definition head can own.

    The union of every rebinding form these readers know: a plain assignment
    (PRRT_kwDOSJAM6s6q_ywa), a ``for`` or ``with`` target
    (PRRT_kwDOSJAM6s6rBjC4) and an ``import`` of the same name
    (PRRT_kwDOSJAM6s6rAAWY). ``collected`` carries the candidate heads in
    ``_importable_definition_spans_for_names``'s own shape — the names to look
    for, and the lines already read as definitions of their own.

    Assignments are read twice over: lexically, which is the only reading
    JS/TS text has, and from the Python AST, which finds the ones a physical
    line does not start with (PRRT_kwDOSJAM6s6rBbdn) and the loop and ``with``
    targets the lexical reader has no shape for at all.
    """
    names = frozenset(name for name, *_rest in collected)
    head_starts = frozenset(start for _name, start, *_rest in collected)
    return (
        _plain_assignment_rebound_scope_names(
            file_text,
            all_spans,
            path=path,
            names=names,
            definition_head_starts=head_starts,
        )
        | _assignment_rebound_scope_names(
            file_text, all_spans, names=names, definition_head_starts=head_starts
        )
        | _import_rebound_scope_names(file_text, all_spans, names=names)
    )


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


def _import_lines_hidden_from(file_text: str, line: int | None) -> frozenset[int]:
    """1-based lines whose own bindings cannot reach ``line``.

    A function-local ``import`` binds a *local*: ``def a(): from pkg.real
    import record`` is invisible to every line outside ``a``'s body, so a
    sibling ``def b(): from pkg.decoy import record`` is not a second binding
    of the name the call inside ``a`` reaches. Reading the whole file's import
    heads as one identity set made such a name ambiguous at *both* call sites
    and parked a legitimate cross-file correction as ``needs_human``
    (PRRT_kwDOSJAM6s6rA-ru), so the cross-file import readers skip heads
    sitting in a function scope the anchored line is not in.

    Only function scopes are withheld, and only when ``line`` names one: a
    module-level import is in scope everywhere, a class body's import binds an
    attribute the readers already weigh file-wide, and ``line`` is None for the
    re-read whose own text the anchored line does not index (see
    ``_caller_binding_after_correction``) — all three keep the file-wide
    reading, which is the conservative one. Text this reader cannot parse
    hides nothing, for the same reason.
    """
    if line is None:
        return frozenset()
    try:
        tree = ast.parse(file_text)
    except (SyntaxError, ValueError):
        return frozenset()
    hidden: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, _FUNCTION_SCOPES):
            continue
        end = node.end_lineno or node.lineno
        if not node.lineno <= line <= end:
            hidden.update(range(node.lineno, end + 1))
    return frozenset(hidden)


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
