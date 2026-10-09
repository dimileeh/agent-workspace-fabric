"""Cross-file call-site→definition FIXED evidence (issue #1019).

Attempt 0 already accepts call-site→definition evidence, but only *within* the
reviewed file: ``_diff_provides_related_line_evidence`` resolves the callee
referenced at the anchored line against that same file's definition spans. A
review anchored where a behaviour is *observed* and fixed where it is
*implemented* therefore carries no evidence AWF can see once the two live in
different packages, and the correction attempt parks a correct fix as
``needs_human`` (aira-agent PRs #1478 and #1491).

This module widens that one link across files for the correction attempt's
fourth evidence gate (see ``comment_verdict_correction``): the callee names at
the anchored line are resolved against the *changed* files' module-reachable
definitions, and the range's diff must overlap the definition's span — touching
the file is not enough. Kept in its own module because
``pre_push_validation_fix_pass_ancestry`` sits at the first-party line budget,
and because the cross-file rule reads as one unit.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_callees import (
    _DECORATOR_BASENAME_RE,
    _ENCLOSING_DEFINITION_RE,
    _definition_head_is_assignment,
    _definition_head_scan_lines,
    _definition_is_nested_in_other,
    _definition_span_is_class,
    _iter_definition_spans,
    _path_allows_js_private_fields,
)

# Receivers attempt 0 resolves in the reviewed file or its own class. Linking
# them to a same-named definition in another file would be guesswork without
# import/type resolution, so ``self`` / ``cls`` / ``this`` calls fail closed.
# Other qualifiers (``metrics.record(...)``) are exactly the cross-file shape.
_IN_FILE_CALLEE_QUALIFIERS = frozenset({"self", "cls", "this"})

# Upper bound on the changed paths this probe reads back. A single item's commit
# range touches one to three files; the cap only stops a pathological range from
# turning the fourth evidence gate into an unbounded fan of Git reads.
_MAX_CALLEE_EVIDENCE_CANDIDATE_PATHS = 25

# Suffixes whose bare-name bindings the import reader below understands. A bare
# callee in any other language keeps the name-only rule.
_PYTHON_CALL_SITE_SUFFIXES = frozenset({".py", ".pyi"})

# ``from pkg.mod import a, b as c`` — absolute targets. A plain ``import
# pkg.mod`` binds ``pkg`` rather than the callee, so it narrows no *bare*
# candidate path and is not matched here; relative targets are read below, and
# the plain form is read for receivers by ``_receiver_import_module_targets``.
_ABSOLUTE_FROM_IMPORT_RE = re.compile(
    r"^[ \t]*from[ \t]+([A-Za-z_]\w*(?:\.\w+)*)[ \t]+import[ \t]+(.+)$"
)

# ``from .mod import x`` / ``from ..pkg.mod import x`` / ``from . import x`` —
# the leading dots and the optional module tail, resolved against the call
# site's own directory by ``_relative_import_module_path``.
_RELATIVE_FROM_IMPORT_RE = re.compile(
    r"^[ \t]*from[ \t]+(\.+)(\w+(?:\.\w+)*)?[ \t]+import[ \t]+(.+)$"
)

# ``import pkg.mod`` / ``import pkg.mod as alias`` — the statement binds a
# module, which is the receiver shape ``pkg.mod.record(...)`` and
# ``alias.record(...)`` call through, so it does narrow a *qualified* callee.
_PLAIN_IMPORT_RE = re.compile(r"^[ \t]*import[ \t]+(.+)$")
_DOTTED_MODULE_RE = re.compile(r"[A-Za-z_]\w*(?:\.\w+)*")

# ``(module_path, exact)``: a module path a name is bound to, and whether the
# match is pinned to that module's own file. ``False`` keeps the re-export
# tolerance a package import needs (``from pkg import x`` may bind something
# ``pkg/__init__.py`` re-exported from ``pkg/sub.py``); ``True`` admits only
# ``pkg.py`` / ``pkg/__init__.py``, which is what an imported receiver's
# identity requires of its *containing* package.
_ModuleTarget = tuple[str, bool]

# The binding of a name more than one import statement rebinds. Python keeps
# only the last one, and this lexical reader cannot tell which statement runs
# last (a conditional or function-local import need not be the textually final
# one), so such a name is held to this unmatchable target instead of the union
# of its modules: unioning would accept a correction to the *shadowed*
# definition as evidence while the callee the call site actually reaches stays
# unchanged (PRRT_kwDOSJAM6s6q8-Mw). It is deliberately non-empty, so
# ``_callee_names_bound_to_candidate`` does not fall back to the name-only rule,
# and its module path is empty, which ``_candidate_is_under_module_path`` admits
# from no candidate — every changed file fails closed for that name.
_AMBIGUOUS_IMPORT_TARGET: frozenset[_ModuleTarget] = frozenset({("", False)})

# Stand-in module path for a ``from`` import this reader cannot resolve to a
# path — a relative import whose dots climb past the repo root. The statement
# still *rebinds* the name, so it has to count as one of the identities the
# target builders weigh: dropping it outright would leave the name holding
# another import's target, and would let a plain ``import`` still claim the name
# as a *proven module* receiver even though the call site reaches whatever the
# unresolvable import bound (PRRT_kwDOSJAM6s6q8-M1). It is not a path any
# candidate can match, so it only ever makes a name fail closed.
_UNRESOLVED_IMPORT_MODULE = "?"


def _import_binding_identity(module_path: str | None, imported: str) -> str:
    """The definition identity a ``from`` import binds a name to.

    Two statements binding one name to the same identity are a repeat of the
    same import; binding it to two identities is a rebinding, which fails closed
    (see ``_AMBIGUOUS_IMPORT_TARGET``). An unresolvable module path keeps a
    distinct identity rather than disappearing (see
    ``_UNRESOLVED_IMPORT_MODULE``).
    """
    if module_path is None:
        return f"{_UNRESOLVED_IMPORT_MODULE}/{imported}"
    return f"{module_path}/{imported}"


def _module_path_segments(path: str) -> list[str]:
    """``path`` as directory segments, with a Python module suffix dropped."""
    normalized = path.replace("\\", "/")
    stem, _dot, suffix = normalized.rpartition(".")
    if stem and f".{suffix.lower()}" in _PYTHON_CALL_SITE_SUFFIXES:
        normalized = stem
    return [segment for segment in normalized.split("/") if segment]


def _import_line_without_comment(line: str) -> str:
    """``line`` up to its first ``#``.

    A comment ends at its own physical line, but a parenthesized target list is
    joined line by line, so it has to be dropped *before* the join — otherwise
    one ``# note`` swallows every name listed below it, those names never bind,
    and the gate silently falls back to the name-only rule that accepts an
    unrelated same-named definition (PRRT_kwDOSJAM6s6q8BmK). Both import readers
    split on commas, so both strip the comment first. An import statement cannot
    hold a string literal, so scanning for a bare ``#`` is exact.
    """
    head, _hash, _comment = line.partition("#")
    return head


def _imported_binding_names(targets: str) -> list[tuple[str, str]]:
    """``(bound, imported)`` pairs an import target list binds.

    The two differ under ``orig as alias``: the call site refers to ``alias``,
    while ``orig`` is the name inside the target module — which is the identity
    a receiver resolves through, so both are kept.
    """
    names: list[tuple[str, str]] = []
    for piece in targets.replace("(", " ").replace(")", " ").split(","):
        parts = piece.split()
        if not parts or parts[0] == "*":
            continue
        aliased = len(parts) >= 3 and parts[1] == "as"
        names.append((parts[2] if aliased else parts[0], parts[0]))
    return names


def _relative_import_module_path(path: str, dots: str, module: str | None) -> str | None:
    """A relative import's target as a ``/``-joined path prefix, or None.

    One dot names ``path``'s own directory and each extra dot climbs one level,
    so the target is positional rather than name-based: it needs no package
    root to resolve. Returns None when the climb passes the repo root or when
    the dots name a directory with no segments, leaving the callee on the
    name-only rule rather than inventing a path it may not reach.
    """
    package = _module_path_segments(path)[:-1]
    ascend = len(dots) - 1
    if ascend > len(package):
        return None
    base = package[: len(package) - ascend]
    tail = module.split(".") if module else []
    return "/".join([*base, *tail]) or None


def _import_head_bracket_depths(lines: list[str]) -> list[int]:
    """Open-bracket depth each masked line begins at.

    An ``import`` head starts a logical line, so it can never sit inside an open
    bracket. The masked scan deliberately keeps f-string / template ``{...}``
    bodies scannable so interpolated calls stay visible to callee discovery,
    which leaves an import head quoted inside a *multi-line* interpolation
    readable as code. It is still inside the interpolation's brace, so holding
    heads to depth 0 drops that last decoy binding (PRRT_kwDOSJAM6s6q8MXB); a
    real import's parenthesized target list is unaffected because its head is
    itself at depth 0.
    """
    depths: list[int] = []
    depth = 0
    for line in lines:
        depths.append(depth)
        for char in line:
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth = max(depth - 1, 0)
    return depths


def _iter_from_import_bindings(
    file_text: str, *, path: str
) -> Iterator[tuple[str | None, str, str]]:
    """``(module_path, bound, imported)`` for each ``from`` import in ``file_text``.

    Both ``from M import ...`` and the relative ``from .M import ...`` form are
    read, including the parenthesized multi-line variant; ``module_path`` is the
    target module as a ``/``-joined path prefix, resolved against ``path``'s own
    directory for the relative form. Star imports and plain ``import M`` carry
    no name→path link, so the names they bind are not yielded and keep the
    name-only rule (PRRT_kwDOSJAM6s6q7bSI). Each physical line is read without
    its trailing comment so a ``# note`` beside one name does not drop the names
    below it (PRRT_kwDOSJAM6s6q8BmK). The scan runs over the same
    comment/string-masked lines definition discovery uses, so an import head
    quoted inside a docstring example binds nothing: it would otherwise union a
    decoy module into a readable binding, and because any stored path satisfies
    the candidate match, a correction to the *example's* module would then
    satisfy this gate for a call site that still routes elsewhere
    (PRRT_kwDOSJAM6s6q8MXB). Heads are read only at bracket depth 0, which is
    where a logical line starts, so a head the scan's interpolation retention
    left readable binds nothing either.

    ``module_path`` is None when the statement's target cannot be resolved to a
    path (a relative import climbing past the repo root). The names it binds are
    still yielded, because the statement rebinds them whether or not this reader
    can follow it: callers that need a path skip those bindings, while the ones
    that judge *rebinding* count them (PRRT_kwDOSJAM6s6q8-M1).
    """
    if f".{path.rsplit('.', 1)[-1].lower()}" not in _PYTHON_CALL_SITE_SUFFIXES:
        return
    module_path: str | None
    lines = _definition_head_scan_lines(file_text, path=path)
    depths = _import_head_bracket_depths(lines)
    index = 0
    while index < len(lines):
        if depths[index]:
            index += 1
            continue
        line = _import_line_without_comment(lines[index])
        absolute = _ABSOLUTE_FROM_IMPORT_RE.match(line)
        relative = None if absolute else _RELATIVE_FROM_IMPORT_RE.match(line)
        index += 1
        if absolute is not None:
            module_path = absolute.group(1).replace(".", "/")
            targets = absolute.group(2)
        elif relative is not None:
            module_path = _relative_import_module_path(path, relative.group(1), relative.group(2))
            targets = relative.group(3)
        else:
            continue
        # Consume the wrapped target list even when the head did not resolve,
        # so its names are not re-read as import heads on the next pass.
        while targets.count("(") > targets.count(")") and index < len(lines):
            targets += " " + _import_line_without_comment(lines[index]).strip()
            index += 1
        for bound, imported in _imported_binding_names(targets):
            yield module_path, bound, imported


def _bare_name_import_module_paths(file_text: str, *, path: str) -> dict[str, frozenset[str]]:
    """Module paths each name in ``file_text`` is bound to by a ``from`` import.

    A *bare* callee is reached through the module it was imported from, so that
    module is the whole binding; the imported name adds nothing to it.
    """
    bindings: dict[str, set[str]] = {}
    for module_path, bound, _imported in _iter_from_import_bindings(file_text, path=path):
        if module_path is None:
            continue
        bindings.setdefault(bound, set()).add(module_path)
    return {name: frozenset(paths) for name, paths in bindings.items()}


def _plain_import_module_paths(file_text: str, *, path: str) -> dict[str, frozenset[str]]:
    """Module paths each name a plain ``import`` statement binds as a receiver.

    ``import pkg.mod`` is called through as ``pkg.mod.record(...)``, so the
    receiver captured at the anchored line is the import's last segment;
    ``import pkg.mod as alias`` renames that receiver. Either way the imported
    module's own path is what a qualified callee reaches through it. Pieces that
    are not a dotted module name are skipped, and a name no plain import binds
    keeps the name-only rule. The comma split runs over the statement without
    its trailing comment, so a comma inside a ``# note`` cannot bind the word
    after it to a module the call site never imported — that receiver would then
    fail closed against every changed file (PRRT_kwDOSJAM6s6q8BmK). Lines come
    from the comment/string-masked scan, so a quoted ``import`` inside a
    docstring binds no receiver either (PRRT_kwDOSJAM6s6q8MXB), and a head read
    back from inside a retained interpolation is skipped with it.
    """
    if f".{path.rsplit('.', 1)[-1].lower()}" not in _PYTHON_CALL_SITE_SUFFIXES:
        return {}
    bindings: dict[str, set[str]] = {}
    scan_lines = _definition_head_scan_lines(file_text, path=path)
    depths = _import_head_bracket_depths(scan_lines)
    for depth, scan_line in zip(depths, scan_lines, strict=True):
        if depth:
            continue
        head = _PLAIN_IMPORT_RE.match(_import_line_without_comment(scan_line))
        if head is None:
            continue
        for piece in head.group(1).split(","):
            parts = piece.split()
            if not parts or _DOTTED_MODULE_RE.fullmatch(parts[0]) is None:
                continue
            segments = parts[0].split(".")
            alias = parts[2] if len(parts) >= 3 and parts[1] == "as" else segments[-1]
            bindings.setdefault(alias, set()).add("/".join(segments))
    return {name: frozenset(paths) for name, paths in bindings.items()}


def _bare_name_import_module_targets(
    file_text: str, *, path: str
) -> dict[str, frozenset[_ModuleTarget]]:
    """Bare-callee ``from`` import bindings as descendant-tolerant targets.

    ``from pkg import record`` may bind something ``pkg/__init__.py`` re-exports
    from a submodule, so the callee's definition is allowed anywhere under the
    imported module's path. A name two imports bind to *different* definitions
    is rebound rather than widened, so it fails closed (see
    ``_AMBIGUOUS_IMPORT_TARGET``); repeating the same import is not a rebinding
    and keeps its path.

    Rebinding is judged on the ``module/imported`` identity rather than on the
    module alone, the way ``_receiver_import_module_targets`` judges it: ``from
    pkg import record`` followed by ``from pkg import helper as record`` leaves
    the call reaching ``pkg``'s ``helper``, so a correction to a *same-named*
    ``record`` under ``pkg`` is no more evidence about the effective binding
    than a shadowed definition in another package is (PRRT_kwDOSJAM6s6q8-Mw).
    """
    identities: dict[str, set[str]] = {}
    for module_path, bound, imported in _iter_from_import_bindings(file_text, path=path):
        identities.setdefault(bound, set()).add(_import_binding_identity(module_path, imported))
    return {
        name: (
            _AMBIGUOUS_IMPORT_TARGET
            if len(identities[name]) > 1
            else frozenset((module_path, False) for module_path in paths)
        )
        for name, paths in _bare_name_import_module_paths(file_text, path=path).items()
    }


def _bare_name_imported_definition_names(file_text: str, *, path: str) -> dict[str, str]:
    """Imported symbol each bare-callee binding in ``file_text`` actually names.

    ``from pkg.mod import actual as alias`` makes ``alias()`` a call to
    ``pkg.mod``'s ``actual``: the *local* name narrows the candidate path, but
    the definition the span rule has to find is named ``actual``. Keeping only
    the local name both rejects a real correction to ``actual`` and accepts an
    edit to an unrelated ``alias`` that happens to live in the same module as
    evidence about the call (PRRT_kwDOSJAM6s6q9WnP).

    Only names whose imports agree on one imported symbol are mapped. A name
    several imports bind to *different* symbols is a rebinding that already
    fails closed on its path binding (see ``_AMBIGUOUS_IMPORT_TARGET``), so it
    keeps its local name here rather than this reader picking one of them.
    """
    imported_names: dict[str, set[str]] = {}
    for _module_path, bound, imported in _iter_from_import_bindings(file_text, path=path):
        imported_names.setdefault(bound, set()).add(imported)
    return {
        bound: next(iter(imported))
        for bound, imported in imported_names.items()
        if len(imported) == 1 and bound not in imported
    }


def _receiver_import_module_targets(
    file_text: str, *, path: str
) -> dict[str, frozenset[_ModuleTarget]]:
    """Module targets each *receiver* name at a call site is bound to.

    A qualified callee (``metrics.record()``) reaches its definition through its
    receiver, so the receiver's own import narrows which changed path can hold
    that definition (PRRT_kwDOSJAM6s6q7bSI). ``import pkg.metrics`` binds the
    module itself. ``from pkg import metrics`` keeps the *imported name's*
    identity rather than collapsing to its package: the receiver is either the
    submodule ``pkg/metrics`` — whose own ``__init__`` may re-export the callee
    — or an object ``pkg`` itself defines, so only ``pkg``'s own module file
    satisfies that second reading and a same-named definition in a sibling
    submodule such as ``pkg/unrelated.py``, which the receiver cannot reach,
    fails closed (PRRT_kwDOSJAM6s6q8MW9). A receiver with no readable binding —
    a parameter, an attribute, a star import — keeps the name-only rule rather
    than re-parking the #1019 fixes. A receiver several imports bind to
    different modules keeps only its last binding at runtime, so it fails closed
    instead of offering every module it was ever bound to (see
    ``_AMBIGUOUS_IMPORT_TARGET``); the two targets one ``from`` import yields are
    two readings of that single binding, not a rebinding, so they are counted as
    the one module they came from.
    """
    targets: dict[str, set[_ModuleTarget]] = {}
    bound_modules: dict[str, set[str]] = {}
    for module_path, bound, imported in _iter_from_import_bindings(file_text, path=path):
        bound_modules.setdefault(bound, set()).add(_import_binding_identity(module_path, imported))
        if module_path is None:
            continue
        targets.setdefault(bound, set()).update(
            ((f"{module_path}/{imported}", False), (module_path, True))
        )
    for name, paths in _plain_import_module_paths(file_text, path=path).items():
        bound_modules.setdefault(name, set()).update(paths)
        targets.setdefault(name, set()).update((module_path, False) for module_path in paths)
    return {
        name: (_AMBIGUOUS_IMPORT_TARGET if len(bound_modules[name]) > 1 else frozenset(found))
        for name, found in targets.items()
    }


def _module_bound_receiver_names(file_text: str, *, path: str) -> frozenset[str]:
    """Receiver names a plain ``import`` proves to be modules.

    ``import pkg.metrics as metrics`` binds a module object, so
    ``metrics.record()`` can only reach a *module-level* ``record`` in that
    module: a ``Collector.record`` defined beside it is an attribute of the
    class, never of the module, so a correction that edits only the method
    leaves the call site's actual callee untouched (PRRT_kwDOSJAM6s6q8-M1).
    Returning the receiver's binding *kind* is what lets the span rule below
    hold such a callee to module scope.

    A receiver a ``from`` import binds keeps the class-member tolerance — it may
    be the submodule or an object the imported module defines (see
    ``_receiver_import_module_targets``) — and so does a receiver no import
    binds, which keeps the name-only rule the #1019 fixes depend on. A name
    both forms bind is rebound rather than proven, so it is not claimed here and
    fails closed on ``_AMBIGUOUS_IMPORT_TARGET`` instead — including when the
    ``from`` import's own target is unresolvable, since such a statement rebinds
    the name all the same and so disproves the plain import's module identity
    just as a resolvable one does.
    """
    from_bound = {
        bound for _module_path, bound, _imported in _iter_from_import_bindings(file_text, path=path)
    }
    return frozenset(
        name for name in _plain_import_module_paths(file_text, path=path) if name not in from_bound
    )


def _names_bound_in_scope(scope: ast.AST) -> Iterator[str]:
    """Names the function scope ``scope`` binds inside its own body.

    Python locals are function-wide — a name assigned anywhere in a function is
    local throughout it — so the whole subtree is read rather than only the
    lines above the anchor. Parameters, assignment / loop / ``with`` targets,
    caught exceptions, function-local imports and nested definitions all count.
    Names a *nested* scope binds come out with them, which can only make a name
    fail closed. ``scope``'s own name does not: a definition's name is bound in
    the scope that *holds* it, so a method named like an imported helper does
    not shadow that import for the calls in its own body.
    """
    for node in ast.walk(scope):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            yield node.id
        elif isinstance(node, ast.arg):
            yield node.arg
        elif isinstance(node, (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef)):
            if node is not scope:
                yield node.name
        elif isinstance(node, ast.alias):
            yield node.asname or node.name.partition(".")[0]
        elif isinstance(node, ast.ExceptHandler) and node.name:
            yield node.name


def _locally_rebound_names_at_line(file_text: str, line: int, *, path: str) -> frozenset[str]:
    """Names a function scope enclosing ``line`` binds itself.

    An import binding is proof about a call only while the name still *holds*
    that import where the call is made. ``from pkg.mod import validate``
    followed by ``def run(validate): validate()`` leaves the call reaching the
    parameter, so a correction to ``pkg/mod.py``'s ``validate`` changes nothing
    the anchored line calls and must not satisfy this gate
    (PRRT_kwDOSJAM6s6q9WnX); the same goes for a local assignment, a loop or
    ``with`` target, a function-local import and a nested definition of the
    name.

    Only function scopes shadow: a class body's binding is an attribute of the
    class and is invisible to the calls inside its methods. A name *no* import
    binds is not reported on by this reader at all — it keeps the name-only rule
    the #1019 fixes and the parameter-receiver tolerance depend on, because an
    unknown local is exactly the unreadable binding that rule exists for. Text
    this reader cannot parse yields nothing, which leaves the lexical readers'
    bindings as they were rather than failing every import-bound callee closed
    on a parse error this probe cannot act on.
    """
    if f".{path.rsplit('.', 1)[-1].lower()}" not in _PYTHON_CALL_SITE_SUFFIXES:
        return frozenset()
    try:
        tree = ast.parse(file_text)
    except (SyntaxError, ValueError):
        return frozenset()
    bound: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef, ast.Lambda)):
            continue
        if node.lineno <= line <= (node.end_lineno or node.lineno):
            bound.update(_names_bound_in_scope(node))
    return frozenset(bound)


def _global_declared_names(scope: ast.AST) -> Iterator[str]:
    """Names ``scope`` or anything nested in it declares ``global``.

    ``global f`` exists only so an assignment reaches the module binding, so a
    function carrying one rebinds ``f`` at module scope even though the
    statement that does it sits in a body the module-scope reader never
    descends into.
    """
    for node in ast.walk(scope):
        if isinstance(node, ast.Global):
            yield from node.names


def _module_scope_rebound_names(file_text: str, *, path: str) -> frozenset[str]:
    """Names the module's own body binds besides its imports.

    An import proves a callee's module only while the name still holds that
    import, and a module global is rebindable from the module body as well as
    from a function scope: ``from pkg.mod import validate`` followed by
    ``validate = build_validator()`` leaves every later call — at module level
    or inside a function that reads the global — reaching the reassigned
    global, so a correction to ``pkg/mod.py``'s ``validate`` touches nothing the
    anchored line calls (PRRT_kwDOSJAM6s6q9WnX). This lexical reader cannot
    order the import against the rebinding, so any such name fails closed
    rather than being read in textual order.

    Collected module-wide rather than at the anchored line, because a module
    global is in scope for the whole file. Import aliases are *not* collected —
    they are the binding this gate exists to trust. Function and class bodies
    are not descended into: their bindings are locals and class attributes, and
    the function-scope ones are ``_locally_rebound_names_at_line``'s subject;
    only a ``global`` declaration inside them reaches back out. A top-level
    ``def`` / ``class`` of the name does shadow the import and is collected.
    Text this reader cannot parse yields nothing, matching the companion reader
    rather than failing every import-bound callee closed on a parse error.
    """
    if f".{path.rsplit('.', 1)[-1].lower()}" not in _PYTHON_CALL_SITE_SUFFIXES:
        return frozenset()
    try:
        tree = ast.parse(file_text)
    except (SyntaxError, ValueError):
        return frozenset()
    bound: set[str] = set()
    pending: list[ast.AST] = list(ast.iter_child_nodes(tree))
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef)):
            bound.add(node.name)
            bound.update(_global_declared_names(node))
            continue
        if isinstance(node, ast.Lambda):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        pending.extend(ast.iter_child_nodes(node))
    return frozenset(bound)


def _import_targets_without_locally_rebound(
    bindings: dict[str, frozenset[_ModuleTarget]], rebound: frozenset[str]
) -> dict[str, frozenset[_ModuleTarget]]:
    """``bindings`` with every locally rebound name held to the unmatchable target.

    A name the anchored scope — or the module body around it — binds itself
    does not reach its import, so it is
    held to ``_AMBIGUOUS_IMPORT_TARGET`` exactly as a name two imports rebind
    is: unmatchable, and deliberately non-empty so the name does not fall back
    to the name-only rule and accept any changed file that happens to carry a
    same-named definition (PRRT_kwDOSJAM6s6q9WnX).
    """
    return {
        name: (_AMBIGUOUS_IMPORT_TARGET if name in rebound else targets)
        for name, targets in bindings.items()
    }


def _candidate_is_under_module_path(
    candidate: str, module_path: str, *, call_site: str, exact: bool = False
) -> bool:
    """True when ``candidate`` is the imported module's file or sits inside it.

    Matched as a contiguous segment run so a source root (``src/``) is tolerated
    and a package import whose ``__init__`` re-exports the callee still reaches
    the submodule that defines it. The run's prefix must be a prefix of
    ``call_site``'s own directory, which is the only root the absolute import at
    that call site is shown to resolve against: a mirrored path under an
    unrelated root (``tests/pkg_b/metrics.py`` for ``from pkg_b.metrics import
    ...``) is not importable from the call site, so an ambiguous root fails
    closed (PRRT_kwDOSJAM6s6q791s). A facade that re-exports *across* packages
    is not followed and fails closed either, leaving the item on the #928
    escalation path rather than accepting a path the call site cannot be shown
    to reach.

    ``exact`` drops the descendant tolerance: the run must end at ``candidate``,
    so only that module's own file — ``M.py`` or the package's ``M/__init__.py``
    — matches and a sibling submodule under it does not
    (PRRT_kwDOSJAM6s6q8MW9). An empty ``module_path`` names no module and so
    matches nothing, which is what makes ``_AMBIGUOUS_IMPORT_TARGET`` fail
    closed.
    """
    segments = _module_path_segments(candidate)
    if exact and segments[-1:] == ["__init__"]:
        segments = segments[:-1]
    wanted = _module_path_segments(module_path)
    if not wanted or len(wanted) > len(segments):
        return False
    roots = _module_path_segments(call_site)[:-1]
    last = len(segments) - len(wanted)
    starts = [last] if exact else range(last + 1)
    return any(
        segments[start : start + len(wanted)] == wanted and segments[:start] == roots[:start]
        for start in starts
    )


def _callee_names_bound_to_candidate(
    refs: frozenset[tuple[str, str]],
    bindings: dict[str, frozenset[_ModuleTarget]],
    candidate: str,
    *,
    call_site: str,
) -> frozenset[str]:
    """Callee names whose binding key, when readable, admits ``candidate``.

    The key is a bare callee's own name or a qualified callee's receiver; a key
    with no readable import binding keeps the name-only rule.
    """
    return frozenset(
        name
        for key, name in refs
        if not bindings.get(key)
        or any(
            _candidate_is_under_module_path(
                candidate, module_path, call_site=call_site, exact=exact
            )
            for module_path, exact in bindings[key]
        )
    )


def _definition_is_reachable_from_module_scope(
    file_text: str,
    all_spans: list[tuple[str, int, int, int]],
    *,
    start: int,
    indent: int,
) -> bool:
    """True when every definition enclosing ``start`` is a class.

    A method of a module-level class is importable as ``Class.method``; a
    closure defined inside a function is not reachable from another module at
    all, so it can never be the callee of a cross-file call site.
    """
    return all(
        _definition_span_is_class(file_text, span_start)
        for _name, span_start, span_end, span_indent in all_spans
        if span_start < start <= span_end and span_indent < indent
    )


def _definition_span_start_with_decorators(file_text: str, start: int) -> int:
    """``start`` moved up over the definition's contiguous decorator stack.

    A correction that changes only a decorator — ``@staticmethod`` to
    ``@classmethod``, a retry/auth decorator's arguments — changes the callee
    without touching its ``def`` line or body, so the decorators have to sit
    inside the span the overlap check runs against (PRRT_kwDOSJAM6s6q791u).
    Blank/comment gaps and multiline decorator call tails stay inside the stack;
    ordinary code above an undecorated head does not (an unbalanced closer only
    keeps the walk alive while a decorator head is still pending below).
    """
    lines = file_text.splitlines()
    if start < 2 or start > len(lines):
        return start
    extended = start
    depth = 0
    for idx in range(start - 2, -1, -1):
        raw = lines[idx]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        depth += stripped.count(")") + stripped.count("]") + stripped.count("}")
        depth -= stripped.count("(") + stripped.count("[") + stripped.count("{")
        if depth <= 0 and _DECORATOR_BASENAME_RE.match(raw) is not None:
            extended = idx + 1
            depth = 0
            continue
        if depth > 0:
            continue
        break
    return extended


def _diff_adds_decorators_above_span(diff_text: str, start: int) -> bool:
    """True when the range attaches a new decorator stack directly above ``start``.

    A unified diff anchors a pure insert *after* its old-side line, so decorating
    a previously bare callee — adding ``@retry(...)`` / ``@staticmethod`` — reports
    ``start - 1`` and overlaps no line of the definition span, even though the
    inserted lines become part of that definition (PRRT_kwDOSJAM6s6q791u).
    Accepted only when the insert carries a decorator and no definition head of
    its own, so inserting an unrelated function above the callee is still not
    evidence about the callee.
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _iter_unified_diff_old_hunks,
    )

    for old_start, old_count, added_lines in _iter_unified_diff_old_hunks(diff_text):
        if old_count != 0 or old_start != start - 1:
            continue
        if any(_ENCLOSING_DEFINITION_RE.match(line) for line in added_lines):
            continue
        if any(_DECORATOR_BASENAME_RE.match(line) for line in added_lines):
            return True
    return False


def _importable_definition_spans_for_names(
    file_text: str,
    names: frozenset[str],
    *,
    path: str | None = None,
    bare_names: frozenset[str] = frozenset(),
) -> list[tuple[int, int]]:
    """Spans in ``file_text`` another module could reach as the named callee.

    Returns ``(start, end)`` line spans for every ``def`` / ``class`` /
    ``function`` / arrow head whose name matches, under the reachability rule
    its *call shape* allows. ``names`` are attribute-qualified callees
    (``metrics.record()``), whose receiver may be a module or an instance, so a
    direct member of a module-level class counts alongside a module-level head.
    ``bare_names`` are the names held to the module-scope rule, under which
    ``Class.method`` is reachable as an attribute but never as a module-scope
    name, so a class member fails closed: bare callees (``record()``), which
    another module can only reach through a module-scope binding
    (PRRT_kwDOSJAM6s6q699Q) — matching the module-scope candidate rule attempt
    0's ``_resolve_callee_definition_span`` applies to bare calls — and
    qualified callees whose receiver a plain ``import`` proves to be a *module*
    (``import pkg.metrics as metrics``), since a method of a class in that
    module is not an attribute of the module and so is never that call's callee
    (PRRT_kwDOSJAM6s6q8-M1); the caller sorts the two by binding kind. A name
    called both ways on the anchored line keeps the attribute rule.

    Function-local closures, indented JS/TS heads and indented assignment
    bindings are block-scoped or unreachable and fail closed under both rules.

    Each span starts at the head's topmost contiguous decorator, so a correction
    that only swaps a decorator still overlaps the callee's definition.
    """
    if not (names or bare_names) or not file_text:
        return []
    all_spans = _iter_definition_spans(file_text, path=path)
    js_ts = _path_allows_js_private_fields(path)
    spans: list[tuple[int, int]] = []
    for name, start, end, indent in all_spans:
        qualified = name in names
        if not qualified and name not in bare_names:
            continue
        if indent > 0 and (js_ts or _definition_head_is_assignment(file_text, start)):
            continue
        if qualified:
            if not _definition_is_reachable_from_module_scope(
                file_text, all_spans, start=start, indent=indent
            ):
                continue
        elif _definition_is_nested_in_other(all_spans, start=start, indent=indent):
            continue
        spans.append((_definition_span_start_with_decorators(file_text, start), end))
    return spans


async def _callee_definition_survives_at_right(
    self: Any,
    *,
    worktree_path: Path,
    right: str,
    path: str,
    names: frozenset[str],
    bare_names: frozenset[str],
) -> bool:
    """True when ``path`` still holds a reachable definition of the callee at ``right``.

    The spans the overlap check runs against are read at ``left``, so a
    correction that *deletes* the callee — its whole file, or just its
    definition out of a surviving file — produces a deletion hunk overlapping
    the old span and would otherwise read as evidence that the reviewed call
    site was fixed, while the unchanged caller now references a missing callee
    (PRRT_kwDOSJAM6s6q8MWy). ``path`` is the candidate's rename target when the
    range moved it, so an already-accepted move keeps resolving. Fails closed
    when the right-side text is unreadable.
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _path_text_at_ref,
    )

    right_text = await _path_text_at_ref(self, worktree_path=worktree_path, ref=right, path=path)
    if not right_text:
        return False
    return bool(
        _importable_definition_spans_for_names(right_text, names, path=path, bare_names=bare_names)
    )


async def _path_diff_text_in_commit_range(
    self: Any,
    *,
    worktree_path: Path,
    left: str,
    right: str,
    path: str,
    rename_path: str | None = None,
) -> str | None:
    """``git diff -U0 left right -- path [rename_path]``, or None when unreadable.

    ``rename_path`` is ``path``'s rename target in the range, when it has one.
    Both sides must sit in the pathspec or git cannot pair the move: pathspec
    filtering runs before rename detection, so an old-path-only diff of a pure
    rename reads as a whole-file deletion whose hunk overlaps every definition
    span in the file. Passing both paths keeps a move with an unchanged body
    hunkless, and keeps a move that also edits the body expressed as old-side
    hunks in ``path``'s line numbering (PRRT_kwDOSJAM6s6q65JH).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _GIT_DIFF_FIND_RENAMES,
        _git_env_for_merge_safety_object_lookup,
    )

    pathspec = [path] if rename_path is None else [path, rename_path]
    result = await self._deps.runner.run(
        git_worktree_command(
            worktree_path,
            "diff",
            _GIT_DIFF_FIND_RENAMES,
            "-U0",
            left,
            right,
            "--",
            *pathspec,
        ),
        env=_git_env_for_merge_safety_object_lookup(),
    )
    if not result.ok:
        return None
    raw = result.stdout_bytes
    if raw is not None:
        return cast(str, raw.decode("utf-8", errors="surrogateescape"))
    return cast(str, result.stdout or "")


def _cross_file_callee_names(
    file_text: str, line: int, *, path: str
) -> tuple[frozenset[tuple[str, str]], frozenset[tuple[str, str]]]:
    """Callee refs at ``line`` that may resolve in another file.

    Returns ``(attribute_qualified, bare)`` separately because the two call
    shapes reach different definitions across a module boundary — see
    ``_importable_definition_spans_for_names``. Each ref is a
    ``(binding_key, name)`` pair, the key being the name whose import binding
    narrows the candidate path: a qualified callee binds through its receiver, a
    bare callee through itself. The two differ for an aliased bare callee, whose
    key is the local binding while the definition to look for carries the
    imported symbol's name (PRRT_kwDOSJAM6s6q9WnP); a qualified callee's name is
    an attribute of its receiver, which no import renames.
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_callees import (
        _callee_refs_from_file_line,
    )

    refs = _callee_refs_from_file_line(file_text, line, path=path)
    qualified = frozenset(
        (qualifier, name)
        for qualifier, name in refs
        if qualifier is not None and qualifier not in _IN_FILE_CALLEE_QUALIFIERS
    )
    imported = _bare_name_imported_definition_names(file_text, path=path)
    bare = frozenset(
        (name, imported.get(name, name)) for qualifier, name in refs if qualifier is None
    )
    return qualified, bare


async def _commit_range_changes_callee_definition(
    self: Any,
    *,
    worktree_path: Path,
    left: str,
    right: str,
    item_path: str,
    item_line: int,
) -> bool:
    """True when ``left``..``right`` changes a callee's definition in another file.

    Resolves the callee(s) referenced at ``item_line`` of ``item_path`` as of
    ``left`` — the side ``-U0`` hunk headers and the definition spans are both
    expressed in — then, for each other path the range changed, requires a
    module-reachable definition of one of those names whose span the range's
    diff overlaps. Each callee is additionally held to the module its own import
    binds — a bare callee through its ``from`` import, a qualified callee
    through its receiver — so a same-named definition in a module the call site
    never imported is not evidence (PRRT_kwDOSJAM6s6q7bSI). A callee whose
    binding is unreadable keeps the name-only rule, which is the #1019 shape the
    gate exists for; a receiver imported by name resolves to that name's own
    module or to the importing module's file, not to any sibling under its
    package (PRRT_kwDOSJAM6s6q8MW9). The receiver's binding *kind* is kept too:
    a receiver a plain ``import`` proves to be a module reaches only
    module-level definitions, so editing a same-named method of a class in that
    module is not evidence about the call (PRRT_kwDOSJAM6s6q8-M1). A name two
    imports rebind resolves to one of them at runtime, so it is held to neither
    rather than to their union, whether or not both targets resolve to a path
    (PRRT_kwDOSJAM6s6q8-Mw). An aliased bare callee is looked for under the
    *imported* symbol's name rather than its local binding, so a correction to
    the definition it really reaches counts and an edit to an unrelated
    same-named definition in that module does not (PRRT_kwDOSJAM6s6q9WnP).
    A name the anchored line's own scope binds — a parameter, a local
    assignment, a nested definition — reaches that binding rather than the
    import, so it is held closed the same way instead of pointing the gate at
    the imported module's same-named definition (PRRT_kwDOSJAM6s6q9WnX).
    A candidate that the range renamed is diffed against its
    rename target too, so a pure move of the callee's file is not mistaken for a
    change to its body. An overlap is accepted only when *that* callee is still
    reachable at ``right``, so a correction that deletes the definition — or its
    whole file — is not read as a fix of a caller that still calls it, and a
    surviving sibling callee from the same module does not stand in for it
    (PRRT_kwDOSJAM6s6q8MWy). ``item_path`` itself is skipped: a same-path change
    is what the line-anchored and path-level gates already answer. Fails closed
    on any unreadable Git output.
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _changed_paths_in_commit_range,
        _diff_hunk_overlaps_line_span,
        _normalize_evidence_item_path,
        _path_text_at_ref,
        _rename_map_in_commit_range,
    )

    normalized_item = _normalize_evidence_item_path(item_path)
    if not normalized_item or item_line < 1:
        return False
    item_text = await _path_text_at_ref(
        self, worktree_path=worktree_path, ref=left, path=normalized_item
    )
    if not item_text:
        return False
    names, bare_names = _cross_file_callee_names(item_text, item_line, path=normalized_item)
    if not (names or bare_names):
        return False
    candidates = [
        normalized
        for normalized in (
            _normalize_evidence_item_path(changed)
            for changed in await _changed_paths_in_commit_range(
                self, worktree_path=worktree_path, left=left, right=right
            )
        )
        if normalized and normalized != normalized_item
    ][:_MAX_CALLEE_EVIDENCE_CANDIDATE_PATHS]
    if not candidates:
        return False
    rename_map, _name_status_z = await _rename_map_in_commit_range(
        self, worktree_path=worktree_path, left=left, right=right
    )
    rebound = _locally_rebound_names_at_line(
        item_text, item_line, path=normalized_item
    ) | _module_scope_rebound_names(item_text, path=normalized_item)
    bare_bindings = _import_targets_without_locally_rebound(
        _bare_name_import_module_targets(item_text, path=normalized_item), rebound
    )
    receiver_bindings = _import_targets_without_locally_rebound(
        _receiver_import_module_targets(item_text, path=normalized_item), rebound
    )
    # A receiver a plain ``import`` binds is a module, so its callee is held to
    # module scope like a bare one; every other receiver may be an instance and
    # keeps the class-member tolerance (PRRT_kwDOSJAM6s6q8-M1).
    module_receivers = _module_bound_receiver_names(item_text, path=normalized_item)
    module_refs = frozenset(ref for ref in names if ref[0] in module_receivers)
    for candidate in candidates:
        candidate_names = _callee_names_bound_to_candidate(
            names - module_refs, receiver_bindings, candidate, call_site=normalized_item
        )
        candidate_bare = _callee_names_bound_to_candidate(
            bare_names, bare_bindings, candidate, call_site=normalized_item
        ) | _callee_names_bound_to_candidate(
            module_refs, receiver_bindings, candidate, call_site=normalized_item
        )
        if not (candidate_names or candidate_bare):
            continue
        candidate_text = await _path_text_at_ref(
            self, worktree_path=worktree_path, ref=left, path=candidate
        )
        if not candidate_text:
            continue
        # Spans are resolved one callee at a time so the survival check is held
        # to the name whose definition the range actually touched: the anchored
        # line can bind several callees to the same candidate, and a surviving
        # sibling is no evidence that the deleted one's caller was fixed
        # (PRRT_kwDOSJAM6s6q8MWy).
        per_callee: list[tuple[frozenset[str], frozenset[str], list[tuple[int, int]]]] = []
        for name in sorted(candidate_names | candidate_bare):
            one = frozenset({name})
            one_names, one_bare = candidate_names & one, candidate_bare & one
            spans = _importable_definition_spans_for_names(
                candidate_text, one_names, path=candidate, bare_names=one_bare
            )
            if spans:
                per_callee.append((one_names, one_bare, spans))
        if not per_callee:
            continue
        diff_text = await _path_diff_text_in_commit_range(
            self,
            worktree_path=worktree_path,
            left=left,
            right=right,
            path=candidate,
            rename_path=rename_map.get(candidate),
        )
        if diff_text is None:
            continue
        for one_names, one_bare, spans in per_callee:
            # Survival is a property of the callee, not of one of its spans, so
            # the first overlap settles this name; another callee bound to the
            # same candidate may still carry its own evidence.
            if not any(
                _diff_hunk_overlaps_line_span(diff_text, start, end, file_text=candidate_text)
                or _diff_adds_decorators_above_span(diff_text, start)
                for start, end in spans
            ):
                continue
            if await _callee_definition_survives_at_right(
                self,
                worktree_path=worktree_path,
                right=right,
                path=rename_map.get(candidate) or candidate,
                names=one_names,
                bare_names=one_bare,
            ):
                return True
    return False
