"""Import-binding readers for the cross-file callee-evidence probe (issue #1019).

Split out of ``..._ancestry_cross_file.py`` so that module stays under the
first-party line guardrail (``MAX_FIRST_PARTY_FILE_LINES``); every name here is
re-exported there, so existing imports and test references keep working.

This is the binding layer of the cross-file rule: it reads a call site's own
``import`` statements and the scopes around the anchored line, and reports which
module path — if any — each callee name or receiver is bound to. The span rule
in the parent module then holds a changed candidate path to that binding, so a
same-named definition the call site cannot reach is not FIXED evidence.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator

from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_callees import (
    _ANCHORED_SCOPES,
    _anchored_class_scope,
    _definition_head_scan_lines,
    _names_bound_in_scope,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_import_lines import (
    _import_head_bracket_depths,
    _import_line_without_comment,
    _import_line_without_continuation,
    _import_logical_statements,
    _import_statement_and_trailing,
    _imported_binding_names,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_rebinding import (
    _import_lines_hidden_from,
)

# Suffixes whose bare-name bindings the import reader below understands. A bare
# callee in any other language keeps the name-only rule.
_PYTHON_CALL_SITE_SUFFIXES = frozenset({".py", ".pyi"})

# ``from pkg.mod import a, b as c`` — absolute targets. A plain ``import
# pkg.mod`` binds ``pkg`` rather than the callee, so it narrows no *bare*
# candidate path and is not matched here; relative targets are read below, and
# the plain form is read for receivers by ``_receiver_import_module_targets``.
# Either head's keyword also ends at the ``(`` of a parenthesized target list,
# so both admit that boundary: held to whitespace, the real head ``from pkg
# import(record)`` matches nothing, ``record`` binds to no module and keeps the
# name-only rule that accepts an unrelated same-named definition in another
# package (PRRT_kwDOSJAM6s6rBbdr).
_ABSOLUTE_FROM_IMPORT_RE = re.compile(
    r"^[ \t]*from[ \t]+([A-Za-z_]\w*(?:\.\w+)*)[ \t]+import(?:[ \t]+|(?=\())(.+)$"
)

# ``from .mod import x`` / ``from ..pkg.mod import x`` / ``from . import x`` —
# the leading dots and the optional module tail, resolved against the call
# site's own directory by ``_relative_import_module_path``.
_RELATIVE_FROM_IMPORT_RE = re.compile(
    r"^[ \t]*from[ \t]+(\.+)(\w+(?:\.\w+)*)?[ \t]+import(?:[ \t]+|(?=\())(.+)$"
)

# ``import pkg.mod`` / ``import pkg.mod as alias`` — the statement binds a
# module, which is the receiver shape ``pkg.mod.record(...)`` and
# ``alias.record(...)`` call through, so it does narrow a *qualified* callee.
_PLAIN_IMPORT_RE = re.compile(r"^[ \t]*import[ \t]+(.+)$")
_DOTTED_MODULE_RE = re.compile(r"[A-Za-z_]\w*(?:\.\w+)*")

# ``(module_path, exact, enclosed_by)``: a module path a name is bound to,
# whether the match is pinned to that module's own file, and the symbol that
# must *enclose* the callee's definition under this reading. ``exact=False``
# keeps the re-export tolerance a package import needs (``from pkg import x``
# may bind something ``pkg/__init__.py`` re-exported from ``pkg/sub.py``);
# ``True`` admits only ``pkg.py`` / ``pkg/__init__.py``, which is what an
# imported receiver's identity requires of its *containing* package.
# ``enclosed_by`` is None for every target that places no scope requirement of
# its own, and the imported symbol for the receiver reading whose module file
# the target admits — see ``_receiver_import_module_targets``.
_ModuleTarget = tuple[str, bool, str | None]

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
_AMBIGUOUS_IMPORT_TARGET: frozenset[_ModuleTarget] = frozenset({("", False, None)})

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


def _plain_import_binding_identity(module_path: str) -> str:
    """The definition identity a plain ``import`` binds a receiver name to.

    Namespaced apart from ``_import_binding_identity`` because the two forms
    bind *different* objects even when their module paths spell the same
    string: ``import pkg.mod as m`` binds the submodule, while ``from pkg
    import mod as m`` binds whatever ``pkg`` exposes under that name. Sharing
    one string would read the pair as a repeat of a single import, so the
    rebinding guard would not fire — while
    ``_module_bound_receiver_names`` has already dropped the proven-module
    restriction for a name both forms bind, leaving the receiver *more*
    tolerant than the plain import alone and accepting a same-named class
    method as evidence (PRRT_kwDOSJAM6s6q9Xo3). The ``import `` prefix cannot
    collide with a ``from`` identity, whose own prefix is a dotted module path.
    """
    return f"import {module_path}"


def _module_path_segments(path: str) -> list[str]:
    """``path`` as directory segments, with a Python module suffix dropped."""
    normalized = path.replace("\\", "/")
    stem, _dot, suffix = normalized.rpartition(".")
    if stem and f".{suffix.lower()}" in _PYTHON_CALL_SITE_SUFFIXES:
        normalized = stem
    return [segment for segment in normalized.split("/") if segment]


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


def _iter_from_import_bindings(
    file_text: str, *, path: str, line: int | None = None
) -> Iterator[tuple[str | None, str, str]]:
    """``(module_path, bound, imported)`` for each ``from`` import in ``file_text``.

    Both ``from M import ...`` and the relative ``from .M import ...`` form are
    read, including the parenthesized multi-line variant and the backslash-
    continued one, whose physical lines are joined into the logical statement
    before the heads are matched (PRRT_kwDOSJAM6s6rAf0X); a statement that
    follows the wrapped list on its closing line is split back off the joined
    targets and matched as a head of its own (PRRT_kwDOSJAM6s6rBTTM).
    ``module_path`` is the
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
    that judge *rebinding* count them (PRRT_kwDOSJAM6s6q8-M1). ``line`` scopes
    the scan to the heads visible there (see ``_import_lines_hidden_from``).
    """
    if f".{path.rsplit('.', 1)[-1].lower()}" not in _PYTHON_CALL_SITE_SUFFIXES:
        return
    module_path: str | None
    hidden = _import_lines_hidden_from(file_text, line)
    lines = _definition_head_scan_lines(file_text, path=path)
    depths = _import_head_bracket_depths(lines)
    index = 0
    while index < len(lines):
        if depths[index]:
            index += 1
            continue
        head_line = index + 1
        pending, index = _import_logical_statements(lines, index)
        while pending:
            statement = pending.pop(0)
            absolute = _ABSOLUTE_FROM_IMPORT_RE.match(statement)
            relative = None if absolute else _RELATIVE_FROM_IMPORT_RE.match(statement)
            if absolute is not None:
                module_path = absolute.group(1).replace(".", "/")
                targets = absolute.group(2)
            elif relative is not None:
                module_path = _relative_import_module_path(
                    path, relative.group(1), relative.group(2)
                )
                targets = relative.group(3)
            else:
                continue
            # Consume the wrapped target list even when the head did not
            # resolve, so its names are not re-read as import heads later.
            while targets.count("(") > targets.count(")") and index < len(lines):
                joined = _import_line_without_comment(lines[index])
                targets += " " + _import_line_without_continuation(joined).strip()
                index += 1
            # The statements that follow the now-complete one are still embedded
            # in its target list, so they are split off and matched as heads of
            # their own (see ``_import_statement_and_trailing``).
            targets, trailing = _import_statement_and_trailing(targets)
            pending.extend(trailing)
            if head_line in hidden:
                continue
            for bound, imported in _imported_binding_names(targets):
                yield module_path, bound, imported


def _bare_name_import_module_paths(
    file_text: str, *, path: str, line: int | None = None
) -> dict[str, frozenset[str]]:
    """Module paths each name in ``file_text`` is bound to by a ``from`` import.

    A *bare* callee is reached through the module it was imported from, so that
    module is the whole binding; the imported name adds nothing to it.
    """
    bindings: dict[str, set[str]] = {}
    for module_path, bound, _name in _iter_from_import_bindings(file_text, path=path, line=line):
        if module_path is None:
            continue
        bindings.setdefault(bound, set()).add(module_path)
    return {name: frozenset(paths) for name, paths in bindings.items()}


def _plain_import_module_paths(
    file_text: str, *, path: str, line: int | None = None
) -> dict[str, frozenset[str]]:
    """Module paths each name a plain ``import`` statement binds as a receiver.

    ``import pkg.mod`` is called through as ``pkg.mod.record(...)``, so the
    receiver captured at the anchored line is the import's last segment;
    ``import pkg.mod as alias`` renames that receiver. Either way the imported
    module's own path is what a qualified callee reaches through it. The unaliased form
    also binds the package root Python puts in the namespace, because ``pkg.record()``
    reaches an attribute of ``pkg`` itself: left unbound that root keeps its callee on
    the name-only rule, so a correction to a same-named definition in another package
    resolves the thread (PRRT_kwDOSJAM6s6rAAWV). An alias binds no root. Pieces that
    are not a dotted module name are skipped, and a name no plain import binds
    keeps the name-only rule. The comma split runs over the *logical* statement:
    backslash-continued physical lines are joined first, so a receiver listed
    after the marker binds the module it names instead of nothing
    (PRRT_kwDOSJAM6s6rA-rt), semicolon-separated statements are matched one by
    one, and a wrapped ``from`` target list that precedes a plain import on its
    closing line is consumed so that import is still read as a head of its own
    (PRRT_kwDOSJAM6s6rBTTM). The statement is read without its trailing
    comment, so a comma inside a ``# note`` cannot bind the word after it to a
    module the call site never imported — that receiver would then fail closed
    against every changed file (PRRT_kwDOSJAM6s6q8BmK). Lines come
    from the comment/string-masked scan, so a quoted ``import`` inside a
    docstring binds no receiver either (PRRT_kwDOSJAM6s6q8MXB), and a head read
    back from inside a retained interpolation is skipped with it. ``line``
    scopes the scan to the heads visible there (``_import_lines_hidden_from``).
    """
    if f".{path.rsplit('.', 1)[-1].lower()}" not in _PYTHON_CALL_SITE_SUFFIXES:
        return {}
    bindings: dict[str, set[str]] = {}
    hidden = _import_lines_hidden_from(file_text, line)
    scan_lines = _definition_head_scan_lines(file_text, path=path)
    depths = _import_head_bracket_depths(scan_lines)
    index = 0
    while index < len(scan_lines):
        if depths[index]:
            index += 1
            continue
        head_line = index + 1
        pending, index = _import_logical_statements(scan_lines, index)
        while pending:
            statement = pending.pop(0)
            # A ``from`` head's wrapped target list is consumed here too, so the
            # plain import that follows it on the closing line is matched as a
            # head of its own instead of staying hidden behind the bracket-depth
            # gate (PRRT_kwDOSJAM6s6rBTTM).
            while statement.count("(") > statement.count(")") and index < len(scan_lines):
                joined = _import_line_without_comment(scan_lines[index])
                statement += " " + _import_line_without_continuation(joined).strip()
                index += 1
            statement, trailing = _import_statement_and_trailing(statement)
            pending.extend(trailing)
            head = _PLAIN_IMPORT_RE.match(statement)
            if head is None or head_line in hidden:
                continue
            for piece in head.group(1).split(","):
                parts = piece.split()
                if not parts or _DOTTED_MODULE_RE.fullmatch(parts[0]) is None:
                    continue
                segments = parts[0].split(".")
                aliased = len(parts) >= 3 and parts[1] == "as"
                alias = parts[2] if aliased else segments[-1]
                bindings.setdefault(alias, set()).add("/".join(segments))
                if not aliased:
                    bindings.setdefault(segments[0], set()).add(segments[0])
    return {name: frozenset(paths) for name, paths in bindings.items()}


def _bare_name_import_module_targets(
    file_text: str, *, path: str, line: int | None = None
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
    Both readings see only the imports ``line`` can, so two sibling functions
    lazily importing one local name are two bindings (PRRT_kwDOSJAM6s6rA-ru).
    """
    identities: dict[str, set[str]] = {}
    for module_path, bound, imported in _iter_from_import_bindings(file_text, path=path, line=line):
        identities.setdefault(bound, set()).add(_import_binding_identity(module_path, imported))
    return {
        name: (
            _AMBIGUOUS_IMPORT_TARGET
            if len(identities[name]) > 1
            else frozenset((module_path, False, None) for module_path in paths)
        )
        for name, paths in _bare_name_import_module_paths(file_text, path=path, line=line).items()
    }


def _bare_name_imported_definition_names(
    file_text: str, *, path: str, line: int | None = None
) -> dict[str, str]:
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
    for _path, bound, imported in _iter_from_import_bindings(file_text, path=path, line=line):
        imported_names.setdefault(bound, set()).add(imported)
    return {
        bound: next(iter(imported))
        for bound, imported in imported_names.items()
        if len(imported) == 1 and bound not in imported
    }


def _receiver_import_module_targets(
    file_text: str, *, path: str, line: int | None = None
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
    fails closed (PRRT_kwDOSJAM6s6q8MW9). That second reading also carries the
    *imported symbol* with it: the object is the one ``pkg`` binds under that
    name, so the callee has to be a member of it and a same-named method of
    another class in ``pkg.py`` is not evidence about the call
    (PRRT_kwDOSJAM6s6q-L4H). A receiver with no readable binding —
    a parameter, an attribute, a star import — keeps the name-only rule rather
    than re-parking the #1019 fixes. A receiver several imports bind to
    different modules keeps only its last binding at runtime, so it fails closed
    instead of offering every module it was ever bound to (see
    ``_AMBIGUOUS_IMPORT_TARGET``); the two targets one ``from`` import yields are
    two readings of that single binding, not a rebinding, so they are counted as
    the one module they came from. The two *forms* are counted apart even when
    their module paths coincide, because ``import pkg.mod as m`` and ``from pkg
    import mod as m`` bind different objects (see
    ``_plain_import_binding_identity``). Both forms are read only where
    ``line`` can see them (PRRT_kwDOSJAM6s6rA-ru).
    """
    targets: dict[str, set[_ModuleTarget]] = {}
    bound_modules: dict[str, set[str]] = {}
    for module_path, bound, imported in _iter_from_import_bindings(file_text, path=path, line=line):
        bound_modules.setdefault(bound, set()).add(_import_binding_identity(module_path, imported))
        if module_path is None:
            continue
        targets.setdefault(bound, set()).update(
            ((f"{module_path}/{imported}", False, None), (module_path, True, imported))
        )
    for name, paths in _plain_import_module_paths(file_text, path=path, line=line).items():
        bound_modules.setdefault(name, set()).update(
            _plain_import_binding_identity(module_path) for module_path in paths
        )
        targets.setdefault(name, set()).update((module_path, False, None) for module_path in paths)
    return {
        name: (_AMBIGUOUS_IMPORT_TARGET if len(bound_modules[name]) > 1 else frozenset(found))
        for name, found in targets.items()
    }


def _module_bound_receiver_names(
    file_text: str, *, path: str, line: int | None = None
) -> frozenset[str]:
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
    bindings = _iter_from_import_bindings(file_text, path=path, line=line)
    from_bound = {bound for _module_path, bound, _imported in bindings}
    plain = _plain_import_module_paths(file_text, path=path, line=line)
    return frozenset(name for name in plain if name not in from_bound)


def _locally_rebound_names_at_line(file_text: str, line: int, *, path: str) -> frozenset[str]:
    """Names a scope enclosing ``line`` binds itself.

    An import binding is proof about a call only while the name still *holds*
    that import where the call is made. ``from pkg.mod import validate``
    followed by ``def run(validate): validate()`` leaves the call reaching the
    parameter, so a correction to ``pkg/mod.py``'s ``validate`` changes nothing
    the anchored line calls and must not satisfy this gate
    (PRRT_kwDOSJAM6s6q9WnX); the same goes for a local assignment, a loop or
    ``with`` target and a nested definition of the name.

    A function-local ``import`` of the name is *not* such a binding: it is the
    binding the import readers above read — they match indented statements too
    — so reporting it here would invalidate the very evidence it supplies and
    park a correction confined to the module it names as ``needs_human``
    (PRRT_kwDOSJAM6s6rAhm2). A second import binding the name to a different
    definition is still caught, by those readers' own rebinding guard (see
    ``_AMBIGUOUS_IMPORT_TARGET``), and a scope that binds the name some *other*
    way as well still reports it through that binding. This matches
    ``_module_scope_rebound_names``, which leaves import aliases uncollected
    for the same reason.

    A class body's binding is an attribute of the class and is invisible to the
    calls inside its methods, but a line executing *directly* in the class body
    resolves through the class namespace — ``class C: validate = local;
    result = validate()`` calls the attribute — so the class body the anchor
    sits in directly is read as well (PRRT_kwDOSJAM6s6q_ywe). A comprehension is
    anchored as well, because its generator targets bind inside it — they shadow
    the import for the calls it contains and for nothing else
    (PRRT_kwDOSJAM6s6q_M0s). A name *no* import binds is not reported on by this
    reader at all — it keeps the name-only rule the #1019 fixes and the
    parameter-receiver tolerance depend on, because an unknown local is exactly
    the unreadable binding that rule exists for. Text
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
        if not isinstance(node, _ANCHORED_SCOPES):
            continue
        if node.lineno <= line <= (node.end_lineno or node.lineno):
            bound.update(_names_bound_in_scope(node))
    anchored_class = _anchored_class_scope(tree, line)
    if anchored_class is not None:
        bound.update(_names_bound_in_scope(anchored_class))
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
    only a ``global`` declaration inside them reaches back out. A name the
    anchored function imports itself is local for the whole of that body, so
    the caller drops it from these names — see
    ``_function_local_import_names_at_line``. A top-level
    ``def`` / ``class`` of the name does shadow the import and is collected, as
    do a module-level ``match`` statement's capture, star and mapping-rest
    targets, whose names live on the pattern nodes instead of on an ``ast.Name``
    store (PRRT_kwDOSJAM6s6q-N1B).
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
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)
        pending.extend(ast.iter_child_nodes(node))
    return frozenset(bound)
