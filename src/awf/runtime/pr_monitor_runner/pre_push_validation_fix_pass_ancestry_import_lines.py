"""Physical-line readers the import scanners in the cross-file probe share.

An ``import`` statement is a *logical* line: it can carry a trailing comment,
wrap inside parentheses, or continue after a backslash, and it is only matchable
once those physical lines are reduced to the statement they spell. These readers
do that reduction. They live in their own module because
``pre_push_validation_fix_pass_ancestry_cross_file`` sits at the first-party line
budget, and because physical→logical line reading is one unit.
"""

from __future__ import annotations


def _import_statement_separator_positions(statement: str) -> list[int]:
    """Offsets of the top-level ``;`` in ``statement``.

    Semicolons inside brackets are skipped: a parenthesized target list is the
    only bracket an import head can open, and a ``;`` can never appear inside
    it, so a bracketed one belongs to an interpolation the masked scan retained
    rather than to this statement. Scanning for a bare ``;`` is otherwise exact,
    because the readers run over comment/string-masked lines and a semicolon
    cannot appear in a Python expression.
    """
    positions: list[int] = []
    depth = 0
    for position, char in enumerate(statement):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(depth - 1, 0)
        elif char == ";" and depth == 0:
            positions.append(position)
    return positions


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


def _import_line_without_continuation(line: str) -> str:
    """``line`` without its trailing backslash line-continuation marker.

    ``from pkg.mod import \\`` + ``record`` is one logical statement in Python, so
    the marker has to be dropped and the next physical line joined on before the
    import heads are matched. Left split, the head's target list is the backslash
    itself, ``record`` binds to no module and keeps the name-only rule that accepts
    an unrelated same-named definition in another package (PRRT_kwDOSJAM6s6rAf0X).
    """
    head = line.rstrip()
    return head[:-1] if head.endswith("\\") else head


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


def _joined_import_logical_line(lines: list[str], index: int) -> tuple[str, int]:
    """The logical line starting at ``lines[index]``, and the index just past it.

    Backslash continuations are joined before any import head is matched: the
    marker can split a statement either side of ``import``, so the head is only
    matchable once whole, and consuming the joined physical lines keeps the
    names they carry from being re-read as heads of their own. Both import
    readers need this — a ``from`` head (PRRT_kwDOSJAM6s6rAf0X) and a plain
    ``import pkg.other, \\`` + ``pkg.real as metrics``, whose receiver would
    otherwise bind to nothing and keep the name-only rule that accepts an
    unrelated same-named definition (PRRT_kwDOSJAM6s6rA-rt). Each physical line
    is read without its trailing comment. A continuation with no line after it
    has no target list; dropping the dangling marker leaves the head
    unmatchable, so it binds nothing.
    """
    line = _import_line_without_comment(lines[index])
    index += 1
    while line.rstrip().endswith("\\") and index < len(lines):
        line = (
            _import_line_without_continuation(line)
            + " "
            + _import_line_without_comment(lines[index]).strip()
        )
        index += 1
    return _import_line_without_continuation(line), index


def _import_logical_statements(lines: list[str], index: int) -> tuple[list[str], int]:
    """The simple statements the logical line at ``lines[index]`` spells, and the next index.

    A logical line can hold several simple statements separated by ``;``, and an
    ``import`` is one of them, so each separated statement has to be matched as
    a head of its own. Read whole instead, ``from pkg.mod import record; cache =
    {}`` hands the import scanners the target list ``record; cache = {}``, which
    binds ``record;`` and leaves ``record`` bound to no module — back on the
    name-only rule that accepts an unrelated same-named definition in another
    package (PRRT_kwDOSJAM6s6rBKOW). Plain imports carry the same shape, and an
    import that *follows* a separator binds nothing at all when the suffix is
    not split off. Only the last statement can hold an unbalanced bracket,
    because an open bracket continues the logical line past any later ``;``.
    """
    statement, index = _joined_import_logical_line(lines, index)
    start = 0
    statements: list[str] = []
    for position in _import_statement_separator_positions(statement):
        statements.append(statement[start:position])
        start = position + 1
    statements.append(statement[start:])
    return statements, index


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
