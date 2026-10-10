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
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from typing import Any, cast

from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_callees import (
    _ANCHORED_SCOPES,
    _DECORATOR_BASENAME_RE,
    _ENCLOSING_DEFINITION_RE,
    _anchored_class_scope,
    _definition_binding_scope,
    _definition_head_is_assignment,
    _definition_head_scan_lines,
    _definition_is_nested_in_other,
    _definition_span_is_class,
    _iter_definition_spans,
    _names_bound_in_scope,
    _path_allows_js_private_fields,
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
    _effective_scope_head_starts,
    _function_local_import_names_at_line,
    _import_lines_hidden_from,
    _rebound_scope_names,
    _without_shadowed_enclosing_definitions,
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
            for module_path, exact, _enclosed_by in bindings[key]
        )
    )


def _callee_enclosing_symbols_for_candidate(
    refs: frozenset[tuple[str, str]],
    bindings: dict[str, frozenset[_ModuleTarget]],
    candidate: str,
    *,
    call_site: str,
) -> dict[str, frozenset[str]]:
    """Symbols that must enclose each callee's definition inside ``candidate``.

    A receiver a ``from`` import binds is read two ways, and only one of them —
    the object the imported module itself defines — admits that module's own
    file. Under it the callee is a member of the *imported symbol*, so the span
    rule has to be held to that symbol or ``Other.record`` satisfies the gate
    for a ``Collector.record()`` call site (PRRT_kwDOSJAM6s6q-L4H).

    A name is reported only when *every* admitting target of *every* ref that
    carries it names an enclosing symbol; a ref with no readable binding, or a
    target admitting ``candidate`` under a reading that places no such
    requirement (a submodule receiver, a plain ``import``), leaves the name
    unrestricted — the same name-only tolerance
    :func:`_callee_names_bound_to_candidate` keeps.
    """
    required: dict[str, set[str]] = {}
    unrestricted: set[str] = set()
    for key, name in refs:
        targets = bindings.get(key)
        if not targets:
            unrestricted.add(name)
            continue
        for module_path, exact, enclosed_by in targets:
            if not _candidate_is_under_module_path(
                candidate, module_path, call_site=call_site, exact=exact
            ):
                continue
            if enclosed_by is None:
                unrestricted.add(name)
            else:
                required.setdefault(name, set()).add(enclosed_by)
    return {
        name: frozenset(symbols) for name, symbols in required.items() if name not in unrestricted
    }


def _callee_names_bound_to_path(
    path: str,
    *,
    refs: frozenset[tuple[str, str]],
    bare_refs: frozenset[tuple[str, str]],
    module_refs: frozenset[tuple[str, str]],
    receiver_bindings: dict[str, frozenset[_ModuleTarget]],
    bare_bindings: dict[str, frozenset[_ModuleTarget]],
    call_site: str,
) -> tuple[frozenset[str], frozenset[str], dict[str, frozenset[str]]]:
    """``(qualified, bare, enclosed_by)`` for the callees ``path`` is bound to.

    The first two are the callee names whose import binding admits ``path``, by
    call shape; ``enclosed_by`` holds the symbols a qualified name's definition
    must sit under inside ``path`` (PRRT_kwDOSJAM6s6q-L4H).

    Applied to a changed path to pick its callees, and again to that path's
    rename target so a move is only evidence while the moved definition is
    still reachable through the same binding (PRRT_kwDOSJAM6s6q-L4B).
    """
    qualified_refs = refs - module_refs
    qualified = _callee_names_bound_to_candidate(
        qualified_refs, receiver_bindings, path, call_site=call_site
    )
    bare = _callee_names_bound_to_candidate(
        bare_refs, bare_bindings, path, call_site=call_site
    ) | _callee_names_bound_to_candidate(module_refs, receiver_bindings, path, call_site=call_site)
    enclosed_by = _callee_enclosing_symbols_for_candidate(
        qualified_refs, receiver_bindings, path, call_site=call_site
    )
    return qualified, bare, enclosed_by


def _bindings_fail_closed_when_unbound(
    bindings: dict[str, frozenset[_ModuleTarget]], keys: frozenset[str]
) -> dict[str, frozenset[_ModuleTarget]]:
    """``bindings`` with every key in ``keys`` it does not bind held unmatchable.

    The name-only rule in :func:`_callee_names_bound_to_candidate` exists for a
    call site whose import this reader cannot see. It must not hand a rename
    target a second, binding-free chance at acceptance, so a key with no
    readable import fails closed on ``_AMBIGUOUS_IMPORT_TARGET`` instead
    (PRRT_kwDOSJAM6s6q-L4B).
    """
    unbound: dict[str, frozenset[_ModuleTarget]] = dict.fromkeys(keys, _AMBIGUOUS_IMPORT_TARGET)
    return unbound | {key: targets for key, targets in bindings.items() if targets}


def _receiver_bindings_through_chains(
    bindings: dict[str, frozenset[_ModuleTarget]],
    chains: dict[str, tuple[str, ...] | None],
) -> dict[str, frozenset[_ModuleTarget]]:
    """``bindings`` with each chained key re-pointed at the module its chain spells.

    ``import pkg_b as api`` plus ``api.metrics.record()`` reaches
    ``pkg_b/metrics``, not every module under ``pkg_b``: keeping the chain
    root's own target discards the ``metrics`` segment and lets a correction to
    a same-named definition in a sibling such as ``pkg_b/unrelated.py`` satisfy
    the gate while ``api.metrics.record`` stays untouched
    (PRRT_kwDOSJAM6s6q_M0j). A chain whose segments are unresolvable (None),
    and the reading where the root is an *object* an imported module defines —
    whose attribute is no module path — name no module at all, so they resolve
    to nothing and the key is held to the unmatchable target every chained key
    already fails closed on.
    """
    extended = dict(bindings)
    for key, segments in chains.items():
        targets = bindings.get(key, frozenset()) if segments is not None else frozenset()
        tail = segments or ()
        extended[key] = frozenset(
            ("/".join([module_path, *tail]), False, None)
            for module_path, _exact, enclosed_by in targets
            if module_path and enclosed_by is None
        )
    return _bindings_fail_closed_when_unbound(extended, frozenset(chains))


def _caller_binding_resolver(
    caller_text: str,
    *,
    call_site: str,
    rebound: frozenset[str],
    refs: frozenset[tuple[str, str]],
    bare_refs: frozenset[tuple[str, str]],
    chained_receivers: dict[str, tuple[str, ...] | None] | None = None,
    require_binding: bool = False,
    line: int | None = None,
) -> Callable[[str], tuple[frozenset[str], frozenset[str], dict[str, frozenset[str]]]]:
    """Bind ``refs`` to one caller text's imports, as a path-keyed callable.

    A receiver a plain ``import`` binds is a module, so its callee is held to
    module scope like a bare one; every other receiver may be an instance and
    keeps the class-member tolerance (PRRT_kwDOSJAM6s6q8-M1).
    ``chained_receivers`` maps the keys a chained receiver resolved to onto the
    chain segments following the key, appended to the key's module path so the
    callee is held to the module the whole chain spells
    (PRRT_kwDOSJAM6s6q_M0j). Such a key never takes the name-only fallback
    either: an attribute chain is not a name an import can bind, so a root this
    reader cannot tie to the candidate fails closed instead of accepting any
    reachable same-named definition
    (PRRT_kwDOSJAM6s6q-6LK). ``require_binding`` drops that fallback for every
    key, for the re-read that has to *prove* a rename target reachable rather
    than merely not refute it; it composes with the chain extension instead of
    replacing it, so that re-read cannot re-widen a chained callee back to the
    chain root's subtree (PRRT_kwDOSJAM6s6q_M0j). ``line`` scopes the import
    readers to the bindings visible there, and is None for a ``caller_text``
    the anchored line does not index (PRRT_kwDOSJAM6s6rA-ru).
    """
    bare_bindings = _import_targets_without_locally_rebound(
        _bare_name_import_module_targets(caller_text, path=call_site, line=line), rebound
    )
    receiver_bindings = _import_targets_without_locally_rebound(
        _receiver_import_module_targets(caller_text, path=call_site, line=line), rebound
    )
    if chained_receivers:
        receiver_bindings = _receiver_bindings_through_chains(receiver_bindings, chained_receivers)
    if require_binding:
        bare_bindings = _bindings_fail_closed_when_unbound(
            bare_bindings, frozenset(key for key, _ in bare_refs)
        )
        receiver_bindings = _bindings_fail_closed_when_unbound(
            receiver_bindings, frozenset(key for key, _ in refs)
        )
    module_receivers = _module_bound_receiver_names(caller_text, path=call_site, line=line)
    return partial(
        _callee_names_bound_to_path,
        refs=refs,
        bare_refs=bare_refs,
        module_refs=frozenset(ref for ref in refs if ref[0] in module_receivers),
        receiver_bindings=receiver_bindings,
        bare_bindings=bare_bindings,
        call_site=call_site,
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


def _definition_is_member_of_named_module_scope_definition(
    all_spans: list[tuple[str, int, int, int]],
    names: frozenset[str],
    *,
    start: int,
    indent: int,
) -> bool:
    """True when ``start`` is a direct member of a module-level ``names`` head.

    The scope a receiver's imported symbol pins: ``Collector.record()`` reaches
    the ``record`` declared directly in a module-level ``Collector``, so a
    member of a *different* class in that file, a module-level definition, and a
    member of a same-named class nested inside another one are all something the
    call site cannot reach (PRRT_kwDOSJAM6s6q-L4H).
    "Module-level" is *one* enclosing definition, not a textual indent of 0: a
    class declared under a module-level ``if``/``try`` is indented yet still
    binds in the module globals the import reads, so requiring indent 0 would
    drop a real correction to its member (PRRT_kwDOSJAM6s6q-8T_).
    """
    enclosing = [
        name
        for name, span_start, span_end, span_indent in all_spans
        if span_start < start <= span_end and span_indent < indent
    ]
    return len(enclosing) == 1 and enclosing[0] in names


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
    enclosed_by: dict[str, frozenset[str]] | None = None,
) -> list[tuple[int, int]]:
    """Spans in ``file_text`` another module could reach as the named callee.

    Returns ``(start, end)`` line spans for every ``def`` / ``class`` /
    ``function`` / arrow head whose name matches, under the reachability rule
    its *call shape* allows. ``names`` are attribute-qualified callees
    (``metrics.record()``), whose receiver may be a module or an instance, so a
    direct member of a module-level class counts alongside a module-level head.
    ``enclosed_by`` narrows that for a name whose receiver import pins the
    object it reaches through — ``from M import Collector`` matched against
    ``M``'s own file — to a direct member of a module-level definition it names,
    so a same-named method of another class in that file fails closed
    (PRRT_kwDOSJAM6s6q-L4H).
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

    When one scope binds one name more than once, only the last of those
    definitions is returned: a module or class body binds the name to the
    definition it executes last, so an earlier same-named definition is dead
    code the importing call site cannot reach, and a correction confined to it
    changes nothing that call does (PRRT_kwDOSJAM6s6q9Wnf). This is the rule
    attempt 0's ``_resolve_callee_definition_span`` already applies in-file.
    Duplicates whose effective head that order cannot prove are withheld
    entirely, and the fold is per binding scope so same-named members of
    distinct classes stay distinct — see ``_effective_scope_head_starts``.

    The same rule holds the candidate's *enclosing* heads: a member is reached
    only through the object its enclosing definition binds, so a method of the
    first of two ``class Collector`` declarations is dead code even though the
    pinned name sits on its enclosing class
    (PRRT_kwDOSJAM6s6rAhm1) — see ``_without_shadowed_enclosing_definitions``.

    That order only covers the heads this scan recognizes, so a scope that
    rebinds the name with a statement carrying no head of its own — a plain
    ``validate = replacement`` (PRRT_kwDOSJAM6s6q_ywa) or an ``import`` of that
    same name (PRRT_kwDOSJAM6s6rAAWY), both invisible to definition discovery
    and the binding an import of this module then reaches — withholds every
    span of that name as well.
    """
    if not (names or bare_names) or not file_text:
        return []
    all_spans = _iter_definition_spans(file_text, path=path)
    js_ts = _path_allows_js_private_fields(path)
    collected: list[tuple[str, int, int, tuple[int, int], tuple[int, int]]] = []
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
            pinned = (enclosed_by or {}).get(name)
            if pinned is not None and not _definition_is_member_of_named_module_scope_definition(
                all_spans, pinned, start=start, indent=indent
            ):
                continue
        elif _definition_is_nested_in_other(all_spans, start=start, indent=indent):
            continue
        span = (_definition_span_start_with_decorators(file_text, start), end)
        scope = _definition_binding_scope(file_text, all_spans, start=start, indent=indent)
        collected.append((name, start, indent, scope, span))
    collected = _without_shadowed_enclosing_definitions(
        file_text, all_spans, path=path, collected=collected
    )
    effective = _effective_scope_head_starts(collected)
    rebound = _rebound_scope_names(file_text, all_spans, path=path, collected=collected)
    return [
        span
        for name, start, _indent, (scope_start, _body_indent), span in collected
        if effective.get((scope_start, name)) == start and (scope_start, name) not in rebound
    ]


async def _caller_binding_after_correction(
    self: Any,
    *,
    worktree_path: Path,
    right: str,
    call_site: str,
    rebound: frozenset[str],
    refs: frozenset[tuple[str, str]],
    bare_refs: frozenset[tuple[str, str]],
    chained_receivers: dict[str, tuple[str, ...] | None] | None = None,
) -> Callable[[str], tuple[frozenset[str], frozenset[str], dict[str, frozenset[str]]]] | None:
    """The caller's own import binding *after* the correction, or None if unreadable.

    One correction can move the callee and update this caller's import together;
    the binding that then has to admit the rename target is the corrected
    caller's, not the stale left-side one, so holding the move to the left-side
    binding alone would fail a complete fix closed. Names the left side rebinds
    stay unmatchable here too, and a caller whose right-side text is unreadable
    — or which no longer imports the callee at all — fails closed rather than
    falling back to the name-only rule (PRRT_kwDOSJAM6s6q-L4B).

    A chained receiver carries its chain here too: re-reading the root's
    binding alone would hand the move the root's whole subtree and accept a
    same-named definition the chain never reaches (PRRT_kwDOSJAM6s6q_M0j).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _path_text_at_ref,
    )

    caller_text = await _path_text_at_ref(
        self, worktree_path=worktree_path, ref=right, path=call_site
    )
    if not caller_text:
        return None
    return _caller_binding_resolver(
        caller_text,
        call_site=call_site,
        rebound=rebound,
        refs=refs,
        bare_refs=bare_refs,
        chained_receivers=chained_receivers,
        require_binding=True,
    )


async def _callee_definition_survives_at_right(
    self: Any,
    *,
    worktree_path: Path,
    right: str,
    path: str,
    names: frozenset[str],
    bare_names: frozenset[str],
    enclosed_by: dict[str, frozenset[str]],
) -> bool:
    """True when ``path`` still holds a reachable definition of the callee at ``right``.

    The spans the overlap check runs against are read at ``left``, so a
    correction that *deletes* the callee — its whole file, or just its
    definition out of a surviving file — produces a deletion hunk overlapping
    the old span and would otherwise read as evidence that the reviewed call
    site was fixed, while the unchanged caller now references a missing callee
    (PRRT_kwDOSJAM6s6q8MWy). ``path`` is the candidate's rename target when the
    range moved it, so an already-accepted move keeps resolving — and
    ``enclosed_by`` is then that target's own requirement, because a receiver
    pinned to the imported symbol in one path need not be pinned in the other
    (PRRT_kwDOSJAM6s6q-L4H). Fails closed when the right-side text is
    unreadable.
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _path_text_at_ref,
    )

    right_text = await _path_text_at_ref(self, worktree_path=worktree_path, ref=right, path=path)
    if not right_text:
        return False
    return bool(
        _importable_definition_spans_for_names(
            right_text, names, path=path, bare_names=bare_names, enclosed_by=enclosed_by
        )
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
    file_text: str, line: int, *, path: str, rebound: frozenset[str]
) -> tuple[
    frozenset[tuple[str, str]], frozenset[tuple[str, str]], dict[str, tuple[str, ...] | None]
]:
    """Callee refs at ``line`` that may resolve in another file.

    Returns ``(attribute_qualified, bare, chained_receivers)``. The first two
    are separate because the two call shapes reach different definitions across
    a module boundary — see ``_importable_definition_spans_for_names``. Each ref
    is a ``(binding_key, name)`` pair, the key being the name whose import
    binding narrows the candidate path: a qualified callee binds through its
    receiver, a bare callee through itself. The two differ for an aliased bare
    callee, whose key is the local binding while the definition to look for
    carries the imported symbol's name (PRRT_kwDOSJAM6s6q9WnP); a qualified
    callee's name is an attribute of its receiver, which no import renames.

    A callee reached through a *chain* of receivers (``api.metrics.record()``)
    does not bind through its immediate qualifier ``metrics``, which is an
    attribute no import can bind — keying such a ref on the qualifier would
    leave it on the name-only rule and let a correction to any reachable
    ``record`` resolve the thread while ``api.metrics.record`` stayed untouched.
    A chain that itself spells a plain-imported module
    (``pkg.obs.metrics.record()``) is that module's receiver and keeps the
    qualifier its import already binds — unless ``rebound`` holds the chain's
    root, in which case the call reaches an attribute of that binding rather
    than the module the chain spells, so it takes the root-keyed path below and
    fails closed there (PRRT_kwDOSJAM6s6rAh8I); any other chain is keyed on its root —
    the one link an import can bind — and reported as chained, so the resolver
    holds it closed instead of falling back to the name-only rule when the root
    reaches nothing the candidate can satisfy (PRRT_kwDOSJAM6s6q-6LK). The
    segments *between* the root and the callee ride along with that key, so the
    resolver holds the callee to the module the whole chain spells rather than
    to the root's subtree; they are None for an unreadable chain and for a root
    two chains share, which the one-key-per-name resolver cannot hold apart
    (PRRT_kwDOSJAM6s6q_M0j).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_callees import (
        _callee_refs_from_file_line,
        _receiver_chain_segments_from_file_line,
    )

    refs = _callee_refs_from_file_line(file_text, line, path=path)
    chains = _receiver_chain_segments_from_file_line(file_text, line, path=path)
    plain_imports = _plain_import_module_paths(file_text, path=path, line=line)
    qualified: set[tuple[str, str]] = set()
    chained_receivers: dict[str, tuple[str, ...] | None] = {}
    for qualifier, name in refs:
        if qualifier is None or qualifier in _IN_FILE_CALLEE_QUALIFIERS:
            continue
        if qualifier not in chains:
            qualified.add((qualifier, name))
            continue
        chain = chains[qualifier]
        if (
            chain is not None
            and chain[0] not in rebound
            and "/".join(chain) in plain_imports.get(qualifier, frozenset())
        ):
            # ``import pkg.obs.metrics`` binds the receiver this chain spells,
            # under its last segment — the qualifier already carries it.
            qualified.add((qualifier, name))
            continue
        # An unreadable chain keeps the qualifier as the key; either way the key
        # is reported as chained, which is what holds it closed.
        key = chain[0] if chain is not None else qualifier
        segments = chain[1:] if chain is not None else None
        if key in chained_receivers and chained_receivers[key] != segments:
            segments = None
        chained_receivers[key] = segments
        qualified.add((key, name))
    imported = _bare_name_imported_definition_names(file_text, path=path, line=line)
    bare = frozenset(
        (name, imported.get(name, name)) for qualifier, name in refs if qualifier is None
    )
    return frozenset(qualified), bare, chained_receivers


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
    never imported is not evidence (PRRT_kwDOSJAM6s6q7bSI). A callee reached through a chain of
    receivers binds through the chain's root instead of the attribute in front
    of it, and never takes the name-only fallback, so an edit to a same-named
    definition in a module that root cannot reach is not evidence
    (PRRT_kwDOSJAM6s6q-6LK); the chain's remaining segments extend that root's
    module path, so a sibling module under it is not evidence either, and a
    chain this reader cannot resolve to one module fails closed
    (PRRT_kwDOSJAM6s6q_M0j). A callee whose
    binding is unreadable keeps the name-only rule, which is the #1019 shape the
    gate exists for; a receiver imported by name resolves to that name's own
    module or to the importing module's file, not to any sibling under its
    package (PRRT_kwDOSJAM6s6q8MW9). The receiver's binding *kind* is kept too:
    a receiver a plain ``import`` proves to be a module reaches only
    module-level definitions, so editing a same-named method of a class in that
    module is not evidence about the call (PRRT_kwDOSJAM6s6q8-M1); a receiver a
    ``from`` import binds to an object of the imported module reaches only that
    object's own members there, so a same-named method of another class in the
    same file is not evidence either (PRRT_kwDOSJAM6s6q-L4H). A name two
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
    change to its body; that target is held to an import binding that reaches it
    — the one the old path satisfied, or the corrected caller's own — so a move
    *out* of the module the caller still imports fails closed instead of reading
    a now-broken import as a fix
    (PRRT_kwDOSJAM6s6q-L4B). An overlap is accepted only when *that* callee is still
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
    # A module-scope rebinding cannot reach a name the anchored function
    # imports itself: that import binds the name for the whole body, so the
    # global is unreachable there (PRRT_kwDOSJAM6s6rAhm2). Read *before* the
    # callee names, which consume it to decide whether a chain spelling a
    # plain-imported module still reaches that module: deferring this read
    # until after that call reopens the rebound-root shortcut
    # (PRRT_kwDOSJAM6s6rAh8I).
    rebound = _locally_rebound_names_at_line(item_text, item_line, path=normalized_item) | (
        _module_scope_rebound_names(item_text, path=normalized_item)
        - _function_local_import_names_at_line(item_text, item_line)
    )
    names, bare_names, chained_receivers = _cross_file_callee_names(
        item_text, item_line, path=normalized_item, rebound=rebound
    )
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
    bound_to = _caller_binding_resolver(
        item_text,
        call_site=normalized_item,
        rebound=rebound,
        refs=names,
        bare_refs=bare_names,
        chained_receivers=chained_receivers,
        line=item_line,
    )
    bound_to_after: (
        Callable[[str], tuple[frozenset[str], frozenset[str], dict[str, frozenset[str]]]] | None
    ) = None
    read_binding_after = False
    for candidate in candidates:
        candidate_names, candidate_bare, enclosed_by = bound_to(candidate)
        # The scope a pinned callee must sit under belongs to the path being
        # read, so the survival read at ``right`` — which runs against the
        # rename target when the range moved the candidate — carries that
        # target's own requirement rather than the old path's
        # (PRRT_kwDOSJAM6s6q-L4H).
        survival_enclosed_by = enclosed_by
        rename_target = rename_map.get(candidate)
        if rename_target is not None and rename_target != candidate:
            # A move that carries the callee out of the module the *unchanged*
            # caller's import binds leaves that import resolving to nothing, so
            # following the rename target would read a broken call site as
            # fixed. The target is held to the same binding that admitted the
            # old path; when that binding no longer reaches it, the only other
            # binding that can is the corrected caller's own, read once at
            # ``right``, and the move fails closed when neither reaches it
            # (PRRT_kwDOSJAM6s6q-L4B).
            moved_names, moved_bare, moved_enclosed_by = bound_to(rename_target)
            if not (candidate_names & moved_names or candidate_bare & moved_bare):
                if not read_binding_after:
                    read_binding_after = True
                    bound_to_after = await _caller_binding_after_correction(
                        self,
                        worktree_path=worktree_path,
                        right=right,
                        call_site=rename_map.get(normalized_item) or normalized_item,
                        rebound=rebound,
                        refs=names,
                        bare_refs=bare_names,
                        chained_receivers=chained_receivers,
                    )
                if bound_to_after is not None:
                    after_names, after_bare, after_enclosed_by = bound_to_after(rename_target)
                    moved_names |= after_names
                    moved_bare |= after_bare
                    # The corrected caller's reading is the one that admitted
                    # the target, so its requirement takes precedence for a
                    # name both readings carry.
                    moved_enclosed_by = {**moved_enclosed_by, **after_enclosed_by}
            candidate_names &= moved_names
            candidate_bare &= moved_bare
            survival_enclosed_by = moved_enclosed_by
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
                candidate_text,
                one_names,
                path=candidate,
                bare_names=one_bare,
                enclosed_by=enclosed_by,
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
                enclosed_by=survival_enclosed_by,
            ):
                return True
    return False
